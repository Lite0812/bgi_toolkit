from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
import traceback
from typing import Callable, Iterator

from .asdis.common.bgi_dialog_json import (
    extract_dialog_json_from_bsd,
    extract_push_string_json_from_bpd,
    import_dialog_json_to_bsd,
    import_push_string_json_to_bpd,
)
from .asdis.common.bgi_dialog_txt import (
    extract_dialog_txt_from_bsd,
    extract_push_string_txt_from_bpd,
    import_dialog_txt_to_bsd,
    import_push_string_txt_to_bpd,
)
from .asdis.service import (
    assemble_bp,
    assemble_story,
    detect_bsd_version,
    detect_script_version,
    disassemble_bp,
    disassemble_story,
)
from .bpd2c import pipeline as bpd_pipeline
from .bp_engine import detect_engine
from .bsd2c import pipeline as bsd_pipeline
from .detector import detect_input, iter_inputs, non_story_notice
from .models import (
    Artifact,
    Diagnostic,
    InputKind,
    Operation,
    ProgressCallback,
    TaskRequest,
    TaskResult,
    TextFormat,
)


SUPPORTED_INPUTS = {
    InputKind.STORY_BINARY,
    InputKind.BSD,
    InputKind.BP_BINARY,
    InputKind.BPD,
}


class ToolkitRunner:
    def __init__(
        self,
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.progress = progress or (lambda message: None)
        self.cancelled = cancelled or (lambda: False)

    def log(self, message: str) -> None:
        self.progress(message)

    def execute(self, request: TaskRequest) -> TaskResult:
        result = TaskResult()
        if not request.input_path.exists():
            result.diagnostics.append(
                Diagnostic("error", f"Input does not exist: {request.input_path}", request.input_path)
            )
            return result

        if request.operation is Operation.INSPECT:
            return self._inspect(request)

        accepted = self._accepted_kinds(request.operation)
        def skipped_resource(path, kind):
            message = non_story_notice(path, kind)
            result.diagnostics.append(Diagnostic("warning", message, path))
            self.log(message)

        sources = iter_inputs(
            request.input_path,
            accepted,
            recursive=request.options.recursive,
            on_skip=skipped_resource if request.operation in {Operation.DISASSEMBLE, Operation.TEXT_EXPORT} else None,
        )
        if not sources:
            result.diagnostics.append(
                Diagnostic(
                    "error",
                    f"No supported inputs for {request.operation.value}: {request.input_path}",
                    request.input_path,
                )
            )
            return result

        if request.input_path.is_dir() and request.operation in {
            Operation.DECOMPILE,
            Operation.TEXT_EXPORT,
        }:
            sources, skipped = self._prefer_text_sources(sources)
            for binary_source, text_source in skipped:
                result.diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"Skipped because a matching intermediate source exists: {text_source}",
                        binary_source,
                    )
                )

        input_root = request.input_path if request.input_path.is_dir() else request.input_path.parent
        with self._workspace(request) as workspace:
            if request.operation is Operation.DECOMPILE and request.input_path.is_dir():
                self._decompile_many(request, sources, input_root, workspace, result)
                return result
            total = len(sources)
            for index, source in enumerate(sources, start=1):
                if self.cancelled():
                    result.diagnostics.append(Diagnostic("warning", "Task cancelled"))
                    break
                kind = detect_input(source)
                self.log(f"[{index}/{total}] {request.operation.value}: {source}")
                try:
                    artifacts = self._execute_one(
                        request,
                        source,
                        kind,
                        input_root,
                        workspace,
                    )
                    result.artifacts.extend(artifacts)
                except Exception as exc:
                    result.diagnostics.append(Diagnostic("error", str(exc), source))
                    self.log(traceback.format_exc().rstrip())
        return result

    def _decompile_many(
        self,
        request: TaskRequest,
        sources: list[Path],
        input_root: Path,
        workspace: Path,
        task_result: TaskResult,
    ) -> None:
        story_records: list[tuple[Path, Path]] = []
        bp_records: list[tuple[Path, Path]] = []
        total = len(sources)
        for index, source in enumerate(sources, start=1):
            if self.cancelled():
                task_result.diagnostics.append(Diagnostic("warning", "Task cancelled"))
                return
            kind = detect_input(source)
            self.log(f"[{index}/{total}] prepare: {source}")
            try:
                if kind is InputKind.STORY_BINARY:
                    if detect_script_version(source, request.options.asdis) == "v0":
                        raise ValueError(
                            "v0 story decompilation is not supported by bsd2c; use disassemble or text export"
                        )
                elif kind is InputKind.BSD and detect_bsd_version(source) == "v0":
                    raise ValueError(
                        "v0 BSD decompilation is not supported by bsd2c; its opcode semantics differ from bs5"
                    )
                if request.options.output_format == "rpy" and kind in {
                    InputKind.BP_BINARY,
                    InputKind.BPD,
                }:
                    raise ValueError("RPY output is only supported for story/BSD inputs")
                intermediate, artifacts = self._intermediate_source(
                    request,
                    source,
                    kind,
                    input_root,
                    workspace,
                )
                task_result.artifacts.extend(artifacts)
                if kind in {InputKind.STORY_BINARY, InputKind.BSD}:
                    story_records.append((source, intermediate))
                else:
                    bp_records.append((source, intermediate))
            except Exception as exc:
                task_result.diagnostics.append(Diagnostic("error", str(exc), source))
                self.log(traceback.format_exc().rstrip())

        output_root = request.output_path or self._default_output_root(request)
        if story_records:
            override = None if request.options.bsd_hint == "auto" else request.options.bsd_hint
            definitions_target = output_root / "bsd2c_defs.json"
            try:
                story_results = bsd_pipeline.decompile_files(
                    [intermediate for _, intermediate in story_records],
                    override,
                    inference_progress_callback=lambda pass_index, index, count, path: self.log(
                        f"signature pass {pass_index}: {index}/{count} {path.name}"
                    ),
                    definitions_override_path=definitions_target,
                )
                project_context = (
                    bsd_pipeline.build_project_rpy_context(story_results)
                    if request.options.output_format == "rpy"
                    else None
                )
                for (source, _), result in zip(story_records, story_results, strict=True):
                    output_name = bsd_pipeline.derive_output_path_for_format(
                        Path(source.name),
                        result.detected_type,
                        request.options.output_format,
                    ).name
                    destination = self._destination(request, source, input_root, output_name)
                    written = bsd_pipeline.write_result(
                        result,
                        destination,
                        output_format=request.options.output_format,
                        project_context=project_context,
                    )
                    task_result.artifacts.append(
                        Artifact(request.options.output_format, written, source)
                    )
                    if request.options.emit_ir:
                        ir_path = bsd_pipeline.write_ir_result(
                            result,
                            bsd_pipeline.derive_ir_output_path(written),
                        )
                        task_result.artifacts.append(Artifact("ir", ir_path, source))
                definitions = bsd_pipeline.write_definition_bundle(
                    story_results,
                    definitions_target,
                )
                task_result.artifacts.append(
                    Artifact("definitions", definitions, request.input_path)
                )
            except Exception as exc:
                task_result.diagnostics.append(
                    Diagnostic("error", f"BSD batch decompilation failed: {exc}", request.input_path)
                )
                self.log(traceback.format_exc().rstrip())

        if bp_records:
            try:
                bp_results = bpd_pipeline.decompile_files(
                    [intermediate for _, intermediate in bp_records],
                    progress=self.log,
                    include_source_comments=request.options.include_source_comments,
                    engine=request.options.asdis.bp_engine,
                    games_path=request.options.asdis.bp_games,
                    executable=request.options.asdis.bp_executable,
                )
                for (source, _), result in zip(bp_records, bp_results, strict=True):
                    destination = self._destination(
                        request,
                        source,
                        input_root,
                        f"{source.name}.c",
                    )
                    written = bpd_pipeline.write_result(result, destination)
                    task_result.artifacts.append(Artifact("c", written, source))
                    if request.options.emit_ir:
                        ir_path = bpd_pipeline.write_ir_result(
                            result,
                            destination.with_suffix(".ir.txt"),
                        )
                        task_result.artifacts.append(Artifact("ir", ir_path, source))
            except Exception as exc:
                task_result.diagnostics.append(
                    Diagnostic("error", f"BPD batch decompilation failed: {exc}", request.input_path)
                )
                self.log(traceback.format_exc().rstrip())

    def _inspect(self, request: TaskRequest) -> TaskResult:
        result = TaskResult()
        target = request.input_path
        if target.is_file():
            result.inspected.append((target, detect_input(target)))
            return result
        iterator = target.rglob("*") if request.options.recursive else target.glob("*")
        for path in sorted(candidate for candidate in iterator if candidate.is_file()):
            kind = detect_input(path)
            if kind is not InputKind.UNKNOWN:
                result.inspected.append((path, kind))
        return result

    @staticmethod
    def _accepted_kinds(operation: Operation) -> set[InputKind]:
        if operation is Operation.DISASSEMBLE:
            return {InputKind.STORY_BINARY, InputKind.BP_BINARY}
        if operation is Operation.ASSEMBLE:
            return {InputKind.BSD, InputKind.BPD}
        if operation in {Operation.DECOMPILE, Operation.TEXT_EXPORT, Operation.TEXT_IMPORT}:
            return set(SUPPORTED_INPUTS)
        return set()

    @staticmethod
    def _source_identity(path: Path, kind: InputKind) -> tuple[Path, str, str]:
        name = path.name
        if kind is InputKind.BSD:
            stem = name[:-8] if name.lower().endswith(".bsd.txt") else path.stem
            family = "story"
        elif kind is InputKind.STORY_BINARY:
            stem = path.stem if path.suffix.lower() == "._bs" else name
            family = "story"
        else:
            stem = path.stem
            family = "bp"
        return path.parent.resolve(), family, stem.lower()

    def _prefer_text_sources(
        self,
        sources: list[Path],
    ) -> tuple[list[Path], list[tuple[Path, Path]]]:
        text_sources: dict[tuple[Path, str, str], Path] = {}
        for source in sources:
            kind = detect_input(source)
            if kind in {InputKind.BSD, InputKind.BPD}:
                text_sources[self._source_identity(source, kind)] = source
        kept: list[Path] = []
        skipped: list[tuple[Path, Path]] = []
        for source in sources:
            kind = detect_input(source)
            if kind in {InputKind.STORY_BINARY, InputKind.BP_BINARY}:
                text_source = text_sources.get(self._source_identity(source, kind))
                if text_source is not None:
                    skipped.append((source, text_source))
                    continue
            kept.append(source)
        return kept, skipped

    @contextmanager
    def _workspace(self, request: TaskRequest) -> Iterator[Path]:
        if request.options.keep_intermediate:
            if request.output_path is not None:
                base = request.output_path if request.input_path.is_dir() else request.output_path.parent
            elif request.input_path.is_dir():
                base = self._default_output_root(request)
            else:
                base = request.input_path.parent
            workspace = base / "_intermediate"
            workspace.mkdir(parents=True, exist_ok=True)
            yield workspace
            return
        with tempfile.TemporaryDirectory(prefix="bgi_toolkit_") as temp_dir:
            yield Path(temp_dir)

    def _execute_one(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
        workspace: Path,
    ) -> list[Artifact]:
        if request.operation is Operation.DISASSEMBLE:
            return self._disassemble(request, source, kind, input_root)
        if request.operation is Operation.ASSEMBLE:
            return self._assemble(request, source, kind, input_root)
        if request.operation is Operation.DECOMPILE:
            return self._decompile(request, source, kind, input_root, workspace)
        if request.operation is Operation.TEXT_EXPORT:
            return self._text_export(request, source, kind, input_root, workspace)
        if request.operation is Operation.TEXT_IMPORT:
            return self._text_import(request, source, kind, input_root, workspace)
        raise ValueError(f"Unsupported operation: {request.operation.value}")

    def _default_output_root(self, request: TaskRequest) -> Path:
        suffixes = {
            Operation.DISASSEMBLE: "_out",
            Operation.ASSEMBLE: "_build",
            Operation.DECOMPILE: "_decompiled",
            Operation.TEXT_EXPORT: "_text",
            Operation.TEXT_IMPORT: "_translated",
        }
        suffix = suffixes.get(request.operation, "_out")
        return request.input_path.parent / f"{request.input_path.name}{suffix}"

    def _destination(
        self,
        request: TaskRequest,
        source: Path,
        input_root: Path,
        output_name: str,
    ) -> Path:
        if request.input_path.is_file():
            return request.output_path or source.with_name(output_name)
        output_root = request.output_path or self._default_output_root(request)
        relative_parent = source.relative_to(input_root).parent
        return output_root / relative_parent / output_name

    @staticmethod
    def _disassembly_name(source: Path, kind: InputKind) -> str:
        if kind is InputKind.BP_BINARY:
            return f"{source.stem}.bpd"
        if source.suffix.lower() == "._bs":
            return f"{source.stem}.bsd"
        return f"{source.name}.bsd"

    @staticmethod
    def _assembly_name(source: Path, kind: InputKind) -> str:
        if kind is InputKind.BPD:
            return f"{source.stem}._bp"
        lower_name = source.name.lower()
        if lower_name.endswith(".bsd.txt"):
            return source.name[:-8]
        return source.stem

    def _disassemble(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
    ) -> list[Artifact]:
        destination = self._destination(
            request,
            source,
            input_root,
            self._disassembly_name(source, kind),
        )
        if kind is InputKind.STORY_BINARY:
            written, version = disassemble_story(source, destination, request.options.asdis)
            self.log(f"Detected story version: {version}")
            return [Artifact("bsd", written, source)]
        written = disassemble_bp(source, destination, request.options.asdis)
        options = request.options.asdis
        self.log(detect_engine(source, options.bp_engine, games_path=options.bp_games,
                              executable=options.bp_executable).describe())
        return [Artifact("bpd", written, source)]

    def _assemble(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
    ) -> list[Artifact]:
        destination = self._destination(
            request,
            source,
            input_root,
            self._assembly_name(source, kind),
        )
        if kind is InputKind.BSD:
            written, version = assemble_story(source, destination, request.options.asdis)
            self.log(f"Detected BSD version: {version}")
            return [Artifact("story-binary", written, source)]
        written = assemble_bp(source, destination, request.options.asdis)
        return [Artifact("bp-binary", written, source)]

    def _intermediate_source(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
        workspace: Path,
    ) -> tuple[Path, list[Artifact]]:
        if kind in {InputKind.BSD, InputKind.BPD}:
            return source, []
        relative_parent = source.relative_to(input_root).parent
        destination = workspace / relative_parent / self._disassembly_name(source, kind)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if kind is InputKind.STORY_BINARY:
            written, _ = disassemble_story(
                source, destination, request.options.asdis,
                use_function_names=request.operation not in {Operation.TEXT_EXPORT, Operation.TEXT_IMPORT},
            )
            artifact_kind = "bsd"
        else:
            written = disassemble_bp(source, destination, request.options.asdis)
            artifact_kind = "bpd"
        artifacts = (
            [Artifact(f"intermediate-{artifact_kind}", written, source)]
            if request.options.keep_intermediate
            else []
        )
        return written, artifacts

    def _decompile(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
        workspace: Path,
    ) -> list[Artifact]:
        if request.options.output_format == "rpy" and kind in {
            InputKind.BP_BINARY,
            InputKind.BPD,
        }:
            raise ValueError("RPY output is only supported for story/BSD inputs")
        if (
            kind is InputKind.STORY_BINARY
            and detect_script_version(source, request.options.asdis) == "v0"
        ):
            raise ValueError(
                "v0 story decompilation is not supported by bsd2c; use disassemble or text export"
            )
        if kind is InputKind.BSD and detect_bsd_version(source) == "v0":
            raise ValueError(
                "v0 BSD decompilation is not supported by bsd2c; its opcode semantics differ from bs5"
            )

        intermediate, artifacts = self._intermediate_source(
            request,
            source,
            kind,
            input_root,
            workspace,
        )
        if kind in {InputKind.STORY_BINARY, InputKind.BSD}:
            override = None if request.options.bsd_hint == "auto" else request.options.bsd_hint
            if request.input_path.is_file() and request.output_path is not None:
                destination = request.output_path
            else:
                result_preview = bsd_pipeline.decompile_file(intermediate, override)
                output_name = bsd_pipeline.derive_output_path_for_format(
                    Path(source.name),
                    result_preview.detected_type,
                    request.options.output_format,
                ).name
                destination = self._destination(request, source, input_root, output_name)
                result = result_preview
            if "result" not in locals():
                result = bsd_pipeline.decompile_file(
                    intermediate,
                    override,
                    definitions_override_path=destination.parent / "bsd2c_defs.json",
                )
            project_context = (
                bsd_pipeline.build_project_rpy_context([result])
                if request.options.output_format == "rpy"
                else None
            )
            written = bsd_pipeline.write_result(
                result,
                destination,
                output_format=request.options.output_format,
                project_context=project_context,
            )
            artifacts.append(Artifact(request.options.output_format, written, source))
            definitions = bsd_pipeline.write_definitions(
                result,
                written.parent / "bsd2c_defs.json",
            )
            artifacts.append(Artifact("definitions", definitions, source))
            if request.options.emit_ir:
                ir_path = bsd_pipeline.write_ir_result(
                    result,
                    bsd_pipeline.derive_ir_output_path(written),
                )
                artifacts.append(Artifact("ir", ir_path, source))
            return artifacts

        result = bpd_pipeline.decompile_file(
            intermediate,
            include_source_comments=request.options.include_source_comments,
            engine=request.options.asdis.bp_engine,
            games_path=request.options.asdis.bp_games,
            executable=request.options.asdis.bp_executable,
        )
        if result.assembly.engine_detection:
            self.log(result.assembly.engine_detection.describe())
        output_name = f"{source.name}.c"
        destination = self._destination(request, source, input_root, output_name)
        written = bpd_pipeline.write_result(result, destination)
        artifacts.append(Artifact("c", written, source))
        if request.options.emit_ir:
            ir_path = bpd_pipeline.write_ir_result(
                result,
                destination.with_suffix(".ir.txt"),
            )
            artifacts.append(Artifact("ir", ir_path, source))
        return artifacts

    def _text_output_name(self, source: Path, text_format: TextFormat) -> str:
        if source.suffix:
            return f"{source.stem}.{text_format.value}"
        return f"{source.name}.{text_format.value}"

    def _text_export(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
        workspace: Path,
    ) -> list[Artifact]:
        intermediate, artifacts = self._intermediate_source(
            request,
            source,
            kind,
            input_root,
            workspace,
        )
        destination = self._destination(
            request,
            source,
            input_root,
            self._text_output_name(source, request.text_format),
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        users = request.options.user_function_names
        if kind in {InputKind.STORY_BINARY, InputKind.BSD}:
            if request.text_format is TextFormat.JSON:
                count = extract_dialog_json_from_bsd(intermediate, destination, users)
            else:
                count, _ = extract_dialog_txt_from_bsd(intermediate, destination, users)
        elif request.text_format is TextFormat.JSON:
            count = extract_push_string_json_from_bpd(intermediate, destination)
        else:
            count, _ = extract_push_string_txt_from_bpd(intermediate, destination)
        self.log(f"Extracted {count} text entries")
        artifacts.append(Artifact(request.text_format.value, destination, source))
        if kind in {InputKind.STORY_BINARY, InputKind.BSD}:
            artifacts.append(Artifact("dialog-metadata", Path(str(destination) + ".meta.json"), source))
        return artifacts

    def _resolve_translation(
        self,
        request: TaskRequest,
        source: Path,
        input_root: Path,
    ) -> Path:
        translation = request.translation_path
        if translation is None:
            raise ValueError("Translation path is required for text import")
        if request.input_path.is_file():
            if not translation.is_file():
                raise FileNotFoundError(f"Translation file does not exist: {translation}")
            return translation
        if not translation.is_dir():
            raise NotADirectoryError(f"Translation directory does not exist: {translation}")
        relative = source.relative_to(input_root)
        candidate = translation / relative.parent / self._text_output_name(
            source,
            request.text_format,
        )
        if not candidate.is_file():
            raise FileNotFoundError(f"Translation file does not exist: {candidate}")
        return candidate

    @staticmethod
    def _translated_name(source: Path, kind: InputKind) -> str:
        if kind is InputKind.STORY_BINARY:
            return f"{source.name}_translated" if not source.suffix else f"{source.stem}_translated{source.suffix}"
        if kind is InputKind.BP_BINARY:
            return f"{source.stem}_translated._bp"
        if kind is InputKind.BSD:
            return f"{source.stem}_translated.bsd"
        return f"{source.stem}_translated.bpd"

    def _text_import(
        self,
        request: TaskRequest,
        source: Path,
        kind: InputKind,
        input_root: Path,
        workspace: Path,
    ) -> list[Artifact]:
        translation = self._resolve_translation(request, source, input_root)
        intermediate, artifacts = self._intermediate_source(
            request,
            source,
            kind,
            input_root,
            workspace,
        )
        relative_parent = source.relative_to(input_root).parent
        patched_suffix = ".bsd" if kind in {InputKind.STORY_BINARY, InputKind.BSD} else ".bpd"
        patched = workspace / relative_parent / f"{source.name}.translated{patched_suffix}"
        patched.parent.mkdir(parents=True, exist_ok=True)
        users = request.options.user_function_names
        if kind in {InputKind.STORY_BINARY, InputKind.BSD}:
            if request.text_format is TextFormat.JSON:
                _, applied = import_dialog_json_to_bsd(intermediate, translation, patched, users)
            else:
                _, applied, _ = import_dialog_txt_to_bsd(intermediate, translation, patched, users)
        elif request.text_format is TextFormat.JSON:
            _, applied = import_push_string_json_to_bpd(intermediate, translation, patched)
        else:
            _, applied, _ = import_push_string_txt_to_bpd(intermediate, translation, patched)
        self.log(f"Imported {applied} text entries")

        destination = self._destination(
            request,
            source,
            input_root,
            self._translated_name(source, kind),
        )
        if kind is InputKind.STORY_BINARY:
            version = detect_script_version(source, request.options.asdis)
            written, _ = assemble_story(
                patched,
                destination,
                request.options.asdis.__class__(
                    encoding=request.options.asdis.encoding,
                    fallback_encoding=request.options.asdis.fallback_encoding,
                    source_encoding=request.options.asdis.source_encoding,
                    source_fallback_encoding=request.options.asdis.source_fallback_encoding,
                    script_version=version,
                    exact=request.options.asdis.exact,
                ),
            )
            artifact_kind = "story-binary"
        elif kind is InputKind.BP_BINARY:
            written = assemble_bp(patched, destination, request.options.asdis)
            artifact_kind = "bp-binary"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(patched.read_bytes())
            written = destination
            artifact_kind = "bsd" if kind is InputKind.BSD else "bpd"
        artifacts.append(Artifact(artifact_kind, written, source))
        if request.options.keep_intermediate:
            artifacts.append(Artifact("intermediate-patched", patched, source))
        return artifacts


__all__ = ["ToolkitRunner"]
