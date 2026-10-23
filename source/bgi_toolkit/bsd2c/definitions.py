from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
import json
from pathlib import Path
import re
from typing import Iterable

from .ir import (
    AddressOf,
    AssignStmt,
    BinaryExpr,
    BranchStmt,
    CallExpr,
    DerefExpr,
    Expr,
    ExprStmt,
    Function,
    IntLiteral,
    Module,
    ReturnStmt,
    SetBaseStmt,
    StringLiteral,
    UnaryExpr,
)
from .signatures import NO_DEFAULT, ParamSpec, Signature, SignatureLibrary


DEFINITIONS_FILENAME = "bsd2c_defs.json"
DEFINITIONS_VERSION = 4
PROTOTYPE_RE = re.compile(r"^\s*(?P<return_type>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_:]*)\s*\((?P<params>.*)\)\s*$")
PARAM_RE = re.compile(r"^\s*(?P<type_name>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*$")


@dataclass(slots=True)
class FileDefinitions:
    imports: list[str] = field(default_factory=list)
    user_signatures: dict[str, Signature] = field(default_factory=dict)


@dataclass(slots=True)
class ProjectDefinitions:
    generated_from: list[str] = field(default_factory=list)
    external_signatures: dict[str, Signature] = field(default_factory=dict)
    user_signatures: dict[str, Signature] = field(default_factory=dict)
    files: dict[str, FileDefinitions] = field(default_factory=dict)


def derive_definitions_path(source_path: str | Path) -> Path:
    return Path(source_path).resolve().parent / DEFINITIONS_FILENAME


def _format_default_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _format_param(spec: ParamSpec) -> str:
    text = f"{spec.type_name} {spec.name}"
    if spec.has_default:
        text += f" = {_format_default_value(spec.default)}"
    return text


def format_signature_prototype(signature: Signature) -> str:
    parts = [_format_param(param) for param in signature.params]
    if signature.variadic:
        parts.append("...")
    params_text = ", ".join(parts) if parts else "void"
    return f"{signature.return_type} {signature.name}({params_text})"


def _parse_default_value(text: str) -> object:
    text = text.strip()
    if not text:
        return NO_DEFAULT
    if text[0] in {'"', "'"}:
        return json.loads(text.replace("'", '"'))
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text, 0)
    except ValueError:
        return text


def _split_param_text(params_text: str) -> list[str]:
    if not params_text.strip():
        return []

    parts: list[str] = []
    current: list[str] = []
    in_string = False
    quote_char = ""

    for char in params_text:
        if in_string:
            current.append(char)
            if char == quote_char:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote_char = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _param_from_dict(data: dict[str, object]) -> ParamSpec:
    return ParamSpec(
        name=str(data.get("name", "arg")),
        type_name=str(data.get("type", "long")),
        default=data.get("default", NO_DEFAULT),
    )


def parse_signature_prototype(prototype: str, default_source: str, aliases: tuple[str, ...] = ()) -> Signature:
    match = PROTOTYPE_RE.match(prototype)
    if match is None:
        raise ValueError(f"Invalid function prototype: {prototype}")

    params: list[ParamSpec] = []
    variadic = False
    params_text = match.group("params").strip()
    if params_text and params_text != "void":
        for part in _split_param_text(params_text):
            if part == "...":
                variadic = True
                continue

            default = NO_DEFAULT
            raw_param = part
            if "=" in part:
                raw_param, raw_default = part.split("=", 1)
                default = _parse_default_value(raw_default)
            param_match = PARAM_RE.match(raw_param.strip())
            if param_match is None:
                raise ValueError(f"Invalid parameter declaration: {part}")
            params.append(
                ParamSpec(
                    name=param_match.group("name"),
                    type_name=param_match.group("type_name"),
                    default=default,
                )
            )

    return Signature(
        name=match.group("name"),
        return_type=match.group("return_type"),
        params=tuple(params),
        aliases=aliases,
        source=default_source,
        variadic=variadic,
    )


def _signature_to_entry(signature: Signature) -> dict[str, object]:
    payload: dict[str, object] = {"prototype": format_signature_prototype(signature)}
    if signature.aliases:
        payload["aliases"] = list(signature.aliases)
    return payload


def _signature_from_dict(name: str, data: dict[str, object], default_source: str) -> Signature:
    return Signature(
        name=name,
        return_type=str(data.get("return_type", "void")),
        params=tuple(_param_from_dict(item) for item in data.get("params", [])),
        aliases=tuple(str(alias) for alias in data.get("aliases", [])),
        source=str(data.get("source", default_source)),
        variadic=bool(data.get("variadic", False)),
    )


def _signatures_from_entries(entries: list[dict[str, object]], default_source: str) -> dict[str, Signature]:
    signatures: dict[str, Signature] = {}
    for entry in entries:
        signature = parse_signature_prototype(
            str(entry.get("prototype", "")),
            default_source,
            tuple(str(alias) for alias in entry.get("aliases", [])),
        )
        signatures[signature.name] = signature
    return signatures


def _signature_from_entry(entry: dict[str, object], default_source: str) -> Signature:
    return parse_signature_prototype(
        str(entry.get("prototype", "")),
        default_source,
        tuple(str(alias) for alias in entry.get("aliases", [])),
    )


def _load_signature_collection(
    payload: object,
    default_source: str,
) -> dict[str, Signature]:
    if isinstance(payload, list):
        return _signatures_from_entries(payload, default_source)
    if isinstance(payload, dict):
        return {
            str(name): _signature_from_dict(str(name), data, default_source)
            for name, data in payload.items()
            if isinstance(data, dict)
        }
    return {}


def normalize_file_definition_key(value: str | Path) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def _encode_index_set(indices: Iterable[int]) -> str:
    ordered = sorted(set(indices))
    if not ordered:
        return ""
    ranges: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _decode_index_set(value: object) -> list[int]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, int)]
    if not isinstance(value, str) or not value:
        return []
    indices: list[int] = []
    for part in value.split(","):
        bounds = part.split("-", 1)
        try:
            start = int(bounds[0])
            end = int(bounds[1]) if len(bounds) == 2 else start
        except ValueError:
            continue
        if start <= end:
            indices.extend(range(start, end + 1))
    return indices


def find_file_definitions(
    definitions: ProjectDefinitions,
    source_path: str | Path,
    source_key: str | None = None,
) -> tuple[str, FileDefinitions] | None:
    if not definitions.files:
        return None

    candidates = []
    if source_key:
        candidates.append(normalize_file_definition_key(source_key))
    normalized_source = normalize_file_definition_key(source_path)
    candidates.extend((normalized_source, Path(source_path).name))
    lower_to_key = {key.lower(): key for key in definitions.files}
    for candidate in candidates:
        matched_key = lower_to_key.get(candidate.lower())
        if matched_key is not None:
            return matched_key, definitions.files[matched_key]

    basename = Path(source_path).name.lower()
    basename_matches = [
        key
        for key in definitions.files
        if Path(key).name.lower() == basename
    ]
    if len(basename_matches) == 1:
        matched_key = basename_matches[0]
        return matched_key, definitions.files[matched_key]
    return None


def _aggregate_file_user_signatures(
    legacy_signatures: dict[str, Signature],
    files: dict[str, FileDefinitions],
) -> dict[str, Signature]:
    aggregated = dict(legacy_signatures)
    for key in sorted(files, key=str.lower):
        aggregated.update(files[key].user_signatures)
    return aggregated


def load_project_definitions(path: str | Path) -> ProjectDefinitions:
    definitions_path = Path(path)
    if not definitions_path.exists():
        return ProjectDefinitions()

    payload = json.loads(definitions_path.read_text(encoding="utf-8"))
    raw_external = payload.get("external_functions", payload.get("system_functions", {}))
    raw_user = payload.get("user_functions", payload.get("script_functions", {}))

    external_signatures = _load_signature_collection(raw_external, "project-external")
    legacy_user_signatures = _load_signature_collection(raw_user, "project-user")
    files: dict[str, FileDefinitions] = {}
    raw_files = payload.get("files", {})
    raw_function_pool = payload.get("script_function_pool", [])
    function_pool = [
        _signature_from_entry(entry, "project-user")
        for entry in raw_function_pool
        if isinstance(entry, dict)
    ] if isinstance(raw_function_pool, list) else []
    raw_function_sets = payload.get("script_function_sets", [])
    function_sets = raw_function_sets if isinstance(raw_function_sets, list) else []
    if isinstance(raw_files, dict):
        for raw_key, raw_scope in raw_files.items():
            if not isinstance(raw_scope, dict):
                continue
            key = normalize_file_definition_key(str(raw_key))
            user_signatures: dict[str, Signature]
            raw_set_index = raw_scope.get("user_function_set")
            if isinstance(raw_set_index, int) and 0 <= raw_set_index < len(function_sets):
                raw_indices = function_sets[raw_set_index]
                user_signatures = {
                    function_pool[index].name: function_pool[index]
                    for index in _decode_index_set(raw_indices)
                    if 0 <= index < len(function_pool)
                }
            else:
                user_signatures = _load_signature_collection(
                    raw_scope.get("user_functions", raw_scope.get("script_functions", {})),
                    "project-user",
                )
            files[key] = FileDefinitions(
                imports=[str(item) for item in raw_scope.get("imports", [])],
                user_signatures=user_signatures,
            )

    return ProjectDefinitions(
        generated_from=[str(item) for item in payload.get("generated_from", [])],
        external_signatures=external_signatures,
        user_signatures=_aggregate_file_user_signatures(legacy_user_signatures, files),
        files=files,
    )


def write_project_definitions(definitions: ProjectDefinitions, path: str | Path) -> Path:
    definitions_path = Path(path)
    payload: dict[str, object] = {
        "version": DEFINITIONS_VERSION if definitions.files else 2,
        "generated_from": sorted(definitions.generated_from, key=str.lower),
        "external_functions": [
            _signature_to_entry(signature)
            for _, signature in sorted(definitions.external_signatures.items(), key=lambda item: item[0].lower())
        ],
    }
    if definitions.files:
        entry_by_identity: dict[str, dict[str, object]] = {}
        scope_identities: dict[str, tuple[str, ...]] = {}
        for key, scope in definitions.files.items():
            identities: list[str] = []
            for _, signature in sorted(
                scope.user_signatures.items(),
                key=lambda item: item[0].lower(),
            ):
                entry = _signature_to_entry(signature)
                identity = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                entry_by_identity[identity] = entry
                identities.append(identity)
            scope_identities[key] = tuple(identities)

        ordered_identities = sorted(
            entry_by_identity,
            key=lambda identity: (
                str(entry_by_identity[identity].get("prototype", "")).lower(),
                identity,
            ),
        )
        pool_index = {identity: index for index, identity in enumerate(ordered_identities)}
        function_sets = sorted(
            {
                tuple(pool_index[identity] for identity in identities)
                for identities in scope_identities.values()
            }
        )
        set_index = {indices: index for index, indices in enumerate(function_sets)}
        payload["script_function_pool"] = [
            entry_by_identity[identity]
            for identity in ordered_identities
        ]
        payload["script_function_sets"] = [
            _encode_index_set(indices)
            for indices in function_sets
        ]
        files_payload: dict[str, object] = {}
        for key, scope in sorted(definitions.files.items(), key=lambda item: item[0].lower()):
            indices = tuple(pool_index[identity] for identity in scope_identities[key])
            scope_payload: dict[str, object] = {"user_function_set": set_index[indices]}
            if scope.imports:
                scope_payload["imports"] = list(scope.imports)
            files_payload[key] = scope_payload
        payload["files"] = files_payload
    else:
        payload["user_functions"] = [
            _signature_to_entry(signature)
            for _, signature in sorted(definitions.user_signatures.items(), key=lambda item: item[0].lower())
        ]
    definitions_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return definitions_path


def make_script_signature(function: Function) -> Signature:
    params = tuple(
        ParamSpec(
            name=function.params[index] if index < len(function.params) else f"arg{index}",
            type_name=function.param_types[index] if index < len(function.param_types) else "long",
        )
        for index in range(function.arg_count)
    )
    return Signature(name=function.name, return_type=function.return_type, params=params, source="script")


def make_script_signature_map(module: Module) -> dict[str, Signature]:
    return {function.name: make_script_signature(function) for function in module.functions}


def iter_expr_children(expr: Expr) -> tuple[Expr, ...]:
    if isinstance(expr, AddressOf):
        return (expr.target,)
    if isinstance(expr, DerefExpr):
        return (expr.address,)
    if isinstance(expr, BinaryExpr):
        return (expr.left, expr.right)
    if isinstance(expr, UnaryExpr):
        return (expr.value,)
    if isinstance(expr, CallExpr):
        return expr.args
    return ()


def iter_expr_tree(expr: Expr):
    yield expr
    for child in iter_expr_children(expr):
        yield from iter_expr_tree(child)


def iter_stmt_exprs(stmt) -> tuple[Expr, ...]:
    if isinstance(stmt, AssignStmt):
        return (stmt.value,)
    if isinstance(stmt, ExprStmt):
        return (stmt.value,)
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return (stmt.value,)
    if isinstance(stmt, SetBaseStmt):
        return (stmt.value,)
    if isinstance(stmt, BranchStmt):
        return (stmt.condition,)
    return ()


def _iter_calls_with_usage(
    stmt: object,
    fallback_library: SignatureLibrary,
) -> list[tuple[CallExpr, str]]:
    """Collect calls and return-type evidence from each expression context."""
    roots = iter_stmt_exprs(stmt)
    if not roots:
        return []

    if isinstance(stmt, ExprStmt) and isinstance(stmt.value, CallExpr):
        root_type = "void"
    elif isinstance(stmt, BranchStmt):
        root_type = "bool"
    else:
        root_type = "long"
    calls: list[tuple[CallExpr, str]] = []

    def visit(expr: Expr, expected_type: str) -> None:
        if isinstance(expr, CallExpr):
            calls.append((expr, expected_type))
            signature = fallback_library.get(expr.callee)
            for index, child in enumerate(expr.args):
                child_type = (
                    signature.params[index].type_name
                    if signature is not None and index < len(signature.params)
                    else "long"
                )
                visit(child, child_type)
            return
        if isinstance(expr, UnaryExpr) and expr.op == "!":
            visit(expr.value, "bool")
            return
        if isinstance(expr, BinaryExpr):
            child_type = "bool" if expr.op in {"&&", "||"} else "long"
            visit(expr.left, child_type)
            visit(expr.right, child_type)
            return
        for child in iter_expr_children(expr):
            visit(child, expected_type)

    for root in roots:
        visit(root, root_type)
    return calls


def _extend_signature_to_arg_count(signature: Signature | None, name: str, arg_count: int) -> Signature:
    if signature is None:
        params = tuple(ParamSpec(name=f"arg{index}") for index in range(arg_count))
        return Signature(name=name, return_type="void", params=params, source="project-system")

    params = list(signature.params[:arg_count])
    while len(params) < arg_count:
        params.append(ParamSpec(name=f"arg{len(params)}"))
    return Signature(
        name=signature.name,
        return_type=signature.return_type,
        params=tuple(params),
        aliases=signature.aliases,
        source="project-system",
        variadic=signature.variadic,
    )


def _guess_expr_type(expr: Expr) -> str:
    if isinstance(expr, StringLiteral):
        return "char*"
    if isinstance(expr, IntLiteral) and expr.value == 0:
        return "null"
    if isinstance(expr, AddressOf):
        if isinstance(expr.target, StringLiteral):
            return "char*"
        return "long*"
    if isinstance(expr, UnaryExpr) and expr.op == "!":
        return "bool"
    if isinstance(expr, BinaryExpr) and expr.op in {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}:
        return "bool"
    return "long"


def _choose_param_type(votes: Counter[str]) -> str:
    if not votes:
        return "long"

    concrete_votes = Counter({key: value for key, value in votes.items() if key != "null"})
    if concrete_votes:
        votes = concrete_votes
    else:
        return "long"

    priority = {"char*": 4, "long*": 3, "bool": 2, "long": 1}
    return max(votes.items(), key=lambda item: (item[1], priority.get(item[0], 0), item[0] != "long"))[0]


def _choose_return_type(votes: Counter[str] | None) -> str:
    if not votes:
        return "void"
    value_votes = Counter({name: count for name, count in votes.items() if name != "void"})
    if not value_votes:
        return "void"
    priority = {"bool": 3, "long*": 2, "char*": 2, "long": 1}
    return max(value_votes.items(), key=lambda item: (item[1], priority.get(item[0], 0)))[0]


def _extend_signature_with_type_hints(
    signature: Signature | None,
    name: str,
    arg_count: int,
    param_type_votes: list[Counter[str]] | None,
    return_type: str | None = None,
) -> Signature:
    expanded = _extend_signature_to_arg_count(signature, name, arg_count)
    inferred_return_type = expanded.return_type if return_type is None else return_type
    if not param_type_votes:
        if inferred_return_type == expanded.return_type:
            return expanded
        return Signature(
            name=expanded.name,
            return_type=inferred_return_type,
            params=expanded.params,
            aliases=expanded.aliases,
            source=expanded.source,
            variadic=expanded.variadic,
        )

    params: list[ParamSpec] = []
    for index, spec in enumerate(expanded.params):
        if spec.type_name == "label":
            params.append(spec)
            continue
        if index < len(param_type_votes):
            merged_votes = Counter(param_type_votes[index])
            # Keep the existing signature as a weak prior, while allowing call-site votes to override it.
            merged_votes[spec.type_name] += 1
            inferred_type = _choose_param_type(merged_votes)
            params.append(ParamSpec(name=spec.name, type_name=inferred_type, default=spec.default))
            continue
        params.append(spec)

    return Signature(
        name=expanded.name,
        return_type=inferred_return_type,
        params=tuple(params),
        aliases=expanded.aliases,
        source=expanded.source,
        variadic=expanded.variadic,
    )


def collect_external_signatures(
    modules: Iterable[Module],
    fallback_library: SignatureLibrary,
    known_user_signatures: dict[str, Signature] | None = None,
    module_user_signatures: Iterable[dict[str, Signature]] | None = None,
) -> dict[str, Signature]:
    modules = list(modules)
    per_module_user_signatures = (
        list(module_user_signatures)
        if module_user_signatures is not None
        else None
    )
    if per_module_user_signatures is not None and len(per_module_user_signatures) != len(modules):
        raise ValueError("module_user_signatures must match modules")
    shared_user_names = set((known_user_signatures or {}).keys())
    all_module_user_names = {function.name for module in modules for function in module.functions}
    observed_arg_counts: dict[str, int] = {}
    observed_param_type_votes: dict[str, list[Counter[str]]] = {}
    observed_return_type_votes: dict[str, Counter[str]] = {}

    for module_index, module in enumerate(modules):
        if per_module_user_signatures is None:
            user_names = set(all_module_user_names)
            user_names.update(shared_user_names)
        else:
            user_names = {function.name for function in module.functions}
            user_names.update(per_module_user_signatures[module_index])
        for function in module.functions:
            for block in function.blocks:
                statements = list(block.body)
                if block.terminator is not None:
                    statements.append(block.terminator)
                for stmt in statements:
                    for expr, return_type_hint in _iter_calls_with_usage(stmt, fallback_library):
                        if expr.callee in user_names:
                            continue
                        fallback_signature = fallback_library.get(expr.callee)
                        canonical_name = fallback_signature.name if fallback_signature is not None else expr.callee
                        observed_arg_counts[canonical_name] = max(
                            observed_arg_counts.get(canonical_name, 0),
                            len(expr.args),
                        )
                        callee_votes = observed_param_type_votes.setdefault(canonical_name, [])
                        while len(callee_votes) < len(expr.args):
                            callee_votes.append(Counter[str]())
                        for index, arg in enumerate(expr.args):
                            callee_votes[index][_guess_expr_type(arg)] += 1
                        if fallback_signature is None or fallback_signature.source != "builtin":
                            observed_return_type_votes.setdefault(canonical_name, Counter())[return_type_hint] += 1

    return {
        name: _extend_signature_with_type_hints(
            fallback_library.get(name),
            name,
            arg_count,
            observed_param_type_votes.get(name),
            return_type=(
                None
                if fallback_library.get(name) is not None
                and fallback_library.get(name).source == "builtin"
                else _choose_return_type(observed_return_type_votes.get(name))
            ),
        )
        for name, arg_count in sorted(observed_arg_counts.items())
    }


def build_project_definitions(
    results: Iterable[tuple[Path, Module]],
    fallback_library: SignatureLibrary,
    seed_definitions: ProjectDefinitions | None = None,
    source_keys: dict[Path, str] | None = None,
    visible_user_signatures: dict[Path, dict[str, Signature]] | None = None,
) -> ProjectDefinitions:
    items = list(results)
    modules = [module for _, module in items]
    normalized_source_keys = {
        path.resolve(): normalize_file_definition_key(key)
        for path, key in (source_keys or {}).items()
    }
    generated_from = sorted(
        {
            normalized_source_keys.get(path.resolve(), path.name)
            for path, _ in items
        }.union(seed_definitions.generated_from if seed_definitions else []),
        key=str.lower,
    )
    files = dict(seed_definitions.files if seed_definitions else {})
    legacy_user_signatures = (
        seed_definitions.user_signatures
        if seed_definitions is not None and not seed_definitions.files
        else {}
    )
    module_visible_signatures: list[dict[str, Signature]] = []
    for path, module in items:
        resolved = path.resolve()
        key = normalized_source_keys.get(resolved, path.name)
        existing = find_file_definitions(seed_definitions, path, key) if seed_definitions else None
        seeded_user_signatures = dict(existing[1].user_signatures if existing else {})
        module_function_names = {function.name for function in module.functions}
        for name, signature in legacy_user_signatures.items():
            if name in module_function_names:
                seeded_user_signatures[name] = signature
        seeded_user_signatures.update(make_script_signature_map(module))
        if existing is not None and existing[0] != key:
            files.pop(existing[0], None)
        files[key] = FileDefinitions(
            imports=list(module.metadata.imports),
            user_signatures=seeded_user_signatures,
        )
        module_visible_signatures.append(
            (visible_user_signatures or {}).get(resolved, seeded_user_signatures)
        )

    observed_external_signatures = collect_external_signatures(
        modules,
        fallback_library,
        module_user_signatures=module_visible_signatures,
    )
    external_signatures: dict[str, Signature] = {
        signature.name: signature
        for signature in (seed_definitions.external_signatures.values() if seed_definitions else ())
    }
    for name, signature in observed_external_signatures.items():
        external_signatures[signature.name] = signature

    return ProjectDefinitions(
        generated_from=generated_from,
        external_signatures=external_signatures,
        user_signatures=_aggregate_file_user_signatures({}, files),
        files=files,
    )


def merge_project_definitions(definitions_list: Iterable[ProjectDefinitions]) -> ProjectDefinitions:
    generated_from: set[str] = set()
    external_signatures: dict[str, Signature] = {}
    user_signatures: dict[str, Signature] = {}
    files: dict[str, FileDefinitions] = {}

    for definitions in definitions_list:
        generated_from.update(definitions.generated_from)
        for signature in definitions.external_signatures.values():
            external_signatures[signature.name] = signature
        if definitions.files:
            files.update(definitions.files)
        else:
            user_signatures.update(definitions.user_signatures)

    if files:
        user_signatures = _aggregate_file_user_signatures(user_signatures, files)

    return ProjectDefinitions(
        generated_from=sorted(generated_from, key=str.lower),
        external_signatures=external_signatures,
        user_signatures=user_signatures,
        files=files,
    )
