from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from .asm_parser import AsmInstruction, AssemblyModule
from .asm_parser import split_arguments
from .ir import Module
from .lifter import immediate_call_result_counts


FUNCTION_PROTOTYPE_RE = re.compile(
    r"^static\s+(?P<return_type>.+?)(?P<name>bpd_[A-Za-z0-9_]+)"
    r"\((?P<parameters>.*)\);$"
)
SCRIPT_LOAD_RE = re.compile(
    r"(?P<variable>[A-Za-z_]\w*)\s*=\s*sys1_load_program\("
    r"(?P<arguments>.*)\);"
)
SCRIPT_NAME_RE = re.compile(r'"(?P<name>[^"\\/]+)\._bp"', re.IGNORECASE)
DISPATCH_ENTRY_RE = re.compile(
    r"(?:\(BPDEntryPoint\))?&?(?P<name>bpd_[A-Za-z0-9_]+)\s*,?$"
)
LOCAL_DISPATCH_TABLE_RE = re.compile(
    r"^BPDEntryPoint\s+[A-Za-z_]\w*\[[^\]]+\]\s*=\s*\{(?P<entries>.*)\};$"
)
LOCAL_DISPATCH_COPY_RE = re.compile(
    r"^(?P<table>[A-Za-z_]\w*)\[(?P<target>.+)\]\s*=\s*"
    r"\(BPDEntryPoint\[\]\)\{(?P<entries>.*)\}\[(?P<source>.+)\];$"
)
CONTROL_BOUNDARY_RE = re.compile(
    r"^(?:if\b|else\b|while\b|do\b|for\b|switch\b|goto\b|return\b|"
    r"(?:BPD_)?L[0-9A-Fa-f]+:|[{}])"
)
PORTABLE_RETURN_RE = re.compile(
    r"^(?:(?:const|volatile)\s+)?"
    r"(?:void|char|u?int(?:8|16|32|64)_t|intptr_t|uintptr_t|"
    r"BPDValues|BPDProgramEntry|BPDEntryPoint)(?:\s*\*+)?$"
)
PORTABLE_PARAMETER_RE = re.compile(
    r"^(?:(?:const|volatile)\s+)?"
    r"(?:void|char|u?int(?:8|16|32|64)_t|intptr_t|uintptr_t|"
    r"BPDValues|BPDProgramEntry|BPDEntryPoint)"
    r"\s*(?:\*+\s*)?[A-Za-z_]\w*(?:\[[^\]]*\])?$"
)
PORTABLE_STRUCT_PARAMETER_RE = re.compile(
    r"^struct\s+[A-Za-z_]\w*\s*\*+\s*[A-Za-z_]\w*$"
)
SOURCE_COMMENT_RE = re.compile(r"\s*/\*\s*BPD\s+.*?\*/\s*$")


class ProjectResult(Protocol):
    source_path: Path
    c_source: str


class ProjectIrResult(ProjectResult, Protocol):
    assembly: AssemblyModule
    module: Module


@dataclass(slots=True, frozen=True)
class ProjectFunction:
    script_name: str
    name: str
    return_type: str
    parameters: str
    argument_count: int
    result_count: int

    @property
    def prototype(self) -> str:
        separator = "" if self.return_type.endswith("*") else " "
        return f"{self.return_type}{separator}{self.name}({self.parameters});"

    @property
    def is_portable(self) -> bool:
        if PORTABLE_RETURN_RE.fullmatch(self.return_type) is None:
            return False
        parameters = self.parameters.strip()
        if parameters == "void":
            return True
        return all(
            PORTABLE_PARAMETER_RE.fullmatch(parameter.strip()) is not None
            or PORTABLE_STRUCT_PARAMETER_RE.fullmatch(parameter.strip()) is not None
            for parameter in split_arguments(parameters)
        )


@dataclass(slots=True, frozen=True)
class ProjectModule:
    script_name: str
    entry: ProjectFunction | None
    dispatch: tuple[ProjectFunction | None, ...]
    functions: dict[str, ProjectFunction]


@dataclass(slots=True, frozen=True)
class BpdProjectIndex:
    modules: dict[str, ProjectModule]


def _parameter_pointer_type(parameter: str) -> str | None:
    token = parameter.strip()
    array = re.fullmatch(
        r"(?P<type>.+?)\s*(?P<name>[A-Za-z_]\w*)\[[^\]]*\]",
        token,
    )
    if array is not None:
        return array.group("type").rstrip() + " *"
    pointer = re.fullmatch(
        r"(?P<type>.+\*)\s*(?P<name>[A-Za-z_]\w*)",
        token,
    )
    return pointer.group("type").strip() if pointer is not None else None


def _coerce_pointer_arguments(
    target: ProjectFunction,
    arguments: list[str],
) -> list[str]:
    parameters = split_arguments(target.parameters)
    rendered = list(arguments)
    for index, (argument, parameter) in enumerate(zip(arguments, parameters)):
        pointer_type = _parameter_pointer_type(parameter)
        if pointer_type in {None, "uint8_t *", "const uint8_t *"}:
            continue
        absolute = re.fullmatch(
            r"-?(?:0[xX][0-9A-Fa-f]+|\d+)",
            argument.strip(),
        )
        pointee = pointer_type.removeprefix("const ").removesuffix(" *")
        width = {
            "char": 8,
            "int8_t": 8,
            "int16_t": 16,
            "int32_t": 32,
            "int64_t": 64,
        }.get(pointee)
        if absolute is not None and width is not None:
            address = int(absolute.group(0), 0)
            if address >= 0:
                rendered[index] = f"global_i{width}_array_{address:08x}"
                continue
        if re.match(r"^\((?:const\s+)?uint8_t\s*\*\)\s*&", argument.strip()):
            rendered[index] = f"({pointer_type})({argument})"
    return rendered


def normalize_script_name(name: str) -> str:
    return name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()


def _argument_count(parameters: str) -> int:
    text = parameters.strip()
    return 0 if not text or text == "void" else len(split_arguments(text))


def _function_body_lines(source: str, function_name: str) -> list[str]:
    lines = source.splitlines()
    signature = re.compile(
        rf"^(?:static\s+)?\S.*?\b{re.escape(function_name)}\(.*\)$"
    )
    for index, line in enumerate(lines[:-1]):
        if signature.fullmatch(line.strip()) is None or lines[index + 1].strip() != "{":
            continue
        body: list[str] = []
        depth = 0
        for body_line in lines[index + 1:]:
            depth += body_line.count("{")
            depth -= body_line.count("}")
            if depth == 0:
                return body
            body.append(body_line)
    return []


def _function_result_count(source: str, name: str, return_type: str) -> int:
    if return_type == "void":
        return 0
    if return_type != "BPDValues":
        return 1
    counts = [
        len(split_arguments(match.group("values")))
        for line in _function_body_lines(source, name)
        for match in [re.search(r"\breturn\s+values\((?P<values>.*)\);", line)]
        if match is not None
    ]
    return max(counts, default=2)


def _function_prototypes(script_name: str, source: str) -> dict[str, ProjectFunction]:
    functions: dict[str, ProjectFunction] = {}
    for line in source.splitlines():
        match = FUNCTION_PROTOTYPE_RE.fullmatch(line.strip())
        if match is None:
            continue
        parameters = match.group("parameters").strip()
        function = ProjectFunction(
            script_name=script_name,
            name=match.group("name"),
            return_type=match.group("return_type").strip(),
            parameters=parameters,
            argument_count=_argument_count(parameters),
            result_count=_function_result_count(
                source,
                match.group("name"),
                match.group("return_type").strip(),
            ),
        )
        functions[function.name] = function
    return functions


def _entry_body_lines(source: str, entry_name: str) -> list[str]:
    return _function_body_lines(source, entry_name)


def _entry_dispatch(
    source: str,
    entry: ProjectFunction | None,
    functions: dict[str, ProjectFunction],
) -> tuple[ProjectFunction | None, ...]:
    if entry is None:
        return ()
    body = _entry_body_lines(source, entry.name)
    for line in body:
        stripped_line = SOURCE_COMMENT_RE.sub("", line).strip()
        local_table = LOCAL_DISPATCH_TABLE_RE.fullmatch(stripped_line)
        if local_table is None:
            continue
        raw_entries = split_arguments(local_table.group("entries"))
        if raw_entries and all(
            item.strip() in {"0", "0x0", "NULL"} for item in raw_entries
        ):
            continue
        dispatch: list[ProjectFunction | None] = []
        for item in raw_entries:
            stripped = item.strip()
            if stripped == "NULL":
                dispatch.append(None)
                continue
            match = DISPATCH_ENTRY_RE.fullmatch(stripped)
            if match is None:
                return ()
            dispatch.append(functions.get(match.group("name")))
        return tuple(dispatch)
    copied_entries: dict[int, ProjectFunction | None] = {}
    copied_table: str | None = None
    for line in body:
        copied = LOCAL_DISPATCH_COPY_RE.fullmatch(
            SOURCE_COMMENT_RE.sub("", line).strip()
        )
        if copied is None:
            continue
        target_index = copied.group("target").strip()
        source_index = copied.group("source").strip()
        if target_index == source_index:
            start_index = 0
        else:
            offset = re.fullmatch(
                rf"(?P<offset>\d+)\s*\+\s*{re.escape(source_index)}",
                target_index,
            ) or re.fullmatch(
                rf"{re.escape(source_index)}\s*\+\s*(?P<offset>\d+)",
                target_index,
            )
            if offset is None:
                continue
            start_index = int(offset.group("offset"), 10)
        if copied_table not in {None, copied.group("table")}:
            return ()
        copied_table = copied.group("table")
        for relative_index, item in enumerate(split_arguments(copied.group("entries"))):
            stripped = item.strip()
            if stripped in {"0", "0x0", "NULL"}:
                function = None
            else:
                match = DISPATCH_ENTRY_RE.fullmatch(stripped)
                if match is None:
                    return ()
                function = functions.get(match.group("name"))
                if function is None:
                    return ()
            index = start_index + relative_index
            if index in copied_entries and copied_entries[index] != function:
                return ()
            copied_entries[index] = function
    if copied_entries:
        dispatch_call = re.compile(
            rf"^\(?{re.escape(copied_table or '')}\[[^\]]+\]\)?\(.*\);$"
        )
        if not any(
            dispatch_call.fullmatch(SOURCE_COMMENT_RE.sub("", line).strip())
            for line in body
        ):
            return ()
        count = max(copied_entries) + 1
        return tuple(copied_entries.get(index) for index in range(count))
    start = next(
        (
            index
            for index, line in enumerate(body)
            if "static const BPDEntryPoint dispatch_table[]" in line
        ),
        None,
    )
    if start is None:
        return ()
    dispatch: list[ProjectFunction | None] = []
    for line in body[start + 1:]:
        stripped = SOURCE_COMMENT_RE.sub("", line).strip()
        if stripped.startswith("};"):
            break
        if stripped.rstrip(",") == "NULL":
            dispatch.append(None)
            continue
        match = DISPATCH_ENTRY_RE.fullmatch(stripped)
        if match is None:
            return ()
        dispatch.append(functions.get(match.group("name")))
    return tuple(dispatch)


def scan_bpd_project(results: list[ProjectResult]) -> BpdProjectIndex:
    """Collect batch-local entry dispatch tables and callable prototypes."""
    modules: dict[str, ProjectModule] = {}
    for result in results:
        script_name = normalize_script_name(result.source_path.stem)
        functions = _function_prototypes(script_name, result.c_source)
        entry = functions.get(f"bpd_{script_name}_entry")
        modules[script_name] = ProjectModule(
            script_name=script_name,
            entry=entry,
            dispatch=_entry_dispatch(result.c_source, entry, functions),
            functions=functions,
        )
    return BpdProjectIndex(modules)


def _script_from_load(match: re.Match[str]) -> str | None:
    script = SCRIPT_NAME_RE.search(match.group("arguments"))
    return normalize_script_name(script.group("name")) if script is not None else None


def _matching_parenthesis(text: str, opening: int) -> int | None:
    depth = 1
    quote: str | None = None
    index = opening + 1
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def collect_fixed_pointer_call_hints(
    results: list[ProjectIrResult],
) -> dict[Path, dict[int, tuple[int, int]]]:
    """Infer stable native-address arities and trim only excess stack words."""
    observations: dict[
        str,
        list[tuple[ProjectIrResult, int, int, int]],
    ] = {}
    for result in results:
        instructions = {
            item.line_no: item
            for item in result.assembly.items
            if isinstance(item, AsmInstruction) and item.opcode == "call_ptr"
        }
        result_counts = immediate_call_result_counts(result.assembly)
        for block in result.module.blocks:
            for statement in block.statements:
                parsed_calls: list[tuple[str, int]] = []
                for match in re.finditer(r"\bcall_pointer\s*\(", statement.text):
                    opening = statement.text.find("(", match.start())
                    closing = _matching_parenthesis(statement.text, opening)
                    if closing is None:
                        continue
                    arguments = split_arguments(statement.text[opening + 1:closing])
                    if not arguments or re.fullmatch(
                        r"-?(?:0[xX][0-9A-Fa-f]+|\d+)",
                        arguments[0].strip(),
                    ) is None:
                        continue
                    parsed_calls.append(
                        (arguments[0].strip().lower(), len(arguments) - 1)
                    )
                call_lines = sorted(
                    line for line in statement.source_lines if line in instructions
                )
                if len(parsed_calls) != len(call_lines):
                    continue
                for (address, argument_count), line in zip(parsed_calls, call_lines):
                    instruction = instructions[line]
                    if (
                        not instruction.args
                        or instruction.args[0].strip().lower() != address
                    ):
                        continue
                    observations.setdefault(address, []).append(
                        (result, line, argument_count, result_counts.get(line, 0))
                    )

    hints: dict[Path, dict[int, tuple[int, int]]] = {}
    for entries in observations.values():
        counts = Counter(argument_count for _, _, argument_count, _ in entries)
        if not counts:
            continue
        arity, support = counts.most_common(1)[0]
        total = sum(counts.values())
        if (
            support < 4
            or support * 5 < total * 4
            or any(argument_count < arity for argument_count in counts)
        ):
            continue
        for result, line, argument_count, result_count in entries:
            if argument_count <= arity:
                continue
            hints.setdefault(result.source_path, {})[line] = (arity, result_count)
    return hints


def _resolve_target(module: ProjectModule, arguments: list[str]) -> tuple[ProjectFunction, list[str]] | None:
    if module.dispatch:
        if not arguments:
            return None
        try:
            selector = int(arguments[-1].strip(), 0)
        except ValueError:
            return None
        if selector < 0 or selector >= len(module.dispatch):
            return None
        target = module.dispatch[selector]
        call_arguments = arguments[:-1]
    else:
        target = module.entry
        call_arguments = arguments
    if target is None or not target.is_portable or len(call_arguments) < target.argument_count:
        return None
    if target.argument_count:
        call_arguments = call_arguments[-target.argument_count:]
    else:
        call_arguments = []
    return target, call_arguments


def collect_cross_file_call_hints(
    results: list[ProjectIrResult],
    index: BpdProjectIndex | None = None,
) -> dict[Path, dict[int, tuple[int, int]]]:
    """Map dynamic VM call lines to batch-known argument and result widths."""
    project = index or scan_bpd_project(results)
    hints = collect_fixed_pointer_call_hints(results)
    for result in results:
        script_name = normalize_script_name(result.source_path.stem)
        own_module = project.modules.get(script_name)
        roots = {
            "entry" if function.name.endswith("_entry") else function.name.removeprefix(f"bpd_{script_name}_")
            for function in (own_module.functions.values() if own_module is not None else ())
        }
        instructions = {
            item.line_no: item
            for item in result.assembly.items
            if isinstance(item, AsmInstruction)
        }
        blocks = result.module.blocks
        index_by_label = {block.label: position for position, block in enumerate(blocks)}
        root_indices = {
            position for position, block in enumerate(blocks) if block.label in roots
        }

        def transfer_bindings(
            incoming: dict[str, str],
            block_index: int,
        ) -> dict[str, str]:
            current = dict(incoming)
            for statement in blocks[block_index].statements:
                text = statement.text
                load = SCRIPT_LOAD_RE.search(text)
                loaded_variable: str | None = None
                if load is not None:
                    loaded_variable = load.group("variable")
                    loaded_script = _script_from_load(load)
                    if loaded_script is None:
                        current.pop(loaded_variable, None)
                    else:
                        current[loaded_variable] = loaded_script
                stripped = text.strip()
                for variable in tuple(current):
                    assignment = re.match(
                        rf"^(?:BPDProgramEntry\s+)?{re.escape(variable)}\s*=",
                        stripped,
                    )
                    if assignment is not None and variable != loaded_variable:
                        current.pop(variable, None)
            return current

        # Definite-binding data flow: a script entry survives a branch or loop
        # only when every reachable predecessor agrees on the same module.
        # This keeps dominating loads alive across guards without carrying a
        # path-local load into an unrelated merge.
        incoming_bindings: list[dict[str, str] | None] = [None] * len(blocks)
        outgoing_bindings: list[dict[str, str] | None] = [None] * len(blocks)
        worklist: list[int] = sorted(
            root_indices
            | {
                position
                for position in range(len(blocks))
                if not result.module.cfg.predecessors[position]
            }
        )
        for position in worklist:
            incoming_bindings[position] = {}
        while worklist:
            block_index = worklist.pop(0)
            incoming = incoming_bindings[block_index]
            if incoming is None:
                continue
            outgoing = transfer_bindings(incoming, block_index)
            if outgoing_bindings[block_index] == outgoing:
                continue
            outgoing_bindings[block_index] = outgoing
            for successor_label in blocks[block_index].successors:
                successor = index_by_label.get(successor_label)
                if successor is None or successor in root_indices:
                    continue
                previous = incoming_bindings[successor]
                if previous is None:
                    joined = dict(outgoing)
                else:
                    joined = {
                        variable: script
                        for variable, script in previous.items()
                        if outgoing.get(variable) == script
                    }
                if joined != previous:
                    incoming_bindings[successor] = joined
                    worklist.append(successor)

        result_hints: dict[int, tuple[int, int]] = dict(
            hints.get(result.source_path, {})
        )
        for block_index, block in enumerate(blocks):
            current_bindings = dict(incoming_bindings[block_index] or {})
            for statement in block.statements:
                text = statement.text
                load = SCRIPT_LOAD_RE.search(text)
                loaded_variable: str | None = None
                if load is not None:
                    loaded_script = _script_from_load(load)
                    variable = load.group("variable")
                    loaded_variable = variable
                    if loaded_script is None:
                        current_bindings.pop(variable, None)
                    else:
                        current_bindings[variable] = loaded_script

                for variable, loaded_script in tuple(current_bindings.items()):
                    target_module = project.modules.get(loaded_script)
                    if target_module is None or loaded_script == script_name:
                        continue
                    pattern = re.compile(rf"\b{re.escape(variable)}\s*\(")
                    for call_match in pattern.finditer(text):
                        opening = text.find("(", call_match.start())
                        closing = _matching_parenthesis(text, opening)
                        if closing is None:
                            continue
                        arguments = split_arguments(text[opening + 1:closing])
                        resolved = _resolve_target(target_module, arguments)
                        if resolved is None:
                            continue
                        target, _ = resolved
                        call_lines = [
                            line
                            for line in statement.source_lines
                            if instructions.get(line) is not None
                            and instructions[line].opcode == "call"
                        ]
                        if len(call_lines) != 1:
                            continue
                        selector_count = 1 if target_module.dispatch else 0
                        result_hints[call_lines[0]] = (
                            target.argument_count + selector_count,
                            target.result_count,
                        )

                stripped = text.strip()
                for variable in tuple(current_bindings):
                    assignment = re.match(
                        rf"^(?:BPDProgramEntry\s+)?{re.escape(variable)}\s*=",
                        stripped,
                    )
                    if assignment is not None and variable != loaded_variable:
                        current_bindings.pop(variable, None)
        if result_hints:
            hints[result.source_path] = result_hints
    return hints


def _rewrite_calls(
    line: str,
    variable: str,
    module: ProjectModule,
) -> tuple[str, set[str], int]:
    pattern = re.compile(rf"\b{re.escape(variable)}\s*\(")
    replacements: list[tuple[int, int, str]] = []
    referenced: set[str] = set()
    for match in pattern.finditer(line):
        opening = line.find("(", match.start())
        closing = _matching_parenthesis(line, opening)
        if closing is None:
            continue
        arguments = split_arguments(line[opening + 1:closing])
        resolved = _resolve_target(module, arguments)
        if resolved is None:
            continue
        target, call_arguments = resolved
        call_arguments = _coerce_pointer_arguments(target, call_arguments)
        replacements.append(
            (
                match.start(),
                closing + 1,
                f"{target.name}({', '.join(call_arguments)})",
            )
        )
        referenced.add(target.name)
    for start, end, replacement in reversed(replacements):
        line = line[:start] + replacement + line[end:]
    return line, referenced, len(replacements)


def _insert_external_prototypes(source: str, functions: list[ProjectFunction]) -> str:
    if not functions:
        return source
    lines = source.splitlines()
    insertion = next(
        (index for index, line in enumerate(lines) if line.startswith("static ")),
        len(lines),
    )
    prototypes: list[str] = []
    if (
        any("BPDEntryPoint" in function.prototype for function in functions)
        and "typedef intptr_t (*BPDEntryPoint)();" not in source
    ):
        prototypes.extend([
            "typedef intptr_t (*BPDEntryPoint)();",
            "",
        ])
    prototypes.append("/* 已解析的跨文件 BPD 调用。 */")
    prototypes.extend(f"extern {function.prototype}" for function in functions)
    prototypes.append("")
    lines[insertion:insertion] = prototypes
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _export_functions(source: str, names: set[str]) -> str:
    for name in sorted(names):
        source = re.sub(
            rf"(?m)^static\s+(?=[^\n]*\b{re.escape(name)}\s*\()",
            "",
            source,
        )
    return source


def resolve_cross_file_calls(results: list[ProjectResult], index: BpdProjectIndex | None = None) -> int:
    """Resolve load-current-object calls to dispatch targets found in this batch."""
    if len(results) < 2:
        return 0
    project = index or scan_bpd_project(results)
    references_by_result: dict[int, dict[str, ProjectFunction]] = {}
    exports_by_script: dict[str, set[str]] = {}
    resolved_count = 0

    for result_index, result in enumerate(results):
        source = result.c_source
        load_candidates: dict[str, set[str]] = {}
        for match in SCRIPT_LOAD_RE.finditer(source):
            script_name = _script_from_load(match)
            if script_name is not None:
                load_candidates.setdefault(match.group("variable"), set()).add(script_name)

        current_bindings: dict[str, str] = {}
        fresh_bindings: set[str] = set()
        references: dict[str, ProjectFunction] = {}
        rendered_lines: list[str] = []
        for line in source.splitlines():
            load = SCRIPT_LOAD_RE.search(line)
            if load is not None:
                script_name = _script_from_load(load)
                variable = load.group("variable")
                if script_name is None:
                    current_bindings.pop(variable, None)
                    fresh_bindings.discard(variable)
                else:
                    current_bindings[variable] = script_name
                    fresh_bindings.add(variable)

            for variable, script_name in tuple(current_bindings.items()):
                if len(load_candidates.get(variable, ())) > 1 and variable not in fresh_bindings:
                    continue
                module = project.modules.get(script_name)
                if module is None or script_name == normalize_script_name(result.source_path.stem):
                    continue
                line, referenced_names, replacement_count = _rewrite_calls(
                    line,
                    variable,
                    module,
                )
                for name in referenced_names:
                    function = module.functions[name]
                    references[name] = function
                    exports_by_script.setdefault(script_name, set()).add(name)
                resolved_count += replacement_count

            stripped = line.strip()
            for variable in tuple(current_bindings):
                assignment = re.match(
                    rf"^(?:BPDProgramEntry\s+)?{re.escape(variable)}\s*=",
                    stripped,
                )
                if assignment is not None and (load is None or variable != load.group("variable")):
                    current_bindings.pop(variable, None)
                    fresh_bindings.discard(variable)
            if CONTROL_BOUNDARY_RE.match(stripped):
                fresh_bindings.clear()
            rendered_lines.append(line)

        result.c_source = "\n".join(rendered_lines) + ("\n" if source.endswith("\n") else "")
        references_by_result[result_index] = references

    for result_index, result in enumerate(results):
        references = sorted(references_by_result[result_index].values(), key=lambda item: item.name)
        result.c_source = _insert_external_prototypes(result.c_source, references)
        script_name = normalize_script_name(result.source_path.stem)
        result.c_source = _export_functions(result.c_source, exports_by_script.get(script_name, set()))
    return resolved_count
