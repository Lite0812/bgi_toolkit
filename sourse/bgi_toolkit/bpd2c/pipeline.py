from __future__ import annotations
from ..i18n import tr

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable, Iterable
from ..bp_engine import detect_engine

from .asm_parser import AssemblyModule, parse_assembly_file
from .emitter import emit_c
from .ir import Module
from .ir_text import format_module_ir
from .lifter import lift_module
from .project import (
    collect_cross_file_call_hints,
    resolve_cross_file_calls,
    scan_bpd_project,
)


ProgressCallback = Callable[[str], None]


@dataclass(slots=True)
class DecompileResult:
    source_path: Path
    assembly: AssemblyModule
    module: Module
    c_source: str

    @property
    def instruction_count(self) -> int:
        return self.assembly.instruction_count

    @property
    def label_count(self) -> int:
        return self.assembly.label_count


def decompile_file(
    path: str | Path,
    *,
    include_source_comments: bool = True,
    dynamic_call_hints_by_line: dict[int, tuple[int, int]] | None = None,
    recover_named_script_calls: bool = True,
    engine='auto',
    games_path=None,
    executable=None,
) -> DecompileResult:
    source_path = Path(path)
    assembly = parse_assembly_file(source_path, engine=engine, games_path=games_path, executable=executable)
    module = lift_module(
        assembly,
        dynamic_call_hints_by_line,
        recover_named_script_calls=recover_named_script_calls,
    )
    return DecompileResult(
        source_path,
        assembly,
        module,
        emit_c(module, include_source_comments=include_source_comments),
    )


def default_batch_worker_count(file_count: int) -> int:
    if file_count <= 1:
        return 1
    cpu_count = os.cpu_count() or 1
    return min(file_count, 8, max(1, cpu_count - 1))


def _decompile_worker(
    path: str,
    include_source_comments: bool,
    dynamic_call_hints_by_line: dict[int, tuple[int, int]] | None = None,
    recover_named_script_calls: bool = True,
    engine='auto',
    games_path=None,
    executable=None,
) -> DecompileResult:
    return decompile_file(
        path,
        include_source_comments=include_source_comments,
        dynamic_call_hints_by_line=dynamic_call_hints_by_line,
        recover_named_script_calls=recover_named_script_calls,
        engine=engine, games_path=games_path, executable=executable,
    )


def decompile_files(
    paths: Iterable[str | Path],
    progress: ProgressCallback | None = None,
    *,
    include_source_comments: bool = True,
    workers: int | None = None,
    resolve_project_calls: bool = True,
    engine='auto',
    games_path=None,
    executable=None,
) -> list[DecompileResult]:
    ordered_paths = [Path(path) for path in paths]
    if not ordered_paths:
        return []
    # A project signature from one VM ABI must not overwrite another engine's
    # argument or result contract when a directory contains several games.
    by_profile = {}
    detections = {}
    for path in ordered_paths:
        detection = detect_engine(path, engine, games_path=games_path, executable=executable)
        detections[path] = detection
        profile = detection.profile
        by_profile.setdefault(profile, []).append(path)
    if len(by_profile) > 1:
        combined = {}
        for profile, group in by_profile.items():
            if progress:
                progress(tr('BP 引擎分组：{0}，{1} 个文件', profile.name, len(group)))
            for result in decompile_files(group, progress, include_source_comments=include_source_comments,
                    workers=workers, resolve_project_calls=resolve_project_calls, engine=profile):
                result.assembly.engine_detection = detections[result.source_path]
                combined[result.source_path] = result
        return [combined[path] for path in ordered_paths]
    engine = next(iter(by_profile))
    if progress:
        progress(detections[ordered_paths[0]].describe())
    worker_count = default_batch_worker_count(len(ordered_paths)) if workers is None else max(1, workers)
    worker_count = min(worker_count, len(ordered_paths))
    results: list[DecompileResult | None] = [None] * len(ordered_paths)
    use_project_signatures = resolve_project_calls and len(ordered_paths) > 1

    if worker_count == 1:
        for index, source_path in enumerate(ordered_paths):
            if progress is not None:
                progress(f"[{index + 1}/{len(ordered_paths)}] {source_path.name}")
            results[index] = decompile_file(
                source_path,
                include_source_comments=include_source_comments,
                recover_named_script_calls=not use_project_signatures,
                engine=engine,
            )
    else:
        if progress is not None:
            progress(tr('并行反编译: {0} 个工作进程', worker_count))
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            pending = {
                executor.submit(
                    _decompile_worker,
                    str(source_path),
                    include_source_comments,
                    None,
                    not use_project_signatures,
                    engine,
                ): index
                for index, source_path in enumerate(ordered_paths)
            }
            completed = 0
            for future in as_completed(pending):
                index = pending[future]
                results[index] = future.result()
                completed += 1
                if progress is not None:
                    progress(f"[{completed}/{len(ordered_paths)}] {ordered_paths[index].name}")

    completed_results = [result for result in results if result is not None]
    if resolve_project_calls and len(completed_results) > 1:
        if progress is not None:
            progress(tr('前置扫描项目分派表与跨文件函数签名'))
        project = scan_bpd_project(completed_results)
        hints = collect_cross_file_call_hints(completed_results, project)
        if hints:
            if progress is not None:
                progress(tr('使用前置扫描结果重新提升 {0} 个跨文件调用模块', len(hints)))
            hinted_indices = {
                result.source_path: index
                for index, result in enumerate(completed_results)
                if result.source_path in hints
            }
            if worker_count == 1:
                for source_path, index in hinted_indices.items():
                    completed_results[index] = decompile_file(
                        source_path,
                        include_source_comments=include_source_comments,
                        dynamic_call_hints_by_line=hints[source_path],
                        recover_named_script_calls=False,
                        engine=engine,
                    )
            else:
                with ProcessPoolExecutor(max_workers=min(worker_count, len(hinted_indices))) as executor:
                    pending = {
                        executor.submit(
                            _decompile_worker,
                            str(source_path),
                            include_source_comments,
                            hints[source_path],
                            False,
                            engine,
                        ): index
                        for source_path, index in hinted_indices.items()
                    }
                    for future in as_completed(pending):
                        completed_results[pending[future]] = future.result()
            project = scan_bpd_project(completed_results)
        resolved = resolve_cross_file_calls(completed_results, project)
        if progress is not None:
            progress(tr('已解析 {0} 处跨文件 BPD 调用', resolved))
    for result in completed_results:
        result.assembly.engine_detection = detections[result.source_path]
    return completed_results


def iter_input_files(target: str | Path, recursive: bool = True) -> list[Path]:
    target_path = Path(target)
    if target_path.is_file():
        return [target_path] if target_path.suffix.lower() == ".bpd" else []
    if not target_path.is_dir():
        return []
    iterator = target_path.rglob("*") if recursive else target_path.glob("*")
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() == ".bpd")


def derive_output_path(source_path: str | Path) -> Path:
    source = Path(source_path)
    return source.with_name(source.name + ".c")


def derive_ir_output_path(source_path: str | Path) -> Path:
    source = Path(source_path)
    return source.with_name(source.name + ".ir.txt")


def write_result(result: DecompileResult, destination: str | Path | None = None) -> Path:
    output_path = Path(destination) if destination is not None else derive_output_path(result.source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.c_source, encoding="utf-8", newline="\n")
    return output_path


def write_ir_result(result: DecompileResult, destination: str | Path | None = None) -> Path:
    output_path = Path(destination) if destination is not None else derive_ir_output_path(result.source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(format_module_ir(result.module), encoding="utf-8", newline="\n")
    return output_path


def output_path_for_batch(source: Path, input_root: Path, output_root: Path, ir: bool = False) -> Path:
    relative = source.relative_to(input_root)
    suffix = ".ir.txt" if ir else ".c"
    return output_root / relative.with_name(relative.name + suffix)


def decompile_target(
    target: str | Path,
    output_dir: str | Path,
    *,
    ir_dir: str | Path | None = None,
    recursive: bool = True,
    progress: ProgressCallback | None = None,
    include_source_comments: bool = True,
    workers: int | None = None,
    engine='auto',
    games_path=None,
    executable=None,
) -> list[tuple[DecompileResult, Path, Path | None]]:
    target_path = Path(target)
    inputs = iter_input_files(target_path, recursive=recursive)
    if not inputs:
        raise ValueError(tr('没有找到 BPD 文件: {0}', target_path))

    output_root = Path(output_dir)
    ir_root = Path(ir_dir) if ir_dir is not None else None
    input_root = target_path if target_path.is_dir() else target_path.parent
    results = decompile_files(
        inputs,
        progress=progress,
        include_source_comments=include_source_comments,
        workers=workers,
        resolve_project_calls=True,
        engine=engine, games_path=games_path, executable=executable,
    )
    written: list[tuple[DecompileResult, Path, Path | None]] = []

    for source, result in zip(inputs, results, strict=True):
        c_path = output_path_for_batch(source, input_root, output_root)
        write_result(result, c_path)
        ir_path: Path | None = None
        if ir_root is not None:
            ir_path = output_path_for_batch(source, input_root, ir_root, ir=True)
            write_ir_result(result, ir_path)
        written.append((result, c_path, ir_path))
    return written
