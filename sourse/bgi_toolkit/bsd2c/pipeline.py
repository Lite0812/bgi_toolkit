from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from dataclasses import dataclass
import multiprocessing
import os
from pathlib import Path
import re
import sys
from typing import Callable, Iterable, Literal

from .builtin_functions import builtin_resolver_scope
from .asm_parser import AssemblyModule, parse_assembly_file
from .definitions import (
    ProjectDefinitions,
    build_project_definitions,
    derive_definitions_path,
    find_file_definitions,
    load_project_definitions,
    make_script_signature_map,
    merge_project_definitions,
    normalize_file_definition_key,
    write_project_definitions,
)
from .emitter import emit_module as emit_c_module
from .ignore_config import IgnoreBuiltinConfig
from .ir import Module
from .ir_text import format_module_ir
from .lifter import lift_module
from .rpy_emitter import (
    RpyEmitOptions,
    RpyProjectScript,
    build_rpy_project_context,
    derive_script_label,
    derive_script_name,
    emit_module as emit_rpy_module,
    normalize_script_stem,
)
from .signatures import Signature, SignatureLibrary, load_builtin_signature_library


OutputFormat = Literal["c", "rpy"]
emit_module = emit_c_module
IMPORT_DIRECTIVE_RE = re.compile(r'^[ \t]*#import\s+"(?P<name>[^"]+)"', re.MULTILINE)


@dataclass(slots=True)
class DecompileResult:
    source_path: Path
    detected_type: str
    output_path: Path
    definitions_path: Path
    c_source: str
    module: Module
    script_signatures: dict[str, Signature]
    project_definitions: ProjectDefinitions


@dataclass(slots=True)
class DefinitionScanResult:
    definitions_path: Path
    project_definitions: ProjectDefinitions
    source_paths: tuple[Path, ...]


@dataclass(slots=True)
class TextReplacementRule:
    find: str
    replace: str


def apply_text_replacements(text: str, rules: Iterable[TextReplacementRule] | None = None) -> str:
    if not rules:
        return text
    for rule in rules:
        if not rule.find:
            continue
        text = text.replace(rule.find, rule.replace)
    return text


def derive_output_path(source_path: Path, detected_type: str) -> Path:
    return derive_output_path_for_format(source_path, detected_type, "c")


def derive_output_path_for_format(
    source_path: Path,
    detected_type: str,
    output_format: OutputFormat = "c",
    rpy_emit_options: RpyEmitOptions | None = None,
) -> Path:
    if output_format == "rpy":
        if source_path.suffix == ".txt" and source_path.name.endswith(".bsd.txt"):
            destination = source_path.with_suffix("").with_suffix(".rpy")
        else:
            destination = source_path.with_suffix(".rpy")
        normalized_stem = normalize_script_stem(destination.stem, rpy_emit_options)
        return destination.with_name(f"{normalized_stem}{destination.suffix}")

    if detected_type == "bss":
        suffix = ".bss.c"
    elif detected_type == "bsb":
        suffix = ".bsb.c"
    else:
        suffix = f".{detected_type}.c"

    if source_path.suffix == ".txt" and source_path.name.endswith(".bsd.txt"):
        return source_path.with_suffix("").with_suffix(suffix)
    return source_path.with_suffix(suffix)


def iter_input_files(target: str | Path) -> Iterable[Path]:
    path = Path(target)
    if path.is_file():
        yield path
        return

    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.suffix == ".bsd" or candidate.name.endswith(".bsd.txt"):
            yield candidate


def _group_source_paths_by_definitions(
    ordered_paths: list[Path],
    definitions_override_path: str | Path | None = None,
) -> dict[Path, list[tuple[int, Path]]]:
    grouped_paths: dict[Path, list[tuple[int, Path]]] = defaultdict(list)
    if definitions_override_path is not None:
        override_path = Path(definitions_override_path).resolve()
        for index, path in enumerate(ordered_paths):
            grouped_paths[override_path].append((index, path))
        return grouped_paths

    for index, path in enumerate(ordered_paths):
        grouped_paths[derive_definitions_path(path)].append((index, path))
    return grouped_paths


def _build_effective_signature_library(
    builtin_library: SignatureLibrary,
    project_definitions: ProjectDefinitions,
) -> SignatureLibrary:
    if not project_definitions.external_signatures:
        return builtin_library
    merged_overrides: list[Signature] = []
    for signature in project_definitions.external_signatures.values():
        builtin_signature = builtin_library.get(signature.name)
        merged_aliases = tuple(dict.fromkeys((*((builtin_signature.aliases) if builtin_signature is not None else ()), *signature.aliases)))
        merged_overrides.append(
            Signature(
                name=signature.name,
                return_type=signature.return_type,
                params=signature.params,
                aliases=merged_aliases,
                source=signature.source,
                variadic=signature.variadic,
            )
        )
    return SignatureLibrary(
        [
            *builtin_library.signatures(),
            *merged_overrides,
        ]
    )


def _build_seed_user_signatures(project_definitions: ProjectDefinitions) -> dict[str, Signature]:
    seeded = dict(project_definitions.external_signatures)
    seeded.update(project_definitions.user_signatures)
    return seeded

def resolve_import_path(source_path: Path, import_name: str) -> Path | None:
    search_roots = [source_path.parent]
    base_name = import_name.split("/")[-1].split("\\")[-1]
    candidate_bases = [base_name]
    stem, _, suffix = base_name.rpartition(".")
    if stem and suffix.startswith("_"):
        candidate_bases.append(stem)

    candidates: list[str] = []
    for candidate_base in dict.fromkeys(candidate_bases):
        candidates.extend((f"{candidate_base}.bsd.txt", f"{candidate_base}.bsd"))

    for root in search_roots:
        for candidate_name in candidates:
            candidate = root / candidate_name
            if candidate.exists():
                return candidate
    return None


def load_assembly_module(source_path: Path, parsed_modules: dict[Path, AssemblyModule]) -> AssemblyModule:
    path = source_path.resolve()
    cached = parsed_modules.get(path)
    if cached is not None:
        return cached
    parsed = parse_assembly_file(path)
    parsed_modules[path] = parsed
    return parsed


def build_decompile_result(
    source_path: Path,
    detected_type: str,
    module: Module,
    include_c_source: bool = True,
) -> DecompileResult:
    return DecompileResult(
        source_path=source_path,
        detected_type=detected_type,
        output_path=derive_output_path(source_path, detected_type),
        definitions_path=derive_definitions_path(source_path),
        c_source=emit_c_module(module) if include_c_source else "",
        module=module,
        script_signatures=make_script_signature_map(module),
        project_definitions=ProjectDefinitions(),
    )


def decompile_module_recursive(
    source_path: Path,
    override: str | None,
    cache: dict[Path, DecompileResult],
    visiting: set[Path],
    parsed_modules: dict[Path, AssemblyModule],
    signature_library,
    project_script_signatures: dict[str, Signature],
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    emit_c_source: bool = True,
    optimize: bool | None = None,
) -> DecompileResult:
    path = source_path.resolve()
    if path in cache:
        cached = cache[path]
        if emit_c_source and not cached.c_source:
            cached.c_source = emit_c_module(cached.module)
        return cached

    if optimize is None:
        optimize = emit_c_source

    asm_module = load_assembly_module(path, parsed_modules)
    detected_type = override if override and override != "auto" else asm_module.source_type

    if path in visiting:
        module = lift_module(
            asm_module,
            detected_type,
            ignore_builtin_config=ignore_builtin_config,
            optimize=optimize,
        )
        result = build_decompile_result(path, detected_type, module, include_c_source=emit_c_source)
        cache[path] = result
        return result

    visiting.add(path)

    external_script_signatures: dict[str, Signature] = dict(project_script_signatures)
    for import_name in asm_module.imports:
        import_path = resolve_import_path(path, import_name)
        if import_path is None:
            continue
        imported = decompile_module_recursive(
            import_path,
            override,
            cache,
            visiting,
            parsed_modules,
            signature_library,
            project_script_signatures,
            ignore_builtin_config=ignore_builtin_config,
            emit_c_source=False,
            optimize=optimize,
        )
        for name, signature in imported.script_signatures.items():
            external_script_signatures.setdefault(name, signature)

    # Cyclic imports can create a provisional result while the module is
    # already being visited. Reusing it avoids lifting the same module twice
    # in one inference pass; a later pass will refine its signatures.
    cached_cycle_result = cache.get(path)
    if cached_cycle_result is not None:
        visiting.remove(path)
        if emit_c_source and not cached_cycle_result.c_source:
            cached_cycle_result.c_source = emit_c_module(cached_cycle_result.module)
        return cached_cycle_result

    module = lift_module(
        asm_module,
        detected_type,
        external_script_signatures,
        signature_library,
        ignore_builtin_config=ignore_builtin_config,
        optimize=optimize,
    )
    result = build_decompile_result(path, detected_type, module, include_c_source=emit_c_source)
    cache[path] = result
    visiting.remove(path)
    return result


def _run_signature_inference_pass(
    ordered_paths: list[Path],
    override: str | None,
    signature_library,
    known_user_signatures: dict[str, Signature],
    parsed_modules: dict[Path, AssemblyModule],
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    pass_index: int,
    progress_callback: Callable[[int, int, int, Path], None] | None,
) -> tuple[dict[str, Signature], dict[Path, DecompileResult]]:
    cache: dict[Path, DecompileResult] = {}
    visiting: set[Path] = set()
    total = len(ordered_paths)
    for index, path in enumerate(ordered_paths, start=1):
        decompile_module_recursive(
            path,
            override,
            cache,
            visiting,
            parsed_modules,
            signature_library,
            known_user_signatures,
            ignore_builtin_config=ignore_builtin_config,
            emit_c_source=False,
        )
        if progress_callback is not None:
            progress_callback(pass_index, index, total, path)

    next_user_signatures = dict(known_user_signatures)
    for result in cache.values():
        next_user_signatures.update(result.script_signatures)
    return next_user_signatures, cache


def _infer_project_user_signatures(
    source_paths: Iterable[str | Path],
    override: str | None,
    signature_library,
    seed_user_signatures: dict[str, Signature] | None = None,
    parsed_modules: dict[Path, AssemblyModule] | None = None,
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    max_passes: int = 3,
    progress_callback: Callable[[int, int, int, Path], None] | None = None,
) -> tuple[dict[str, Signature], dict[Path, DecompileResult] | None]:
    ordered_paths = [Path(source_path) for source_path in source_paths]
    known_user_signatures = dict(seed_user_signatures or {})
    parsed_modules = parsed_modules if parsed_modules is not None else {}

    if not ordered_paths:
        return known_user_signatures, {}

    stable_cache: dict[Path, DecompileResult] | None = None
    for pass_index in range(max_passes):
        next_user_signatures, cache = _run_signature_inference_pass(
            ordered_paths,
            override,
            signature_library,
            known_user_signatures,
            parsed_modules,
            ignore_builtin_config,
            pass_index + 1,
            progress_callback,
        )

        if next_user_signatures == known_user_signatures:
            stable_cache = cache
            break
        known_user_signatures = next_user_signatures

    return known_user_signatures, stable_cache


def infer_project_user_signatures(
    source_paths: Iterable[str | Path],
    override: str | None,
    signature_library,
    seed_user_signatures: dict[str, Signature] | None = None,
    parsed_modules: dict[Path, AssemblyModule] | None = None,
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    max_passes: int = 3,
    progress_callback: Callable[[int, int, int, Path], None] | None = None,
) -> dict[str, Signature]:
    signatures, _ = _infer_project_user_signatures(
        source_paths,
        override,
        signature_library,
        seed_user_signatures,
        parsed_modules,
        ignore_builtin_config,
        max_passes,
        progress_callback,
    )
    return signatures


def _definitions_cover_sources(
    project_definitions: ProjectDefinitions,
    source_paths: Iterable[Path],
) -> bool:
    generated = {name.lower() for name in project_definitions.generated_from}
    return bool(generated) and all(path.name.lower() in generated for path in source_paths)


def _decompile_requested_module(
    path: Path,
    override: str | None,
    parsed_modules: dict[Path, AssemblyModule],
    signature_library,
    known_user_signatures: dict[str, Signature],
    ignore_builtin_config: IgnoreBuiltinConfig | None,
) -> DecompileResult:
    resolved = path.resolve()
    asm_module = load_assembly_module(resolved, parsed_modules)
    detected_type = override if override and override != "auto" else asm_module.source_type
    module = lift_module(
        asm_module,
        detected_type,
        known_user_signatures,
        signature_library,
        ignore_builtin_config=ignore_builtin_config,
        optimize=True,
    )
    return build_decompile_result(resolved, detected_type, module, include_c_source=True)


@builtin_resolver_scope()
def _decompile_requested_worker(args) -> DecompileResult:
    (
        path,
        override,
        signature_library,
        known_user_signatures,
        ignore_builtin_config,
    ) = args
    return _decompile_requested_module(
        path,
        override,
        {},
        signature_library,
        known_user_signatures,
        ignore_builtin_config,
    )


@builtin_resolver_scope()
def _infer_requested_worker(args) -> dict[str, Signature]:
    (
        path,
        override,
        signature_library,
        seed_user_signatures,
        ignore_builtin_config,
    ) = args
    asm_module = parse_assembly_file(path)
    detected_type = override if override and override != "auto" else asm_module.source_type
    module = lift_module(
        asm_module,
        detected_type,
        seed_user_signatures,
        signature_library,
        ignore_builtin_config=ignore_builtin_config,
        optimize=False,
    )
    return make_script_signature_map(module)


def _parallel_worker_count(paths: list[Path]) -> int:
    if multiprocessing.current_process().name != "MainProcess" or len(paths) < 4:
        return 1
    main_module = sys.modules.get("__main__")
    main_file = getattr(main_module, "__file__", None)
    if not getattr(sys, "frozen", False) and (
        not main_file or str(main_file).startswith("<")
    ):
        return 1
    total_size = sum(path.stat().st_size for path in paths)
    if total_size < 2_000_000:
        return 1
    # Keep batch decompilation responsive on machines where the source set is large.
    cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    return min(4, len(paths), max(1, cpu_count - 1))


def _imports_are_inside_group(paths: list[Path]) -> bool:
    grouped = {path.resolve() for path in paths}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in IMPORT_DIRECTIVE_RE.finditer(text):
            imported = resolve_import_path(path, match.group("name"))
            if imported is not None and imported.resolve() not in grouped:
                return False
    return True


def _load_import_graph(
    source_paths: Iterable[Path],
    parsed_modules: dict[Path, AssemblyModule],
) -> tuple[list[Path], dict[Path, tuple[Path, ...]]]:
    ordered: list[Path] = []
    graph: dict[Path, tuple[Path, ...]] = {}
    pending = [path.resolve() for path in reversed(list(source_paths))]
    queued = set(pending)
    while pending:
        path = pending.pop()
        if path in graph:
            continue
        module = load_assembly_module(path, parsed_modules)
        imported_paths: list[Path] = []
        for import_name in module.imports:
            imported_path = resolve_import_path(path, import_name)
            if imported_path is None:
                continue
            resolved_import = imported_path.resolve()
            imported_paths.append(resolved_import)
            if resolved_import not in graph and resolved_import not in queued:
                pending.append(resolved_import)
                queued.add(resolved_import)
        graph[path] = tuple(imported_paths)
        ordered.append(path)
    return ordered, graph


def _build_source_definition_keys(
    paths: Iterable[Path],
    project_definitions: ProjectDefinitions,
) -> dict[Path, str]:
    resolved_paths = list(dict.fromkeys(path.resolve() for path in paths))
    keys: dict[Path, str] = {}
    unmatched: list[Path] = []
    for path in resolved_paths:
        existing = find_file_definitions(project_definitions, path)
        if existing is None:
            unmatched.append(path)
        else:
            keys[path] = existing[0]

    basename_counts: dict[str, int] = defaultdict(int)
    for path in unmatched:
        basename_counts[path.name.lower()] += 1
    try:
        common_root = Path(os.path.commonpath([str(path.parent) for path in resolved_paths]))
    except ValueError:
        common_root = None
    for path in unmatched:
        if basename_counts[path.name.lower()] == 1:
            key = path.name
        elif common_root is not None:
            key = path.relative_to(common_root).as_posix()
        else:
            key = path.as_posix()
        keys[path] = normalize_file_definition_key(key)
    return keys


def _seed_local_user_signatures(
    path: Path,
    source_key: str,
    project_definitions: ProjectDefinitions,
) -> dict[str, Signature]:
    if project_definitions.files:
        matched = find_file_definitions(project_definitions, path, source_key)
        return dict(matched[1].user_signatures) if matched is not None else {}
    return dict(project_definitions.user_signatures)


def _visible_user_signatures(
    path: Path,
    import_graph: dict[Path, tuple[Path, ...]],
    local_signatures: dict[Path, dict[str, Signature]],
) -> dict[str, Signature]:
    visible: dict[str, Signature] = {}
    visited: set[Path] = set()

    def visit(current: Path) -> None:
        if current in visited:
            return
        visited.add(current)
        for imported in import_graph.get(current, ()):
            visit(imported)
        visible.update(local_signatures.get(current, {}))

    visit(path.resolve())
    return visible


def _with_global_external_signatures(
    project_definitions: ProjectDefinitions,
    user_signatures: dict[str, Signature],
) -> dict[str, Signature]:
    signatures = dict(project_definitions.external_signatures)
    signatures.update(user_signatures)
    return signatures


def _infer_local_signatures(
    paths: list[Path],
    override: str | None,
    signature_library: SignatureLibrary,
    seed_signatures: dict[Path, dict[str, Signature]],
    parsed_modules: dict[Path, AssemblyModule],
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    progress_callback: Callable[[int, int, int, Path], None] | None,
) -> dict[Path, dict[str, Signature]]:
    worker_count = _parallel_worker_count(paths)
    total = len(paths)
    if worker_count > 1:
        payloads = [
            (
                path,
                override,
                signature_library,
                seed_signatures[path],
                ignore_builtin_config,
            )
            for path in paths
        ]
        inferred: dict[Path, dict[str, Signature]] = {}
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            completed = executor.map(_infer_requested_worker, payloads, chunksize=1)
            for index, (path, signatures) in enumerate(
                zip(paths, completed, strict=True),
                start=1,
            ):
                inferred[path] = signatures
                if progress_callback is not None:
                    progress_callback(1, index, total, path)
        return inferred

    inferred = {}
    for index, path in enumerate(paths, start=1):
        asm_module = load_assembly_module(path, parsed_modules)
        detected_type = override if override and override != "auto" else asm_module.source_type
        module = lift_module(
            asm_module,
            detected_type,
            seed_signatures[path],
            signature_library,
            ignore_builtin_config=ignore_builtin_config,
            optimize=False,
        )
        inferred[path] = make_script_signature_map(module)
        if progress_callback is not None:
            progress_callback(1, index, total, path)
    return inferred


def _decompile_scoped_group(
    group: list[tuple[int, Path]],
    definitions_path: Path,
    override: str | None,
    builtin_library: SignatureLibrary,
    parsed_modules: dict[Path, AssemblyModule],
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    inference_progress_callback: Callable[[int, int, int, Path], None] | None,
) -> tuple[dict[int, DecompileResult], ProjectDefinitions]:
    project_definitions = load_project_definitions(definitions_path)
    signature_library = _build_effective_signature_library(builtin_library, project_definitions)
    requested_paths = [path.resolve() for _, path in group]
    all_paths, import_graph = _load_import_graph(requested_paths, parsed_modules)
    source_keys = _build_source_definition_keys(all_paths, project_definitions)
    stored_local_signatures = {
        path: _seed_local_user_signatures(path, source_keys[path], project_definitions)
        for path in all_paths
    }

    definitions_cover_graph = bool(project_definitions.files) and all(
        find_file_definitions(project_definitions, path, source_keys[path]) is not None
        for path in all_paths
    )
    if definitions_cover_graph:
        local_signatures = stored_local_signatures
    else:
        initial_seeds = {
            path: _with_global_external_signatures(
                project_definitions,
                _visible_user_signatures(path, import_graph, stored_local_signatures),
            )
            for path in all_paths
        }
        local_signatures = _infer_local_signatures(
            all_paths,
            override,
            signature_library,
            initial_seeds,
            parsed_modules,
            ignore_builtin_config,
            inference_progress_callback,
        )

    visible_signatures = {
        path: _visible_user_signatures(path, import_graph, local_signatures)
        for path in all_paths
    }
    final_seeds = {
        path: _with_global_external_signatures(project_definitions, visible_signatures[path])
        for path in all_paths
    }
    worker_count = _parallel_worker_count(all_paths)
    if worker_count > 1:
        payloads = [
            (
                path,
                override,
                signature_library,
                final_seeds[path],
                ignore_builtin_config,
            )
            for path in all_paths
        ]
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            completed = executor.map(_decompile_requested_worker, payloads, chunksize=1)
            results_by_path = {
                path: result
                for path, result in zip(all_paths, completed, strict=True)
            }
    else:
        results_by_path = {
            path: _decompile_requested_module(
                path,
                override,
                parsed_modules,
                signature_library,
                final_seeds[path],
                ignore_builtin_config,
            )
            for path in all_paths
        }

    final_local_signatures = {
        path: result.script_signatures
        for path, result in results_by_path.items()
    }
    final_visible_signatures = {
        path: _visible_user_signatures(path, import_graph, final_local_signatures)
        for path in all_paths
    }
    aggregated_definitions = build_project_definitions(
        (
            (result.source_path, result.module)
            for result in results_by_path.values()
        ),
        signature_library,
        seed_definitions=project_definitions,
        source_keys=source_keys,
        visible_user_signatures=final_visible_signatures,
    )
    indexed_results: dict[int, DecompileResult] = {}
    for index, requested_path in group:
        result = results_by_path[requested_path.resolve()]
        result.project_definitions = aggregated_definitions
        result.definitions_path = definitions_path
        indexed_results[index] = result
    return indexed_results, aggregated_definitions


def _parallel_infer_and_decompile(
    paths: list[Path],
    override: str | None,
    signature_library,
    seed_user_signatures: dict[str, Signature],
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    worker_count: int,
    progress_callback: Callable[[int, int, int, Path], None] | None,
) -> tuple[dict[str, Signature], dict[Path, DecompileResult]]:
    inference_payloads = [
        (
            path,
            override,
            signature_library,
            seed_user_signatures,
            ignore_builtin_config,
        )
        for path in paths
    ]
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        signature_maps = executor.map(_infer_requested_worker, inference_payloads, chunksize=1)
        known_user_signatures = dict(seed_user_signatures)
        total = len(paths)
        for index, (path, signatures) in enumerate(
            zip(paths, signature_maps, strict=True),
            start=1,
        ):
            known_user_signatures.update(signatures)
            if progress_callback is not None:
                progress_callback(1, index, total, path)

        final_payloads = [
            (
                path,
                override,
                signature_library,
                known_user_signatures,
                ignore_builtin_config,
            )
            for path in paths
        ]
        results = executor.map(_decompile_requested_worker, final_payloads, chunksize=1)
        completed = {
            path.resolve(): result
            for path, result in zip(paths, results, strict=True)
        }
    return known_user_signatures, completed


@builtin_resolver_scope()
def scan_definition_bundles(
    source_paths: Iterable[str | Path],
    override: str | None = None,
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    inference_progress_callback: Callable[[int, int, int, Path], None] | None = None,
    definitions_override_path: str | Path | None = None,
) -> list[DefinitionScanResult]:
    ordered_paths = [Path(source_path) for source_path in source_paths]
    if not ordered_paths:
        return []

    grouped_paths = _group_source_paths_by_definitions(ordered_paths, definitions_override_path)
    builtin_library = load_builtin_signature_library()
    parsed_modules: dict[Path, AssemblyModule] = {}
    scan_results: list[DefinitionScanResult] = []

    for definitions_path, group in grouped_paths.items():
        _, aggregated_definitions = _decompile_scoped_group(
            group,
            definitions_path,
            override,
            builtin_library,
            parsed_modules,
            ignore_builtin_config,
            inference_progress_callback,
        )
        scan_results.append(
            DefinitionScanResult(
                definitions_path=definitions_path,
                project_definitions=aggregated_definitions,
                source_paths=tuple(path for _, path in group),
            )
        )

    return scan_results


@builtin_resolver_scope()
def decompile_files(
    source_paths: Iterable[str | Path],
    override: str | None = None,
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    inference_progress_callback: Callable[[int, int, int, Path], None] | None = None,
    definitions_override_path: str | Path | None = None,
) -> list[DecompileResult]:
    ordered_paths = [Path(source_path) for source_path in source_paths]
    if not ordered_paths:
        return []

    grouped_paths = _group_source_paths_by_definitions(ordered_paths, definitions_override_path)

    ordered_results: list[DecompileResult | None] = [None] * len(ordered_paths)
    builtin_library = load_builtin_signature_library()
    parsed_modules: dict[Path, AssemblyModule] = {}

    for definitions_path, group in grouped_paths.items():
        indexed_results, _ = _decompile_scoped_group(
            group,
            definitions_path,
            override,
            builtin_library,
            parsed_modules,
            ignore_builtin_config,
            inference_progress_callback,
        )
        for index, result in indexed_results.items():
            ordered_results[index] = result

    return [result for result in ordered_results if result is not None]


def decompile_file(
    source_path: str | Path,
    override: str | None = None,
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    inference_progress_callback: Callable[[int, int, int, Path], None] | None = None,
    definitions_override_path: str | Path | None = None,
) -> DecompileResult:
    return decompile_files(
        [source_path],
        override,
        ignore_builtin_config=ignore_builtin_config,
        inference_progress_callback=inference_progress_callback,
        definitions_override_path=definitions_override_path,
    )[0]


def derive_ir_output_path(output_path: Path) -> Path:
    return output_path.with_suffix(".ir.txt")


def write_ir_result(result: DecompileResult, output_path: str | Path | None = None) -> Path:
    destination = Path(output_path) if output_path else derive_ir_output_path(result.output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(format_module_ir(result.module), encoding="utf-8")
    return destination


def build_project_rpy_context(
    results: Iterable[DecompileResult],
    rpy_emit_options: RpyEmitOptions | None = None,
):
    return build_rpy_project_context(
        [
            RpyProjectScript(
                script_name=derive_script_name(result.source_path.name),
                script_label=derive_script_label(result.source_path.name, rpy_emit_options),
                module=result.module,
            )
            for result in results
        ]
    )


@builtin_resolver_scope()
def render_result(
    result: DecompileResult,
    output_format: OutputFormat = "c",
    project_context=None,
    rpy_emit_options: RpyEmitOptions | None = None,
    replacement_rules: Iterable[TextReplacementRule] | None = None,
) -> str:
    if output_format == "c":
        rendered = result.c_source
    elif output_format == "rpy":
        rendered = emit_rpy_module(
            result.module,
            script_label=derive_script_label(result.source_path.name, rpy_emit_options),
            script_name=derive_script_name(result.source_path.name),
            project_context=project_context,
            emit_options=rpy_emit_options,
        )
    else:
        raise ValueError(f"Unsupported output format: {output_format}")
    return apply_text_replacements(rendered, replacement_rules)


def write_result(
    result: DecompileResult,
    output_path: str | Path | None = None,
    output_format: OutputFormat = "c",
    project_context=None,
    rpy_emit_options: RpyEmitOptions | None = None,
    replacement_rules: Iterable[TextReplacementRule] | None = None,
) -> Path:
    destination = (
        Path(output_path)
        if output_path
        else derive_output_path_for_format(result.source_path, result.detected_type, output_format, rpy_emit_options)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_result(
            result,
            output_format,
            project_context=project_context,
            rpy_emit_options=rpy_emit_options,
            replacement_rules=replacement_rules,
        ),
        encoding="utf-8",
    )
    return destination


def write_definitions(result: DecompileResult, output_path: str | Path | None = None) -> Path:
    destination = Path(output_path) if output_path else result.definitions_path
    return write_project_definitions(result.project_definitions, destination)


def write_definition_bundle(results: Iterable[DecompileResult], output_path: str | Path | None = None) -> Path:
    results = list(results)
    if not results:
        raise ValueError("results must not be empty")
    destination = Path(output_path) if output_path else results[0].definitions_path
    bundled = merge_project_definitions(result.project_definitions for result in results)
    return write_project_definitions(bundled, destination)
