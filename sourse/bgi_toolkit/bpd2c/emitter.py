from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Callable

from .asm_parser import split_arguments
from .resource_semantics import optimize_binary_resource_source
from .cfg import ControlFlowGraph, FunctionOwnership, compute_function_ownership
from .ir import BasicBlock, Effect, Module, Statement, StatementKind, ValueType
from .result_semantics import MULTI_RESULT_SCHEMAS, result_declarations
from .structure_semantics import (
    structure_declarations,
    structure_pointer_field,
    structure_pointer_type,
)


LOCAL_RE = re.compile(r"\blocal_[0-9a-fA-F]+\b")
VARIABLE_RE = re.compile(
    r"\b(?:local_[0-9a-fA-F]+|result_L\d+(?:_\d+)?|phi_\d+|edge_phi_\d+)\b"
)
SCALAR_ARITHMETIC_VARIABLE_RE = re.compile(
    r"(?:"
    r"(?P<left>local_[0-9a-fA-F]+|result_L\d+(?:_\d+)?|phi_\d+|edge_phi_\d+)"
    r"\s+(?:\*|/|%|&|\||\^|<<|>>)\s+"
    r"|(?:\*|/|%|&|\||\^|<<|>>)\s+"
    r"(?P<right>local_[0-9a-fA-F]+|result_L\d+(?:_\d+)?|phi_\d+|edge_phi_\d+)"
    r")"
)
PARAM_ASSIGN_RE = re.compile(r"^(local_[0-9a-fA-F]+) = vm_pop\(vm\);$")
PARAM_VM_ARG_RE = re.compile(r"^.+ = vm_arg\(vm, (?P<index>\d+)\);$")
SCRIPT_ENTRY_ASSIGN_RE = re.compile(
    r"\b(?P<name>local_[0-9a-fA-F]+)\s*=\s*sys1_load_program\("
    r"(?P<args>.*)\);$"
)
SCRIPT_NAME_RE = re.compile(r'"(?P<name>[^"\\/]+)\._bp"')
VARIABLE_ASSIGN_RE = re.compile(
    r"^(?P<name>local_[0-9a-fA-F]+|result_L\d+(?:_\d+)?|phi_\d+|edge_phi_\d+)\s*=\s*(?P<value>.*);$"
)
MATERIALIZED_CALL_RESULT_RE = re.compile(
    r"^(?P<name>result_L\d+(?:_\d+)?)\s*=\s*(?P<value>.*);$"
)
MATERIALIZED_CALL_RESULT_NAME_RE = re.compile(r"\bresult_L\d+(?:_\d+)?\b")
STRUCTURE_POINTER_RE = re.compile(r"^&(?P<array>[A-Za-z_][A-Za-z0-9_]*)\[")
TYPED_POINTER_STORE_RE = re.compile(
    r"^store_(?P<width>i8|i16|i32|i64)\(\((?P<name>local_[0-9a-fA-F]+|result_L\d+|phi_\d+)\), (?P<value>.*)\);$"
)
TYPED_POINTER_LOAD_RE = re.compile(
    r"^load_(?P<width>i8|i16|i32|i64)\(\((?P<name>local_[0-9a-fA-F]+|result_L\d+|phi_\d+)\)\)$"
)
RESULT_SCHEMA_RE = re.compile(
    r"\b(?P<type>BPD[A-Za-z0-9_]+)\s+(?P<name>result_L\d+)\s*="
)
INLINE_RESULT_DECLARATION_RE = re.compile(
    r"^(?:u?int(?:8|16|32|64)_t|intptr_t|BPD[A-Za-z0-9_]+)"
    r"\s+(?P<name>result_L\d+(?:_\d+)?)\s*="
)
FUNCTION_TABLE_TARGET_RE = re.compile(r"^&(?P<name>local_[0-9A-Fa-f]+)$")
FUNCTION_TABLE_ENTRY_RE = re.compile(r"^label_ref\((?P<label>[A-Za-z_][A-Za-z0-9_]*)\)$")
DIRECT_LABEL_CALL_WITH_ARGS_RE = re.compile(
    r"call_direct\(label_ref\((?P<label>[A-Za-z_]\w*)\), "
)
DIRECT_LABEL_CALL_RE = re.compile(
    r"call_direct\(label_ref\((?P<label>[A-Za-z_]\w*)\)\)"
)
LABEL_REF_RE = re.compile(r"label_ref\((?P<label>[A-Za-z_]\w*)\)")
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CALL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\(")
LOAD_WIDTH_RE = re.compile(r"load_(i8|i16|i32|i64)")
STORE_WIDTH_RE = re.compile(r"store_(i8|i16|i32|i64)")
INDEXED_VM_WORD_CALL_RE = re.compile(
    r"\((?P<callee>(?P<base>local_[0-9A-Fa-f]+|result_L\d+(?:_\d+)?|"
    r"phi_\d+|edge_phi_\d+)\s*\[[^\[\]]+\])\)(?=\s*\()"
)
BARE_INDEXED_VM_WORD_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<callee>(?P<base>local_[0-9A-Fa-f]+|"
    r"result_L\d+(?:_\d+)?|phi_\d+|edge_phi_\d+)"
    r"\s*\[[^\[\]]+\])(?=\s*\()"
)
GLOBAL_VM_WORD_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<callee>global_(?:i32_)?[0-9A-Fa-f]+)(?=\s*\()"
)
PAREN_GLOBAL_I32_CALL_RE = re.compile(
    r"\((?P<callee>global_i32_[0-9A-Fa-f]+)\)(?=\s*\()"
)
LOCAL_ADDRESS_ARITHMETIC_RE = re.compile(
    r"&(?P<base>local_[0-9A-Fa-f]+)(?P<index>\[[^\[\]]+\])?"
)
CAST_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*\s*\*\)\s*\(?$")
LOCAL_ARRAY_ASSIGNMENT_RE = re.compile(
    r"^(?P<base>local_[0-9A-Fa-f]+)\[.+\]\s*=\s*(?P<value>.*);$"
)
LOCAL_ARRAY_TARGET_RE = re.compile(
    r"^&(?P<base>local_[0-9a-fA-F]+)(?P<tail>.*)$"
)
LOCAL_INDEXED_TARGET_RE = re.compile(
    r"^(?P<address>&?)(?P<base>local_[0-9a-fA-F]+)(?P<tail>.*)$"
)
VALUE_INDEXED_TARGET_RE = re.compile(
    r"^(?P<address>&?)(?P<base>(?:local_[0-9a-fA-F]+|result_L\d+(?:_\d+)?|phi_\d+|edge_phi_\d+))"
    r"(?P<tail>.*)$"
)
VM_ARGUMENT_RE = re.compile(r"vm_arg\(vm, (?P<index>\d+)\)")
VM_PUSH_RE = re.compile(r"^vm_push\(vm, (?P<value>.*)\);$")
RENDERED_STACK_PUSH_RE = re.compile(
    r"^(?P<indent>\s*)stack_push\((?P<value>.*)\);"
    r"(?P<source>\s*/\* BPD .*? \*/)?\s*$"
)
RENDERED_SOURCE_COMMENT_RE = re.compile(r"\s*/\* BPD (?P<source>.*?) \*/\s*$")
SAFE_RENDERED_STACK_VALUE_RE = re.compile(
    r"(?:"
    r"&?[A-Za-z_]\w*(?:\[\s*(?:0[xX][0-9A-Fa-f]+|\d+)\s*\])?"
    r"|-?(?:0[xX][0-9A-Fa-f]+|\d+)|true|false|NULL"
    r")"
    r"(?:\s*[+-]\s*(?:&?[A-Za-z_]\w*|-?(?:0[xX][0-9A-Fa-f]+|\d+)))?"
)
INDIRECT_DISPATCH_RE = re.compile(
    r"^\(load_indirect_indexed\((?P<base>-?(?:0[xX][0-9A-Fa-f]+|\d+)), "
    r"(?P<index>-?(?:0[xX][0-9A-Fa-f]+|\d+)), 4\)\)\((?P<args>.*)\);$"
)

LOCAL_EXACT_RE = re.compile(r"^local_[0-9A-Fa-f]+$")
VM_ARG_EXACT_RE = re.compile(r"^vm_arg\(vm, (?P<index>\d+)\)$")
ENTRY_PARAMETER_PRELUDE_RE = re.compile(r"^(?:memclr|memset)\(")

STRING_ARGUMENT_TYPES: dict[str, dict[int, str]] = {
    "streq": {0: "const char *", 1: "const char *"},
    "bpd_strstr_index": {0: "const char *", 1: "const char *"},
    "strlen": {0: "const char *"},
    "sprintf": {0: "char *", 1: "const char *"},
    "message": {0: "const char *"},
    "confirm": {0: "const char *"},
    "sys1_set_main_window_title": {0: "const char *"},
    "strlower_inplace": {0: "char *"},
    "strcpy": {0: "char *", 1: "const char *"},
    "strreplace": {
        0: "char *",
        1: "const char *",
        2: "const char *",
        3: "const char *",
    },
    "sys1_split_path_components": {
        2: "int32_t *",
        3: "int32_t *",
        4: "const char *",
    },
    "memory_equals": {0: "const uint8_t *", 1: "const uint8_t *"},
    "memcpy": {0: "uint8_t *", 1: "const uint8_t *"},
    "memclr": {0: "uint8_t *"},
    "memset": {0: "uint8_t *"},
    "memcpy_repeat": {0: "uint8_t *", 3: "const uint8_t *"},
}

WRITABLE_STRING_ARGUMENTS: dict[str, tuple[int, ...]] = {
    "sprintf": (0,),
    "strcpy": (0,),
    "strlower_inplace": (0,),
    "strreplace": (0,),
}


def merge_c_types(types: list[str] | tuple[str, ...] | set[str]) -> str | None:
    """Join C-level constraints while preserving string/pointer intent."""
    values = {value for value in types if value}
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values))
    if values <= {"char *", "const char *"}:
        return "char *" if "char *" in values else "const char *"
    if all(value.endswith("*") for value in values):
        if "char *" in values and values <= {
            "char *",
            "const char *",
            "uint8_t *",
            "const uint8_t *",
        }:
            return "char *"
        if "const char *" in values and values <= {
            "const char *",
            "uint8_t *",
            "const uint8_t *",
        }:
            return "const char *"
        specific = values - {"uint8_t *"}
        if len(specific) == 1:
            return next(iter(specific))
        if "const char *" in values:
            return "const char *"
        return "uint8_t *"
    numeric = {"int8_t", "int16_t", "int32_t", "int64_t"}
    if values <= numeric:
        return max(values, key={"int8_t": 8, "int16_t": 16, "int32_t": 32, "int64_t": 64}.get)
    return None


def value_type_to_c_type(value_type: ValueType) -> str | None:
    return {
        ValueType.BOOL: "int32_t",
        ValueType.I8: "int8_t",
        ValueType.I16: "int16_t",
        ValueType.I32: "int32_t",
        ValueType.I64: "int64_t",
        ValueType.POINTER: "uint8_t *",
        ValueType.STRING: "const char *",
        ValueType.LABEL: "BPDEntryPoint",
    }.get(value_type)


def sanitize_identifier(text: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_]", "_", text)
    if not candidate or candidate[0].isdigit():
        candidate = "bpd_" + candidate
    return candidate


def format_source_lines(lines: tuple[int, ...]) -> str:
    if not lines:
        return ""
    ordered = tuple(dict.fromkeys(lines))
    if len(ordered) == 1:
        return f" /* BPD L{ordered[0]} */"
    if tuple(sorted(ordered)) == tuple(range(min(ordered), max(ordered) + 1)):
        return f" /* BPD L{min(ordered)}-L{max(ordered)} */"
    return " /* BPD " + ", ".join(f"L{line}" for line in ordered) + " */"


def fold_adjacent_rendered_stack_operations(lines: list[str]) -> list[str]:
    """Inline an adjacent push run into the immediately following pops.

    The renderer can leave a short stack protocol when a value crosses a
    block boundary.  If the producer and its first consumer are adjacent in
    the rendered block, the suffix of the consumer's reads belongs to that
    producer run: older values are read first, then the last pushed value.
    Replacing only that suffix preserves an older caller-supplied stack value
    when the consumer reads more values than this block produced.
    """
    folded: list[str] = []
    index = 0
    while index < len(lines):
        run_end = index
        pushes: list[re.Match[str]] = []
        while run_end < len(lines):
            pushed = RENDERED_STACK_PUSH_RE.fullmatch(lines[run_end])
            if pushed is None:
                break
            pushes.append(pushed)
            run_end += 1
        if not pushes or run_end >= len(lines):
            folded.append(lines[index])
            index += 1
            continue

        following = lines[run_end]
        pop_matches = list(re.finditer(r"stack_pop\(\)", following))
        if len(pop_matches) < len(pushes):
            folded.append(lines[index])
            index += 1
            continue

        values = [pushed.group("value").strip() for pushed in pushes]
        indent = pushes[0].group("indent")
        following_indent = following[: len(following) - len(following.lstrip())]
        single_value_direct_use = len(pushes) == 1 and len(pop_matches) == 1
        if (
            following_indent != indent
            or any(pushed.group("indent") != indent for pushed in pushes)
            or (
                not single_value_direct_use
                and any(SAFE_RENDERED_STACK_VALUE_RE.fullmatch(value) is None for value in values)
            )
        ):
            folded.append(lines[index])
            index += 1
            continue

        # The first rendered read represents the oldest value in the call's
        # argument list, so the producer run maps to the final reads in push
        # order.  Replace from right to left to keep offsets stable.
        following_source = RENDERED_SOURCE_COMMENT_RE.search(following)
        following_code = (
            following[: following_source.start()].rstrip()
            if following_source is not None
            else following.rstrip()
        )
        selected = pop_matches[-len(values) :]
        replacement_parts: list[str] = []
        cursor = 0
        for match, value in zip(selected, values):
            replacement_parts.append(following_code[cursor:match.start()])
            replacement_parts.append(value)
            cursor = match.end()
        replacement_parts.append(following_code[cursor:])
        replacement = "".join(replacement_parts)
        sources = [
            match.group("source")
            for match in (
                *(RENDERED_SOURCE_COMMENT_RE.search(line) for line in lines[index:run_end]),
                following_source,
            )
            if match is not None
        ]
        if sources:
            replacement += " /* BPD " + ", ".join(dict.fromkeys(sources)) + " */"
        folded.append(replacement)
        index = run_end + 1
    return folded


def lower_dead_rendered_stack_pushes(lines: list[str]) -> list[str]:
    """Discard residual stack values after all rendered consumers are folded."""
    if any("stack_pop()" in line for line in lines):
        return lines
    lowered: list[str] = []
    for line in lines:
        pushed = RENDERED_STACK_PUSH_RE.fullmatch(line)
        if pushed is None:
            lowered.append(line)
            continue
        source = pushed.group("source") or ""
        lowered.append(
            f"{pushed.group('indent')}(void)({pushed.group('value').strip()});{source}"
        )
    return lowered


RENDERED_IF_GOTO_RE = re.compile(
    r"^(?P<indent>\s*)if \((?P<condition>.*)\) goto (?P<target>L[A-Za-z0-9_]+);"
    r"(?P<source>\s*/\* BPD .*? \*/)?\s*$"
)
RENDERED_GOTO_RE = re.compile(
    r"^(?P<indent>\s*)goto (?P<target>L[A-Za-z0-9_]+);"
    r"(?P<source>\s*/\* BPD .*? \*/)?\s*$"
)
RENDERED_LABEL_RE = re.compile(r"^(?P<indent>\s*)(?P<label>L[A-Za-z0-9_]+):\s*$")
RENDERED_LOCAL_DECLARATION_RE = re.compile(
    r"^\s*(?:(?:static|const|volatile)\s+)*"
    r"(?:u?int(?:8|16|32|64)_t|intptr_t|char|BPD[A-Za-z0-9_]+|struct\s+[A-Za-z_]\w*)"
    r"\s+\**[A-Za-z_]\w*"
)
RENDERED_FOR_DECLARATION_RE = re.compile(
    r"\bfor\s*\(\s*(?:(?:const|volatile)\s+)*"
    r"(?:u?int(?:8|16|32|64)_t|intptr_t|char|BPD[A-Za-z0-9_]+)\b"
)
RENDERED_SCALAR_SELF_ASSIGNMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z_]\w*)\s*=\s*(?P=name);$"
)
RENDERED_COMPARISON_RE = re.compile(
    # Do not interpret the '>' in a rendered pointer member access ("->")
    # as the comparison being negated.  The non-greedy left operand makes
    # that otherwise look like ``pointer- > member != value``.
    r"(.+?)\s*(==|!=|<=|>=|(?<![<])<(?![<])|(?<![-=>])>(?![=>]))\s*(.+)"
)


def fold_redundant_rendered_gotos(lines: list[str]) -> list[str]:
    """Drop a conditional or unconditional jump to the next rendered label."""
    folded: list[str] = []
    for index, line in enumerate(lines):
        match = RENDERED_IF_GOTO_RE.fullmatch(line) or RENDERED_GOTO_RE.fullmatch(line)
        if match is None:
            folded.append(line)
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines) or lines[next_index].strip() != f"{match.group('target')}:":
            folded.append(line)
            continue
        # Keep source traceability without turning the label into a non-label
        # line (the label audit intentionally recognizes bare labels).
        source = match.group("source")
        if source:
            folded.append(f"{match.group('indent')}{source.strip()}")
        continue

    referenced = set(re.findall(r"\bgoto\s+(L[A-Za-z0-9_]+)\s*;", "\n".join(folded)))
    result: list[str] = []
    for line in folded:
        label = re.fullmatch(r"\s*(L[A-Za-z0-9_]+):\s*", line)
        if label is None or label.group(1) in referenced:
            result.append(line)
    return result


def fold_single_entry_rendered_guards(lines: list[str]) -> list[str]:
    """Turn a single-entry forward conditional jump into an inverted if body."""
    folded = list(lines)
    while True:
        target_counts = Counter(
            re.findall(r"\bgoto\s+(L[A-Za-z0-9_]+)\s*;", "\n".join(folded))
        )
        label_indices: dict[str, list[int]] = {}
        for index, line in enumerate(folded):
            label = RENDERED_LABEL_RE.fullmatch(line)
            if label is not None:
                label_indices.setdefault(label.group("label"), []).append(index)

        replacement: tuple[int, int, list[str]] | None = None
        for index, line in enumerate(folded):
            guard = RENDERED_IF_GOTO_RE.fullmatch(line)
            if guard is None:
                continue
            targets = label_indices.get(guard.group("target"), [])
            if target_counts[guard.group("target")] != 1 or len(targets) != 1:
                continue
            target_index = targets[0]
            if target_index <= index + 1:
                continue
            target_label = RENDERED_LABEL_RE.fullmatch(folded[target_index])
            if target_label is None or target_label.group("indent") != guard.group("indent"):
                continue

            body = folded[index + 1:target_index]
            if not any(line.strip() for line in body):
                continue
            if any(RENDERED_LABEL_RE.fullmatch(line) for line in body):
                continue
            if any(
                RENDERED_LOCAL_DECLARATION_RE.match(line)
                or RENDERED_FOR_DECLARATION_RE.search(line)
                for line in body
            ):
                continue
            # The body may contain complete nested constructs, but it must
            # not begin/end inside one.  Folding an unmatched ``} while``
            # into the new guard produced malformed C in the bp6
            # logwnd/recwnd/usdtwnd cleanup paths.
            brace_depth = 0
            structurally_balanced = True
            for body_line in body:
                brace_depth += body_line.count("{")
                brace_depth -= body_line.count("}")
                if brace_depth < 0:
                    structurally_balanced = False
                    break
            if not structurally_balanced or brace_depth:
                continue

            indent = guard.group("indent")
            source = guard.group("source") or ""
            structured = [
                f"{indent}if ({negate_condition(guard.group('condition'))}) {{{source}"
            ]
            structured.extend(f"    {line}" if line.strip() else line for line in body)
            structured.append(f"{indent}}}")
            replacement = (index, target_index + 1, structured)
            break

        if replacement is None:
            return folded
        start, end, structured = replacement
        folded[start:end] = structured


def legalize_rendered_labels(lines: list[str]) -> list[str]:
    """Ensure every C label is followed by a statement under C11 grammar."""
    legalized: list[str] = []
    for index, line in enumerate(lines):
        legalized.append(line)
        label = RENDERED_LABEL_RE.fullmatch(line)
        if label is None:
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines):
            needs_statement = True
        else:
            following = lines[next_index]
            needs_statement = (
                following.strip() == "}"
                or RENDERED_LOCAL_DECLARATION_RE.match(following) is not None
            )
        if needs_statement:
            legalized.append(f"{label.group('indent')}    ;")
    return legalized


def clarify_rendered_control_expressions(lines: list[str]) -> list[str]:
    """Apply expression clarity rules after goto/guard text folding."""
    clarified: list[str] = []
    control = re.compile(r"\b(?:if|while)\s*\(")
    for line in lines:
        match = control.search(line)
        if match is None:
            clarified.append(line)
            continue
        opening = line.find("(", match.start())
        depth = 1
        quote: str | None = None
        closing: int | None = None
        index = opening + 1
        while index < len(line):
            char = line[index]
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
                    closing = index
                    break
            index += 1
        if closing is None:
            clarified.append(line)
            continue
        condition = strip_outer_parentheses(
            clarify_ambiguous_expression(line[opening + 1:closing])
        )
        clarified.append(line[: opening + 1] + condition + line[closing:])
    return clarified


def split_top_level_boolean(text: str, operator: str) -> list[str]:
    if operator not in text:
        return []
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index <= len(text) - len(operator):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(operator, index):
            parts.append(text[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    if parts:
        parts.append(text[start:].strip())
    return parts


def strip_outer_parentheses(text: str) -> str:
    result = text.strip()
    while result.startswith("(") and result.endswith(")"):
        depth = 0
        encloses_all = True
        for index, char in enumerate(result):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(result) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        result = result[1:-1].strip()
    return result


def split_top_level_ternary(text: str) -> tuple[str, str, str] | None:
    """Split a C conditional expression without matching nested calls/strings."""
    source = strip_outer_parentheses(text)
    depth = 0
    quote: str | None = None
    question: int | None = None
    nested_questions = 0
    index = 0
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and char == "?":
            if question is None:
                question = index
            else:
                nested_questions += 1
        elif depth == 0 and char == ":" and question is not None:
            if nested_questions:
                nested_questions -= 1
            else:
                return (
                    source[:question].strip(),
                    source[question + 1:index].strip(),
                    source[index + 1:].strip(),
                )
        index += 1
    return None


_C_BINARY_PRECEDENCE = {
    "=": 1,
    "?": 5,
    ":": 5,
    "||": 10,
    "&&": 20,
    "|": 30,
    "^": 35,
    "&": 40,
    "==": 45,
    "!=": 45,
    "<": 45,
    "<=": 45,
    ">": 45,
    ">=": 45,
    "<<": 50,
    ">>": 50,
    "+": 60,
    "-": 60,
    "*": 70,
    "/": 70,
    "%": 70,
}


def _top_level_binary_operators(text: str) -> list[tuple[int, str, int]]:
    operators: list[tuple[int, str, int]] = []
    depth = 0
    quote: str | None = None
    index = 0
    previous_token = ""
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char in "([{":
            depth += 1
            previous_token = char
            index += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            previous_token = char
            index += 1
            continue
        if depth or char.isspace():
            index += 1
            continue
        if text.startswith("->", index):
            previous_token = ">"
            index += 2
            continue

        operator = next(
            (
                candidate
                for candidate in ("||", "&&", "==", "!=", "<=", ">=", "<<", ">>")
                if text.startswith(candidate, index)
            ),
            char if char in "=?:|^&<>+-*/%" else None,
        )
        if operator is None:
            previous_token = char
            index += 1
            continue
        unary = operator in {"+", "-", "*", "&"} and (
            not previous_token or previous_token in "([{,?:=+-*/%!~&|^<>"
        )
        if not unary:
            operators.append((index, operator, _C_BINARY_PRECEDENCE[operator]))
        previous_token = operator[-1]
        index += len(operator)
    return operators


def _wrap_expression_slice(text: str, start: int, end: int) -> str:
    fragment = text[start:end]
    leading = fragment[: len(fragment) - len(fragment.lstrip())]
    trailing = fragment[len(fragment.rstrip()):]
    core = fragment.strip()
    if not core or strip_outer_parentheses(core) != core:
        return text
    return text[:start] + leading + f"({core})" + trailing + text[end:]


def _clarify_shift_precedence(text: str) -> str:
    if "<<" not in text and ">>" not in text:
        return text
    for _ in range(32):
        operators = _top_level_binary_operators(text)
        changed = False
        for position, (offset, operator, precedence) in enumerate(operators):
            if operator not in {"<<", ">>"}:
                continue
            left_start = 0
            for previous_offset, previous_operator, previous_precedence in operators[:position]:
                if previous_precedence <= precedence:
                    left_start = previous_offset + len(previous_operator)
            right_end = len(text)
            for next_offset, _, next_precedence in operators[position + 1:]:
                if next_precedence <= precedence:
                    right_end = next_offset
                    break
            left = text[left_start:offset]
            right = text[offset + len(operator):right_end]
            if any(item[2] > precedence for item in _top_level_binary_operators(left)):
                updated = _wrap_expression_slice(text, left_start, offset)
                if updated != text:
                    text = updated
                    changed = True
                    break
            if any(item[2] > precedence for item in _top_level_binary_operators(right)):
                updated = _wrap_expression_slice(
                    text,
                    offset + len(operator),
                    right_end,
                )
                if updated != text:
                    text = updated
                    changed = True
                    break
        if not changed:
            return text
    return text


@lru_cache(maxsize=65536)
def clarify_ambiguous_expression(text: str) -> str:
    """为易误读的 C 表达式补充括号。"""
    rendered: list[str] = []
    index = 0
    while index < len(text):
        if text[index] in {'"', "'"}:
            quote = text[index]
            end = index + 1
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    break
                end += 1
            rendered.append(text[index:end])
            index = end
            continue
        if text[index] != "(":
            rendered.append(text[index])
            index += 1
            continue
        depth = 1
        end = index + 1
        quote: str | None = None
        while end < len(text) and depth:
            char = text[end]
            if quote is not None:
                if char == "\\":
                    end += 2
                    continue
                if char == quote:
                    quote = None
            elif char in {'"', "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            end += 1
        if depth:
            rendered.append(text[index:])
            break
        inner = text[index + 1:end - 1]
        parts = split_arguments(inner)
        if len(parts) > 1:
            inner = ", ".join(clarify_ambiguous_expression(part) for part in parts)
        elif re.fullmatch(
            r"\s*(?:const\s+)?(?:struct\s+)?[A-Za-z_]\w*"
            r"(?:\s+[A-Za-z_]\w*)*\s*\*+\s*",
            inner,
        ) is None:
            inner = clarify_ambiguous_expression(inner)
        rendered.append(f"({inner})")
        index = end

    result = _clarify_shift_precedence("".join(rendered))
    disjunction = split_top_level_boolean(result, " || ")
    if disjunction:
        result = " || ".join(
            (
                f"({part.strip()})"
                if split_top_level_boolean(part, " && ")
                and strip_outer_parentheses(part) == part.strip()
                else part.strip()
            )
            for part in disjunction
        )
    return result


@lru_cache(maxsize=65536)
def constant_integer(text: str) -> int | None:
    try:
        root = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None

    def evaluate(node: ast.expr) -> int | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            if value is None:
                return None
            return value if isinstance(node.op, ast.UAdd) else -value
        if not isinstance(node, ast.BinOp):
            return None
        left = evaluate(node.left)
        right = evaluate(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.LShift) and right >= 0:
            return left << right
        if isinstance(node.op, ast.RShift) and right >= 0:
            return left >> right
        return None

    return evaluate(root)


def _additive_terms(node: ast.expr, sign: int = 1) -> list[tuple[int, ast.expr]]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _additive_terms(node.left, sign) + _additive_terms(node.right, sign)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        return _additive_terms(node.left, sign) + _additive_terms(node.right, -sign)
    return [(sign, node)]


def _scaled_index_term(node: ast.expr, divisor: int) -> str | None:
    if divisor == 1:
        return ast.unparse(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = constant_integer(ast.unparse(node.left))
        right = constant_integer(ast.unparse(node.right))
        if left is not None and left % divisor == 0:
            factor = left // divisor
            value = ast.unparse(node.right)
        elif right is not None and right % divisor == 0:
            factor = right // divisor
            value = ast.unparse(node.left)
        else:
            return None
        if factor == 1:
            return value
        return f"{value} * {factor}"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.LShift):
        shift = constant_integer(ast.unparse(node.right))
        if shift is None or shift < 0:
            return None
        factor = (1 << shift) // divisor
        if factor * divisor != 1 << shift:
            return None
        value = ast.unparse(node.left)
        if factor == 1:
            return value
        return f"{value} * {factor}"
    return None


def divide_address_expression(text: str, divisor: int) -> str | None:
    expression = text.strip()
    if not expression:
        return "0"
    try:
        root = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None

    constant = 0
    terms: list[tuple[int, str]] = []
    for sign, node in _additive_terms(root):
        value = constant_integer(ast.unparse(node))
        if value is not None:
            constant += sign * value
            continue
        scaled = _scaled_index_term(node, divisor)
        if scaled is None:
            return None
        terms.append((sign, scaled))
    if constant % divisor != 0:
        return None
    constant //= divisor
    if constant:
        terms.insert(0, (1 if constant > 0 else -1, str(abs(constant))))
    if not terms:
        return "0"

    rendered = ""
    for position, (sign, value) in enumerate(terms):
        if position == 0:
            rendered = value if sign > 0 else f"-{value}"
        else:
            rendered += f" {'+' if sign > 0 else '-'} {value}"
    return rendered


def integer_pointer_index(text: str, divisor: int) -> tuple[str, str] | None:
    """Split ``integer_pointer + byte_offset`` into a typed array view."""
    try:
        root = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None

    base: str | None = None
    constant = 0
    indices: list[tuple[int, str]] = []
    for sign, node in _additive_terms(root):
        if (
            base is None
            and sign > 0
            and isinstance(node, (ast.Name, ast.Subscript, ast.Call))
        ):
            base = ast.unparse(node)
            continue
        value = constant_integer(ast.unparse(node))
        if value is not None:
            constant += sign * value
            continue
        scaled = _scaled_index_term(node, divisor)
        if scaled is None:
            return None
        indices.append((sign, scaled))

    if base is None or constant % divisor:
        return None
    constant //= divisor
    if constant:
        indices.insert(0, (1 if constant > 0 else -1, str(abs(constant))))
    if not indices:
        return base, "0"

    rendered = ""
    for position, (sign, value) in enumerate(indices):
        if position == 0:
            rendered = value if sign > 0 else f"-{value}"
        else:
            rendered += f" {'+' if sign > 0 else '-'} {value}"
    return base, rendered


def absolute_pointer_index(text: str, divisor: int) -> tuple[int, str] | None:
    """Split an absolute base plus scaled offsets into a named global array."""
    try:
        root = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None

    base = 0
    saw_constant = False
    indices: list[tuple[int, str]] = []
    for sign, node in _additive_terms(root):
        value = constant_integer(ast.unparse(node))
        if value is not None:
            base += sign * value
            saw_constant = True
            continue
        scaled = _scaled_index_term(node, divisor)
        if scaled is None:
            return None
        indices.append((sign, scaled))
    if not saw_constant or base < 0 or not indices:
        return None

    rendered = ""
    for position, (sign, value) in enumerate(indices):
        if position == 0:
            rendered = value if sign > 0 else f"-{value}"
        else:
            rendered += f" {'+' if sign > 0 else '-'} {value}"
    return base, rendered


def local_indexed_address(
    address: str,
    element_size: int,
    *,
    allow_bare: bool = False,
) -> tuple[str, str] | None:
    target = strip_outer_parentheses(address)
    match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
    if match is None or (not match.group("address") and not allow_bare):
        return None
    tail = match.group("tail").strip()
    leading_subscript = re.match(
        r"^\[(?P<index>[^\[\]]+)\](?P<rest>.*)$",
        tail,
    )
    if leading_subscript is not None:
        index = leading_subscript.group("index").strip()
        rest = leading_subscript.group("rest").strip()
        if not rest:
            return match.group("base"), index
        if not rest.startswith(("+", "-")):
            return None
        operator = rest[0]
        extra = divide_address_expression(rest[1:].strip(), element_size)
        if extra is None:
            return None
        if extra == "0":
            return match.group("base"), index
        combined = f"{index} {operator} {extra}"
        return match.group("base"), combined
    if tail.startswith("+"):
        tail = tail[1:].strip()
    index = divide_address_expression(tail, element_size)
    if index is None:
        return None
    return match.group("base"), index


def value_indexed_address(
    address: str,
    element_size: int,
    *,
    allow_bare: bool = False,
) -> tuple[str, str] | None:
    target = strip_outer_parentheses(address)
    match = VALUE_INDEXED_TARGET_RE.fullmatch(target)
    if match is None or (not match.group("address") and not allow_bare):
        return None
    tail = match.group("tail").strip()
    leading_subscript = re.match(
        r"^\[(?P<index>[^\[\]]+)\](?P<rest>.*)$",
        tail,
    )
    if leading_subscript is not None:
        index = leading_subscript.group("index").strip()
        rest = leading_subscript.group("rest").strip()
        if not rest:
            return match.group("base"), index
        if not rest.startswith(("+", "-")):
            return None
        operator = rest[0]
        extra = divide_address_expression(rest[1:].strip(), element_size)
        if extra is None:
            return None
        if extra == "0":
            return match.group("base"), index
        combined = f"{index} {operator} {extra}"
        return match.group("base"), combined
    if tail.startswith("+"):
        tail = tail[1:].strip()
    index = divide_address_expression(tail, element_size)
    if index is None:
        return None
    return match.group("base"), index


def bounded_nonnegative_expression_max(
    text: str,
    index_bounds: dict[str, set[int]],
) -> int | None:
    """Return a conservative maximum for a nonnegative linear offset."""
    try:
        root = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None

    def evaluate(node: ast.AST) -> int | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value if node.value >= 0 else None
        if isinstance(node, ast.Name):
            bounds = index_bounds.get(node.id, set())
            return min(bounds) - 1 if bounds and min(bounds) > 0 else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
            return evaluate(node.operand)
        if not isinstance(node, ast.BinOp):
            return None
        left = evaluate(node.left)
        right = evaluate(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right if left >= right else None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and right:
            return left // right
        if isinstance(node.op, ast.LShift):
            return left << right
        return None

    return evaluate(root)


def nonnegative_expression_stride(text: str) -> int | None:
    """Return the smallest positive byte stride contributed by an index."""
    try:
        root = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None

    def coefficient(node: ast.AST) -> int | None:
        if isinstance(node, ast.Name):
            return 1
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
            return coefficient(node.operand)
        if not isinstance(node, ast.BinOp):
            return None
        if isinstance(node.op, (ast.Add, ast.Sub)):
            values = [value for value in (coefficient(node.left), coefficient(node.right)) if value]
            return min(values) if values else None
        if isinstance(node.op, ast.Mult):
            left_constant = constant_integer(ast.unparse(node.left))
            right_constant = constant_integer(ast.unparse(node.right))
            if left_constant is not None and left_constant > 0:
                nested = coefficient(node.right)
                return nested * left_constant if nested else None
            if right_constant is not None and right_constant > 0:
                nested = coefficient(node.left)
                return nested * right_constant if nested else None
            return None
        if isinstance(node.op, ast.LShift):
            shift = constant_integer(ast.unparse(node.right))
            nested = coefficient(node.left)
            return nested << shift if nested and shift is not None and shift >= 0 else None
        return None

    return coefficient(root)


@lru_cache(maxsize=32768)
def iter_function_calls(text: str) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []

    def visit(fragment: str) -> None:
        index = 0
        while index < len(fragment):
            if fragment[index] in {'"', "'"}:
                quote = fragment[index]
                index += 1
                while index < len(fragment):
                    if fragment[index] == "\\":
                        index += 2
                        continue
                    if fragment[index] == quote:
                        index += 1
                        break
                    index += 1
                continue
            identifier = re.match(r"[A-Za-z_][A-Za-z0-9_]*", fragment[index:])
            if identifier is None:
                index += 1
                continue
            name = identifier.group(0)
            name_end = index + len(name)
            if name_end >= len(fragment) or fragment[name_end] != "(":
                index = name_end
                continue
            depth = 1
            end = name_end + 1
            quote: str | None = None
            while end < len(fragment) and depth:
                char = fragment[end]
                if quote is not None:
                    if char == "\\":
                        end += 2
                        continue
                    if char == quote:
                        quote = None
                elif char in {'"', "'"}:
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                end += 1
            if depth:
                return
            inner = fragment[name_end + 1:end - 1]
            visit(inner)
            calls.append((name, split_arguments(inner)))
            index = end

    visit(text)
    return calls


def parse_memcpy_repeat(text: str) -> tuple[str, str, str, str] | None:
    """Return VM repeat-copy operands as destination, element size, count, source."""
    token = text.strip()
    prefix = "memcpy_repeat("
    if not token.startswith(prefix) or not token.endswith(");"):
        return None
    arguments = split_arguments(token[len(prefix):-2])
    if len(arguments) != 4:
        return None
    destination, element_size, count, source = arguments
    return (
        destination.strip(),
        element_size.strip(),
        count.strip(),
        source.strip(),
    )


def _condition_constant(text: str) -> bool | None:
    token = strip_outer_parentheses(text)
    if token == "true":
        return True
    if token == "false":
        return False
    if token.startswith("!") and not token.startswith("!="):
        inner = _condition_constant(token[1:])
        return None if inner is None else not inner
    value = constant_integer(token)
    return None if value is None else value != 0


def _join_boolean(parts: list[str], operator: str) -> str:
    rendered: list[str] = []
    for part in parts:
        simplified = simplify_condition(part)
        constant = _condition_constant(simplified)
        if operator == " && ":
            if constant is False:
                return "false"
            if constant is True:
                continue
        else:
            if constant is True:
                return "true"
            if constant is False:
                continue
        if operator == " && " and split_top_level_boolean(simplified, " || "):
            simplified = f"({simplified})"
        rendered.append(simplified)
    if not rendered:
        return "true" if operator == " && " else "false"
    return operator.join(rendered)


def _negate_simplified_condition(condition: str) -> str:
    text = strip_outer_parentheses(condition)
    constant = _condition_constant(text)
    if constant is not None:
        return "false" if constant else "true"
    conjunction = split_top_level_boolean(text, " && ")
    if conjunction:
        return " || ".join(_negate_simplified_condition(simplify_condition(part)) for part in conjunction)
    disjunction = split_top_level_boolean(text, " || ")
    if disjunction:
        return " && ".join(_negate_simplified_condition(simplify_condition(part)) for part in disjunction)
    if text.startswith("!") and not text.startswith("!="):
        return simplify_condition(text[1:])

    inversions = {"==": "!=", "!=": "==", "<=": ">", ">=": "<", "<": ">=", ">": "<="}
    match = RENDERED_COMPARISON_RE.fullmatch(text)
    if match:
        left, operator, right = match.groups()
        return simplify_condition(f"{left.strip()} {inversions[operator]} {right.strip()}")
    if re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|->)[A-Za-z_][A-Za-z0-9_]*|\[[^\]]+\])*(?:\(.*\))?",
        text,
    ):
        return f"!{text}"
    return f"!({text})"


def simplify_condition(condition: str) -> str:
    text = strip_outer_parentheses(condition)
    disjunction = split_top_level_boolean(text, " || ")
    if disjunction:
        return _join_boolean(disjunction, " || ")
    conjunction = split_top_level_boolean(text, " && ")
    if conjunction:
        return _join_boolean(conjunction, " && ")
    if text.startswith("!") and not text.startswith("!="):
        return _negate_simplified_condition(simplify_condition(text[1:]))

    match = re.fullmatch(r"(.+?)\s*(==|!=)\s*(?:0[xX]0+|0+)", text)
    if match:
        left, operator = match.groups()
        simplified_left = simplify_condition(left)
        if operator == "!=":
            return simplified_left
        return _negate_simplified_condition(simplified_left)
    return text


def negate_condition(condition: str) -> str:
    return simplify_condition(_negate_simplified_condition(simplify_condition(condition)))


def last_control(block: BasicBlock, suppressed: set[int] | None = None) -> Statement | None:
    ignored = suppressed or set()
    for statement in reversed(block.statements):
        if id(statement) in ignored:
            continue
        if statement.kind in {"branch", "jump", "jump_dynamic", "return"}:
            return statement
        if statement.kind not in {"trace"}:
            break
    return None


@dataclass(slots=True)
class BlockRegion:
    index: int


@dataclass(slots=True)
class IfRegion:
    header: int
    condition: str
    then_nodes: list[Region]
    else_nodes: list[Region]


@dataclass(slots=True)
class WhileRegion:
    header: int
    condition: str
    body_nodes: list[Region]
    header_has_body: bool
    exit: int | None


@dataclass(slots=True)
class DoWhileRegion:
    start: int
    latch: int
    condition: str
    body_nodes: list[Region]
    exit: int


@dataclass(slots=True)
class DisabledRegion:
    header: int
    body_nodes: list[Region]


@dataclass(slots=True)
class DispatchRegion:
    headers: list[int]
    selector: str
    tests: list[tuple[str, str, str]]


@dataclass(slots=True)
class FunctionTableGroup:
    name: str
    statement_ids: set[int]
    first_statement_id: int
    dispatch_statement_id: int
    targets: set[str]
    entries: list[str | None]
    index_local: str
    call_arguments: str
    source_lines: tuple[int, ...]


@dataclass(slots=True)
class LocalArrayCopy:
    statement_id: int
    base: str
    start_index: int
    values: list[str]


@dataclass(slots=True)
class LocalArray:
    base: str
    type_id: int
    c_type: str
    element_size: int
    count: int
    copies: list[LocalArrayCopy]
    is_parameter: bool = False
    owns_frame_storage: bool = False
    descending_frame: bool = False


@dataclass(slots=True)
class AssignmentRun:
    statement_ids: list[int]
    target: str
    start_index: int
    end_index: int
    value: str
    loop_variable: str
    source_lines: tuple[int, ...]


Region = BlockRegion | IfRegion | WhileRegion | DoWhileRegion | DisabledRegion | DispatchRegion


class RegionBuilder:
    def __init__(self, blocks: list[BasicBlock]) -> None:
        self.blocks = blocks
        self.cfg = ControlFlowGraph.build(blocks)
        self.index_by_label = self.cfg.index_by_label
        self.suppressed: set[int] = set()
        self.protected_controls: set[int] = set()
        self.dispatch_targets: set[str] = set()
        self.predecessors = self.cfg.predecessors

    def _last_control(self, index: int) -> Statement | None:
        control = last_control(self.blocks[index], self.suppressed)
        if control is not None and id(control) in self.protected_controls:
            return None
        return control

    def _single_entry_range(self, start: int, end: int, allowed_entry: int) -> bool:
        for index in range(start, end):
            if index == allowed_entry:
                continue
            if any(predecessor < start or predecessor >= end for predecessor in self.predecessors[index]):
                return False
        return True

    def _try_do_while(self, start: int, end: int, disabled_loops: set[int]) -> tuple[DoWhileRegion, int] | None:
        if start in disabled_loops:
            return None
        header_label = self.blocks[start].label
        for latch in range(end - 1, start - 1, -1):
            control = self._last_control(latch)
            if (
                control is None
                or control.kind != "branch"
                or control.target != header_label
                or not control.condition
            ):
                continue
            if latch + 1 >= end:
                continue
            natural_loop = self.cfg.natural_loop_for_header(start)
            if natural_loop != set(range(start, latch + 1)):
                continue
            if not self._single_entry_range(start, latch + 1, start):
                continue
            self.suppressed.add(id(control))
            body = self.build_range(start, latch + 1, disabled_loops | {start})
            return DoWhileRegion(start, latch, control.condition, body, latch + 1), latch + 1
        return None

    def _try_infinite_loop(
        self,
        start: int,
        end: int,
        disabled_loops: set[int],
    ) -> tuple[WhileRegion, int] | None:
        if start in disabled_loops:
            return None
        natural_loop = self.cfg.natural_loop_for_header(start)
        if len(natural_loop) <= 1:
            return None
        loop_end = max(natural_loop) + 1
        expected = set(range(start, loop_end))
        if natural_loop != expected or loop_end > end:
            return None
        exits = {
            target
            for index in natural_loop
            for target in self.cfg.successors[index]
            if target not in natural_loop
        }
        if (
            not self._single_entry_range(start, loop_end, start)
            or not all(self.cfg.dominates(start, index) for index in natural_loop)
        ):
            return None
        latch = loop_end - 1
        latch_control = self._last_control(latch)
        if (
            latch_control is not None
            and latch_control.kind == "branch"
            and latch_control.target == self.blocks[start].label
        ):
            return None
        back_sources = {
            source
            for source, target in self.cfg.back_edges
            if target == start and source in natural_loop
        }
        if len(back_sources) == 1:
            back_control = self._last_control(next(iter(back_sources)))
            if back_control is not None and back_control.kind == "branch":
                return None
        if (
            latch_control is not None
            and latch_control.kind == "jump"
            and latch_control.target == self.blocks[start].label
        ):
            self.suppressed.add(id(latch_control))
        loop_exit = loop_end if loop_end < end and loop_end in exits else None
        exit_control_ids: set[int] = set()
        if loop_exit is not None:
            exit_label = self.blocks[loop_exit].label
            for index in natural_loop:
                control = self._last_control(index)
                if control is not None and control.target == exit_label:
                    exit_control_ids.add(id(control))
        newly_protected = exit_control_ids.difference(self.protected_controls)
        self.protected_controls.update(newly_protected)
        try:
            body = self.build_range(start, loop_end, disabled_loops | {start})
        finally:
            self.protected_controls.difference_update(newly_protected)
        return WhileRegion(
            start,
            "true",
            body,
            False,
            loop_exit,
        ), loop_end

    def _try_while(self, start: int, end: int, disabled_loops: set[int]) -> tuple[WhileRegion, int] | None:
        if start in disabled_loops:
            return None
        control = self._last_control(start)
        if control is None or control.kind != "branch" or not control.target or not control.condition:
            return None
        natural_loop = self.cfg.natural_loop_for_header(start)
        if len(natural_loop) > 1:
            loop_end = max(natural_loop) + 1
            expected = set(range(start, loop_end))
            exit_index = self.index_by_label.get(control.target)
            back_sources = {
                source
                for source, target in self.cfg.back_edges
                if target == start and source in natural_loop
            }
            if (
                natural_loop == expected
                and loop_end <= end
                and exit_index == loop_end
                and back_sources
                and self._single_entry_range(start, loop_end, start)
                and all(self.cfg.dominates(start, index) for index in natural_loop)
            ):
                self.suppressed.add(id(control))
                latch = loop_end - 1
                latch_control = self._last_control(latch)
                if (
                    latch_control is not None
                    and latch_control.kind == "jump"
                    and latch_control.target == self.blocks[start].label
                ):
                    self.suppressed.add(id(latch_control))
                body = self.build_range(start + 1, loop_end, disabled_loops | {start})
                header_has_body = any(
                    id(statement) not in self.suppressed and statement.kind != "state"
                    for statement in self.blocks[start].statements
                )
                return WhileRegion(
                    start,
                    negate_condition(control.condition),
                    body,
                    header_has_body,
                    loop_end,
                ), loop_end
            return None
        exit_index = self.index_by_label.get(control.target)
        if exit_index is None or exit_index <= start + 1 or exit_index > end:
            return None

        latch = exit_index - 1
        latch_control = self._last_control(latch)
        if latch_control is None or latch_control.kind != "jump" or latch_control.target != self.blocks[start].label:
            return None
        if not self._single_entry_range(start, exit_index, start):
            return None

        self.suppressed.update({id(control), id(latch_control)})
        body = self.build_range(start + 1, exit_index, disabled_loops | {start})
        header_has_body = any(
            id(statement) not in self.suppressed and statement.kind != "state"
            for statement in self.blocks[start].statements
        )
        return WhileRegion(
            start,
            negate_condition(control.condition),
            body,
            header_has_body,
            exit_index,
        ), exit_index

    def _try_if(self, start: int, end: int, disabled_loops: set[int]) -> tuple[IfRegion, int] | None:
        control = self._last_control(start)
        if control is None or control.kind != "branch" or not control.target or not control.condition:
            return None
        target = self.index_by_label.get(control.target)
        if target is None or target <= start + 1 or target > end:
            return None

        then_end = target
        else_end = target
        else_nodes: list[Region] = []
        trailing_jump: Statement | None = None
        if target - 1 > start:
            candidate = self._last_control(target - 1)
            if candidate is not None and candidate.kind == "jump" and candidate.target:
                join = self.index_by_label.get(candidate.target)
                if join is not None and target < join <= end:
                    trailing_jump = candidate
                    else_end = join

        if not self._single_entry_range(start + 1, then_end, start + 1):
            return None
        if trailing_jump is not None and not self._single_entry_range(target, else_end, target):
            return None

        self.suppressed.add(id(control))
        if trailing_jump is not None:
            self.suppressed.add(id(trailing_jump))
        then_nodes = self.build_range(start + 1, then_end, disabled_loops)
        if trailing_jump is not None:
            else_nodes = self.build_range(target, else_end, disabled_loops)
        return IfRegion(start, negate_condition(control.condition), then_nodes, else_nodes), else_end

    def _try_guarded_prefix(
        self,
        start: int,
        end: int,
        disabled_loops: set[int],
    ) -> tuple[IfRegion, int] | None:
        """Recover an if body that is interleaved with sibling path blocks."""
        control = self._last_control(start)
        if control is None or control.kind != "branch" or not control.target or not control.condition:
            return None
        target = self.index_by_label.get(control.target)
        if (
            target is None
            or target <= start + 1
            or self.cfg.immediate_postdominators.get(start) != target
            or start + 1 >= end
        ):
            return None

        body_end = start + 1
        while body_end < min(end, target) and self.cfg.dominates(start, body_end):
            body_end += 1
        if body_end == start + 1:
            return None
        body_indices = set(range(start + 1, body_end))
        if any(
            predecessor not in body_indices | {start}
            for index in body_indices
            for predecessor in self.predecessors[index]
        ):
            return None
        if any(
            successor not in body_indices and successor != target
            for index in body_indices
            for successor in self.cfg.successors[index]
        ):
            return None

        self.suppressed.add(id(control))
        body = self.build_range(start + 1, body_end, disabled_loops)
        return IfRegion(start, negate_condition(control.condition), body, []), body_end

    def _try_disabled_forward_range(self, start: int, end: int, disabled_loops: set[int]) -> tuple[DisabledRegion, int] | None:
        control = self._last_control(start)
        if control is None or control.kind != "jump" or not control.target:
            return None
        target = self.index_by_label.get(control.target)
        if target is None or target <= start + 1 or target > end:
            return None
        if self.predecessors[start + 1]:
            return None
        if not self._single_entry_range(start + 1, target, start + 1):
            return None
        self.suppressed.add(id(control))
        body = self.build_range(start + 1, target, disabled_loops)
        return DisabledRegion(start, body), target

    def _try_dispatch(self, start: int, end: int) -> tuple[DispatchRegion, int] | None:
        selector: str | None = None
        tests: list[tuple[str, str, str]] = []
        controls: list[Statement] = []
        headers: list[int] = []
        index = start

        while index + 1 < end:
            block = self.blocks[index]
            control = self._last_control(index)
            if control is None or control.kind != "branch" or not control.target or not control.condition:
                break
            if self.blocks[index + 1].label not in block.successors:
                break
            meaningful = [
                statement
                for statement in block.statements
                if statement is not control and statement.kind not in {"state", "trace"}
            ]
            if index != start and meaningful:
                break

            equality = None
            if not split_top_level_boolean(control.condition, " || ") and not split_top_level_boolean(
                control.condition,
                " && ",
            ):
                equality = re.fullmatch(
                    r"(?P<selector>.+?)\s*==\s*"
                    r"(?P<value>-?(?:0[xX][0-9A-Fa-f]+|\d+))",
                    control.condition,
                )
            zero_test = re.fullmatch(
                r"!\(?(?P<selector>[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\)?",
                control.condition,
            )
            if equality:
                current_selector = equality.group("selector")
                if selector is None:
                    selector = current_selector
                if current_selector != selector:
                    break
                tests.append(("case", equality.group("value"), control.target))
            elif zero_test:
                current_selector = zero_test.group("selector")
                if selector is None:
                    selector = current_selector
                if current_selector != selector:
                    break
                tests.append(("case", "0", control.target))
            elif selector is not None and selector in control.condition:
                tests.append(("guard", control.condition, control.target))
            else:
                break

            headers.append(index)
            controls.append(control)
            index += 1

        case_count = sum(kind == "case" for kind, _, _ in tests)
        if selector is None or case_count < 2 or not self._single_entry_range(start, index, start):
            return None
        self.suppressed.update(id(control) for control in controls)
        self.dispatch_targets.update(target for _, _, target in tests)
        return DispatchRegion(headers, selector, tests), index

    def build_range(self, start: int, end: int, disabled_loops: set[int] | None = None) -> list[Region]:
        disabled = disabled_loops or set()
        nodes: list[Region] = []
        index = start
        while index < end:
            while_region = self._try_while(index, end, disabled)
            if while_region is not None:
                node, index = while_region
                nodes.append(node)
                continue
            infinite_loop = self._try_infinite_loop(index, end, disabled)
            if infinite_loop is not None:
                node, index = infinite_loop
                nodes.append(node)
                continue
            do_while = self._try_do_while(index, end, disabled)
            if do_while is not None:
                node, index = do_while
                nodes.append(node)
                continue
            dispatch = self._try_dispatch(index, end)
            if dispatch is not None:
                node, index = dispatch
                nodes.append(node)
                continue
            guarded_prefix = self._try_guarded_prefix(index, end, disabled)
            if guarded_prefix is not None:
                node, index = guarded_prefix
                nodes.append(node)
                continue
            if_region = self._try_if(index, end, disabled)
            if if_region is not None:
                node, index = if_region
                nodes.append(node)
                continue
            disabled_region = self._try_disabled_forward_range(index, end, disabled)
            if disabled_region is not None:
                node, index = disabled_region
                nodes.append(node)
                continue
            nodes.append(BlockRegion(index))
            index += 1
        return nodes


class StructuredEmitter:
    def __init__(
        self,
        module: Module,
        blocks: list[BasicBlock] | None = None,
        function_refs: dict[str, str] | None = None,
        *,
        variable_index_start: int = 1,
        include_source_comments: bool = True,
    ) -> None:
        self.module = module
        self.blocks = blocks if blocks is not None else module.blocks
        self.function_refs = function_refs or {}
        self.call_parameter_types: dict[str, tuple[str, ...]] = {}
        self.variable_index_start = variable_index_start
        self.include_source_comments = include_source_comments
        self._reference_cache: dict[str, str] = {}
        self.builder = RegionBuilder(self.blocks)
        self.entry_reachable_blocks = self._entry_reachable_blocks()
        self.regions = self.builder.build_range(0, len(self.blocks))
        (
            self.inline_dispatch_cases,
            self.inline_dispatch_defaults,
            self.inline_dispatch_blocks,
        ) = self._recover_inline_dispatch_cases()
        self._suppress_fallthrough_jumps()
        self.structured_controls: dict[int, str] = {}
        self._recover_loop_controls(self.regions)
        (
            self.inline_terminal_branches,
            self.inline_terminal_blocks,
        ) = self._recover_inline_terminal_branches()
        self.omitted_blocks = self._omitted_block_indices(self.regions)
        self.omitted_blocks.update(
            index for index in range(len(self.blocks)) if index not in self.entry_reachable_blocks
        )
        self.parameter_bindings, self.parameter_statements = self._infer_parameters()
        self._fold_dead_materialized_call_results()
        self.parameter_locals = [
            parameter
            for kind, parameter in self.parameter_bindings
            if kind == "local"
        ]
        bound_stack_parameters = {
            parameter
            for kind, parameter in self.parameter_bindings
            if kind == "stack"
        }
        self.stack_parameter_indices = [
            parameter
            for kind, parameter in self.parameter_bindings
            if kind == "stack"
        ]
        self.stack_parameter_indices.extend(
            index
            for index in self._infer_stack_parameter_indices()
            if index not in bound_stack_parameters
        )
        extra_stack_parameters = [
            ("stack", index)
            for index in self.stack_parameter_indices
            if index not in bound_stack_parameters
        ]
        self.parameter_bindings = extra_stack_parameters + self.parameter_bindings
        self.has_unresolved_stack_reads = any(
            "vm_pop(vm)" in (statement.condition or statement.text)
            for block_index, block in enumerate(self.blocks)
            if block_index not in self.omitted_blocks
            for statement in block.statements
            if id(statement) not in self.parameter_statements
        )
        self.terminal_returns = self._find_terminal_returns()
        self.function_table_groups = self._recover_function_table_groups()
        self.function_table_statements = {
            statement_id: group
            for group in self.function_table_groups
            for statement_id in group.statement_ids
        }
        self.function_table_dispatches = {
            group.dispatch_statement_id: group for group in self.function_table_groups
        }
        self.function_table_targets = {
            target for group in self.function_table_groups for target in group.targets
        }
        (
            self.local_arrays,
            self.local_array_copies,
            self.local_array_clears,
            self.coalesced_local_array_members,
        ) = self._recover_local_arrays()
        self.local_array_by_base = {array.base: array for array in self.local_arrays}
        self._promote_surface_info_output_buffers()
        self.overlapping_local_array_members = self._rewrite_overlapping_array_members()
        self._refine_overlapping_array_types()
        self.coalesced_local_array_members.update(self.overlapping_local_array_members)
        self.local_array_by_base = {array.base: array for array in self.local_arrays}
        self.local_array_bases = set(self.local_array_by_base)
        self.block_depths = self._collect_block_depths(self.regions)
        (
            self.local_array_initializers,
            self.folded_array_statements,
            self.array_copy_restores,
        ) = self._recover_entry_array_initializers()
        self.local_string_initializers = self._recover_entry_string_initializers()
        for index in self.inline_dispatch_blocks:
            self.block_depths[index] = max(1, self.block_depths.get(index, 0))
        self.variable_names = self._build_variable_names()
        self.rendered_variable_sources = {
            rendered: source for source, rendered in self.variable_names.items()
        }
        self.predeclared_variables = {
            match.group("name")
            for block_index, block in enumerate(self.blocks)
            if block_index not in self.omitted_blocks
            for statement in block.statements
            for match in [INLINE_RESULT_DECLARATION_RE.match(statement.text.strip())]
            if match is not None
        }
        self.stack_parameter_type_hints: dict[int, set[str]] = {}
        self.variable_types = self._infer_variable_types()
        self._refine_pointer_word_array_types()
        stack_parameter_start = self.variable_index_start + len(self.variable_names)
        self.stack_parameter_names = {
            index: f"v{stack_parameter_start + position}"
            for position, index in enumerate(self.stack_parameter_indices)
        }
        self.next_variable_index = stack_parameter_start + len(self.stack_parameter_names)
        self.array_fill_variables: dict[int, str] = {}
        for statement_id, copy in self.local_array_copies.items():
            if (
                statement_id not in self.folded_array_statements
                and statement_id not in self.array_copy_restores
                and len(copy.values) >= 4
                and len({value.strip() for value in copy.values}) == 1
            ):
                self.array_fill_variables[statement_id] = f"v{self.next_variable_index}"
                self.next_variable_index += 1
        self.array_copy_variables: dict[int, str] = {}
        self.dynamic_array_copy_variables: dict[int, tuple[str, str]] = {}
        for statement_id, copy in self.local_array_copies.items():
            if (
                statement_id not in self.folded_array_statements
                and statement_id not in self.array_copy_restores
                and statement_id not in self.array_fill_variables
                and len(copy.values) > 4
            ):
                if all(self._is_declaration_initializer(value) for value in copy.values):
                    self.array_copy_variables[statement_id] = f"v{self.next_variable_index}"
                    self.next_variable_index += 1
                else:
                    self.dynamic_array_copy_variables[statement_id] = (
                        f"v{self.next_variable_index}",
                        f"v{self.next_variable_index + 1}",
                    )
                    self.next_variable_index += 2
        self.array_clear_variables: dict[int, str] = {}
        for statement_id, (base, byte_offset, byte_count) in self.local_array_clears.items():
            if statement_id in self.folded_array_statements:
                continue
            array = self.local_array_by_base[base]
            if byte_offset % array.element_size or byte_count % array.element_size:
                continue
            if byte_count // array.element_size > 4:
                self.array_clear_variables[statement_id] = f"v{self.next_variable_index}"
                self.next_variable_index += 1
        self.memory_repeat_variables: dict[int, str] = {}
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                if (
                    id(statement) not in self.local_array_copies
                    and parse_memcpy_repeat(statement.text) is not None
                ):
                    self.memory_repeat_variables[id(statement)] = (
                        f"v{self.next_variable_index}"
                    )
                    self.next_variable_index += 1
        self.assignment_runs = self._recover_assignment_runs()
        self.assignment_run_by_statement = {
            statement_id: run
            for run in self.assignment_runs
            for statement_id in run.statement_ids
        }
        self.return_type = self._infer_return_type()
        self.implicit_return_binding: tuple[str, int | str] | None = None
        self.remaining_targets = self._remaining_targets()
        self.inline_declarations = self._find_inline_declarations()

    def _suppress_fallthrough_jumps(self) -> None:
        for index, block in enumerate(self.blocks[:-1]):
            control = last_control(block, self.builder.suppressed)
            if (
                control is not None
                and control.kind == "jump"
                and control.target == self.blocks[index + 1].label
            ):
                self.builder.suppressed.add(id(control))

    def _dispatch_regions(self, nodes: list[Region]) -> list[DispatchRegion]:
        result: list[DispatchRegion] = []
        for node in nodes:
            if isinstance(node, DispatchRegion):
                result.append(node)
            elif isinstance(node, IfRegion):
                result.extend(self._dispatch_regions(node.then_nodes))
                result.extend(self._dispatch_regions(node.else_nodes))
            elif isinstance(node, (WhileRegion, DoWhileRegion, DisabledRegion)):
                result.extend(self._dispatch_regions(node.body_nodes))
        return result

    def _terminal_dispatch_chain(self, target: str, headers: set[int]) -> list[int] | None:
        index = self.builder.index_by_label.get(target)
        chain: list[int] = []
        seen: set[int] = set()
        while index is not None and index not in seen and index not in headers:
            seen.add(index)
            chain.append(index)
            block = self.blocks[index]
            controls = [
                statement
                for statement in block.statements
                if statement.kind in {"branch", "jump", "jump_dynamic", "return"}
            ]
            if any(statement.kind in {"branch", "jump_dynamic"} for statement in controls):
                return None
            terminal = next((statement for statement in reversed(controls) if statement.kind == "return"), None)
            if terminal is not None:
                return chain
            jump = next((statement for statement in reversed(controls) if statement.kind == "jump"), None)
            if jump is not None:
                index = self.builder.index_by_label.get(jump.target or "")
                continue
            if len(block.successors) != 1:
                return None
            index = self.builder.index_by_label.get(block.successors[0])
        return None

    def _jump_dispatch_chain(
        self,
        target: str,
        headers: set[int],
    ) -> tuple[list[int], str] | None:
        """Return an acyclic case body and the join reached by its final jump."""
        index = self.builder.index_by_label.get(target)
        chain: list[int] = []
        seen: set[int] = set()
        while index is not None and index not in seen and index not in headers:
            seen.add(index)
            chain.append(index)
            block = self.blocks[index]
            controls = [
                statement
                for statement in block.statements
                if statement.kind in {"branch", "jump", "jump_dynamic", "return"}
            ]
            if any(statement.kind in {"branch", "jump_dynamic", "return"} for statement in controls):
                return None
            jump = next((statement for statement in reversed(controls) if statement.kind == "jump"), None)
            if jump is not None and jump.target:
                return chain, jump.target
            if len(block.successors) != 1:
                return None
            index = self.builder.index_by_label.get(block.successors[0])
        return None

    def _entry_reachable_blocks(self) -> set[int]:
        return set(self.builder.cfg.reachable)

    def _recover_inline_dispatch_cases(
        self,
    ) -> tuple[dict[int, dict[str, list[int]]], dict[int, list[int]], set[int]]:
        recovered: dict[int, dict[str, list[int]]] = {}
        defaults: dict[int, list[int]] = {}
        omitted: set[int] = set()
        reachable = self.entry_reachable_blocks
        for region in self._dispatch_regions(self.regions):
            headers = set(region.headers)
            if not headers or min(headers) not in reachable:
                continue
            targets = dict.fromkeys(target for kind, _, target in region.tests if kind == "case")
            chains: dict[str, list[int]] = {}
            for target in targets:
                chain = self._terminal_dispatch_chain(target, headers)
                if chain is not None:
                    chains[target] = chain
            default_start = max(headers) + 1
            default_chain = (
                self._terminal_dispatch_chain(self.blocks[default_start].label, headers)
                if default_start < len(self.blocks)
                else None
            )
            if chains:
                clone_safe = {
                    index
                    for chain in chains.values()
                    for index in chain[-1:]
                    if any(
                        statement.kind == "return"
                        for statement in self.blocks[index].statements
                    )
                    and not any(
                        statement.kind in {"branch", "jump_dynamic"}
                        for statement in self.blocks[index].statements
                    )
                }
                changed = True
                while changed:
                    changed = False
                    chain_blocks = {index for chain in chains.values() for index in chain}
                    allowed_predecessors = headers | chain_blocks | set(default_chain or ())
                    unsafe_targets = {
                        target
                        for target, chain in chains.items()
                        if any(
                            predecessor in reachable and predecessor not in allowed_predecessors
                            for index in chain
                            if index not in clone_safe
                            for predecessor in self.builder.predecessors[index]
                        )
                    }
                    if unsafe_targets:
                        for target in unsafe_targets:
                            chains.pop(target, None)
                        changed = True
                if not chains:
                    continue
                chain_blocks = {index for chain in chains.values() for index in chain}
                for index in chain_blocks:
                    for statement in self.blocks[index].statements:
                        if statement.kind == "jump":
                            self.builder.suppressed.add(id(statement))
                recovered[id(region)] = chains
                # Case chains may share their terminal tail with the default
                # path. Keep that shared tail in its physical location.
                omittable = {
                    index
                    for index in chain_blocks
                    if all(
                        predecessor in headers | chain_blocks
                        for predecessor in self.builder.predecessors[index]
                    )
                }
                omitted.update(omittable.difference(default_chain or ()))
                continue

            # A common compiler form ends every case with jump_rel16 to
            # one shared continuation. Inline only the exclusive case bodies,
            # emit the default terminal path inside the switch, and leave the
            # shared continuation after the switch.
            chains_by_join: dict[str, dict[str, list[int]]] = {}
            for target in targets:
                recovered_chain = self._jump_dispatch_chain(target, headers)
                if recovered_chain is None:
                    continue
                chain, join = recovered_chain
                chains_by_join.setdefault(join, {})[target] = chain
            if not chains_by_join or default_chain is None:
                continue

            join_label, jump_chains = max(
                chains_by_join.items(),
                key=lambda item: len(item[1]),
            )
            if len(jump_chains) < 4:
                continue
            join_index = self.builder.index_by_label.get(join_label)
            if join_index is None:
                continue
            chain_blocks = {index for chain in jump_chains.values() for index in chain}
            allowed_predecessors = headers | chain_blocks
            if any(
                predecessor in reachable and predecessor not in allowed_predecessors
                for index in chain_blocks
                for predecessor in self.builder.predecessors[index]
            ):
                continue

            default_set = set(default_chain)
            default_omitted = {
                index
                for index in default_chain
                if all(
                    predecessor not in reachable
                    or predecessor in default_set | headers
                    for predecessor in self.builder.predecessors[index]
                )
            }
            for index in chain_blocks | default_omitted:
                for statement in self.blocks[index].statements:
                    if statement.kind == "jump":
                        self.builder.suppressed.add(id(statement))

            dead_between = {
                index
                for index in range(default_start, join_index)
                if index not in reachable and index not in chain_blocks and index not in default_set
            }
            for index in dead_between:
                for statement in self.blocks[index].statements:
                    if statement.kind == "jump":
                        self.builder.suppressed.add(id(statement))
            dead_lines = tuple(
                dict.fromkeys(
                    line
                    for index in sorted(dead_between)
                    for statement in self.blocks[index].statements
                    for line in statement.source_lines
                )
            )
            if dead_lines and self.blocks[join_index].statements:
                destination = self.blocks[join_index].statements[0]
                destination.source_lines = tuple(
                    dict.fromkeys((*dead_lines, *destination.source_lines))
                )

            recovered[id(region)] = jump_chains
            defaults[id(region)] = default_chain
            omitted.update(chain_blocks | default_omitted | dead_between)
        return recovered, defaults, omitted

    def _recover_inline_terminal_branches(self) -> tuple[dict[int, list[int]], set[int]]:
        recovered: dict[int, list[int]] = {}
        omitted: set[int] = set()
        for source_index, block in enumerate(self.blocks):
            if source_index not in self.entry_reachable_blocks:
                continue
            control = last_control(block, self.builder.suppressed)
            if (
                control is None
                or id(control) in self.structured_controls
                or control.kind not in {"branch", "jump"}
                or not control.target
                or (control.kind == "branch" and not control.condition)
            ):
                continue
            target_index = self.builder.index_by_label.get(control.target)
            if target_index is None or target_index <= source_index:
                continue
            chain = self._terminal_dispatch_chain(control.target, set())
            if not chain or any(index in self.inline_dispatch_blocks | omitted for index in chain):
                continue
            allowed = {source_index}
            valid = True
            for index in chain:
                if not self.builder.predecessors[index] <= allowed:
                    valid = False
                    break
                allowed = {index}
            if not valid:
                continue
            for index in chain:
                for statement in self.blocks[index].statements:
                    if statement.kind == "jump":
                        self.builder.suppressed.add(id(statement))
            recovered[id(control)] = chain
            omitted.update(chain)

        for source_index, block in enumerate(self.blocks):
            control = last_control(block, self.builder.suppressed)
            if (
                control is None
                or id(control) in recovered
                or id(control) in self.structured_controls
                or control.kind not in {"branch", "jump"}
                or not control.target
                or (control.kind == "branch" and not control.condition)
            ):
                continue
            target_index = self.builder.index_by_label.get(control.target)
            if (
                target_index is None
                or target_index in self.inline_dispatch_blocks | omitted
                or len(self.builder.predecessors[target_index]) < 2
            ):
                continue
            target = self.blocks[target_index]
            meaningful = [
                statement
                for statement in target.statements
                if statement.kind not in {"state", "trace"}
            ]
            if (
                not 1 <= len(meaningful) <= 16
                or meaningful[-1].kind != "return"
                or any(
                    statement.kind in {"branch", "jump", "jump_dynamic", "stack", "stack_preserved", "opaque"}
                    or "vm_pop(vm)" in statement.text
                    or "vm_arg(vm," in statement.text
                    for statement in meaningful
                )
            ):
                continue
            recovered[id(control)] = [target_index]
        return recovered, omitted

    def _find_terminal_returns(self) -> dict[str, Statement]:
        terminal: dict[str, Statement] = {}
        for block in self.blocks:
            meaningful = [
                statement
                for statement in block.statements
                if statement.kind not in {"state", "trace"}
                and id(statement) not in self.parameter_statements
            ]
            if (
                len(meaningful) == 1
                and meaningful[0].kind == "return"
                and "vm_pop(vm)" not in meaningful[0].text
                and "vm_arg(vm," not in meaningful[0].text
            ):
                terminal[block.label] = meaningful[0]
        return terminal

    @staticmethod
    def _parse_function_table_copy(
        statement: Statement,
    ) -> tuple[str, list[str | None]] | None:
        text = statement.text.strip()
        if not text.startswith("copystack(") or not text.endswith(");"):
            return None
        arguments = split_arguments(text[len("copystack("):-2])
        if len(arguments) < 4:
            return None
        try:
            width = int(arguments[-2], 0)
            count = int(arguments[-1], 0)
        except ValueError:
            return None
        if width != 2 or count < 1 or len(arguments[1:-2]) != count:
            return None

        target_text = arguments[0].strip()
        while target_text.startswith("(") and target_text.endswith(")"):
            target_text = target_text[1:-1].strip()
        target = FUNCTION_TABLE_TARGET_RE.fullmatch(target_text)
        if target is None:
            return None

        entries: list[str | None] = []
        for argument in arguments[1:-2]:
            entry = FUNCTION_TABLE_ENTRY_RE.fullmatch(argument.strip())
            if entry is not None:
                entries.append(entry.group("label"))
                continue
            try:
                value = int(argument, 0)
            except ValueError:
                return None
            if value != 0:
                return None
            entries.append(None)
        return target.group("name"), entries

    def _recover_function_table_groups(self) -> list[FunctionTableGroup]:
        groups: list[FunctionTableGroup] = []
        table_index = 1
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            statements = block.statements
            index = 0
            while index < len(statements):
                first = self._parse_function_table_copy(statements[index])
                if first is None:
                    index += 1
                    continue
                copies: list[tuple[Statement, str, list[str | None]]] = [
                    (statements[index], first[0], first[1])
                ]
                cursor = index + 1
                while cursor < len(statements):
                    parsed = self._parse_function_table_copy(statements[cursor])
                    if parsed is None:
                        break
                    copies.append((statements[cursor], parsed[0], parsed[1]))
                    cursor += 1
                if cursor >= len(statements):
                    index = cursor
                    continue

                dispatch = INDIRECT_DISPATCH_RE.fullmatch(statements[cursor].text.strip())
                try:
                    first_offset = int(first[0].split("_", 1)[1], 16)
                    dispatch_base = int(dispatch.group("base"), 0) if dispatch is not None else -1
                    encoded_index = int(dispatch.group("index"), 0) if dispatch is not None else -1
                except ValueError:
                    dispatch_base = -1
                    encoded_index = -1
                if dispatch is None or dispatch_base != first_offset:
                    index = cursor
                    continue
                entries = [entry for _, _, values in copies for entry in values]
                if not any(entry is not None for entry in entries):
                    index = cursor + 1
                    continue

                name = "dispatch_table" if table_index == 1 else f"dispatch_table{table_index}"
                table_index += 1
                source_lines = tuple(
                    dict.fromkeys(
                        line
                        for statement, _, _ in copies
                        for line in statement.source_lines
                    )
                )
                groups.append(
                    FunctionTableGroup(
                        name=name,
                        statement_ids={id(statement) for statement, _, _ in copies},
                        first_statement_id=id(copies[0][0]),
                        dispatch_statement_id=id(statements[cursor]),
                        targets={target for _, target, _ in copies},
                        entries=entries,
                        index_local=f"local_{encoded_index & 0x3FFF:04x}",
                        call_arguments=dispatch.group("args"),
                        source_lines=source_lines,
                    )
                )
                index = cursor + 1
        return groups

    @staticmethod
    def _parse_local_array_copy(
        statement: Statement,
    ) -> tuple[str, int, int, list[str], bool] | None:
        text = statement.text.strip()
        repeat = parse_memcpy_repeat(text)
        if repeat is not None:
            target, element_size_text, count_text, source = repeat
            element_size = constant_integer(element_size_text)
            count = constant_integer(count_text)
            type_id = {1: 0, 2: 1, 4: 2, 8: 3}.get(element_size or -1)
            if type_id is None or count is None or count < 1 or count > 1024:
                return None
            target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                strip_outer_parentheses(target)
            )
            source_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                strip_outer_parentheses(source)
            )
            if (
                target_match is None
                or source_match is None
                or not source_match.group("address")
                or source_match.group("tail").strip()
            ):
                return None
            byte_offset = (
                constant_integer(target_match.group("tail"))
                if target_match.group("tail").strip()
                else 0
            )
            if byte_offset is None or byte_offset < 0 or byte_offset % element_size:
                return None
            return (
                target_match.group("base"),
                type_id,
                byte_offset // element_size,
                [source_match.group("base")] * count,
                not bool(target_match.group("address")),
            )
        if not text.startswith("copystack(") or not text.endswith(");"):
            return None
        arguments = split_arguments(text[len("copystack("):-2])
        if len(arguments) < 4:
            return None
        try:
            type_id = int(arguments[-2], 0) & 3
            count = int(arguments[-1], 0)
        except ValueError:
            return None
        values = [value.strip() for value in arguments[1:-2]]
        if count < 1 or count != len(values):
            return None

        target = strip_outer_parentheses(arguments[0])
        match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
        if match is None:
            return None
        byte_offset = constant_integer(match.group("tail")) if match.group("tail").strip() else 0
        element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
        if byte_offset is None or byte_offset < 0 or byte_offset % element_size:
            return None
        return (
            match.group("base"),
            type_id,
            byte_offset // element_size,
            values,
            not bool(match.group("address")),
        )

    @staticmethod
    def _local_array_c_type(
        type_id: int,
        copies: list[LocalArrayCopy],
        stored_values: list[str] | None = None,
    ) -> str:
        values = [value.strip() for copy in copies for value in copy.values]
        values.extend(value.strip() for value in stored_values or ())

        def is_zero(value: str) -> bool:
            try:
                return int(value, 0) == 0
            except ValueError:
                return False

        if (
            any(value.startswith('"') for value in values)
            and all(value.startswith('"') or is_zero(value) for value in values)
        ):
            return "const char *"
        if (
            any(FUNCTION_TABLE_ENTRY_RE.fullmatch(value) for value in values)
            and all(FUNCTION_TABLE_ENTRY_RE.fullmatch(value) or is_zero(value) for value in values)
        ):
            return "BPDEntryPoint"
        if type_id in {2, 3} and any(
            value.startswith('"') or FUNCTION_TABLE_ENTRY_RE.fullmatch(value)
            for value in values
        ):
            # A BP word table may deliberately mix pointers/labels with
            # numeric handles.  ``intptr_t`` represents the VM word without
            # pretending every element has one pointee type.
            return "intptr_t"
        if type_id in {2, 3} and any(
            strip_outer_parentheses(value).startswith("&")
            for value in values
        ):
            # Tables of BP addresses are still VM words.  ``intptr_t`` keeps
            # the storage width explicit and lets rendered assignments add a
            # deliberate cast instead of relying on pointer-to-int coercion.
            return "intptr_t"
        return {0: "int8_t", 1: "int16_t", 2: "int32_t", 3: "int64_t"}[type_id]

    def _recover_local_arrays(
        self,
    ) -> tuple[
        list[LocalArray],
        dict[int, LocalArrayCopy],
        dict[int, tuple[str, int, int]],
        set[str],
    ]:
        candidates: dict[tuple[str, int], list[LocalArrayCopy]] = {}
        indexed_constants: dict[tuple[str, int], set[int]] = {}
        indexed_variables: dict[tuple[str, int], set[str]] = {}
        indexed_expressions: dict[tuple[str, int], set[str]] = {}
        stored_values: dict[tuple[str, int], list[str]] = {}
        cleared_bytes: dict[str, int] = {}
        memory_span_bytes: dict[str, int] = {}
        copied_memory_bases: set[str] = set()
        dynamic_memory_spans: list[tuple[str, str, int]] = []
        dynamic_pointer_spans: list[tuple[str, str, int]] = []
        memory_copy_links: list[tuple[str, str]] = []
        alias_targets: dict[str, set[str]] = {}
        index_bounds: dict[str, set[int]] = {}
        types_by_base: dict[str, set[int]] = {}
        addressed_bases: set[str] = set()
        referenced_local_offsets: dict[str, int] = {}
        local_statement_uses: dict[str, set[int]] = {}
        scalar_initializers: dict[str, list[tuple[int, str]]] = {}
        string_buffer_bases: set[str] = set()
        clear_statements: dict[int, tuple[str, int, int]] = {}
        entry_statement_ids = {
            id(statement) for statement in (self.blocks[0].statements if self.blocks else ())
        }
        widths = {"i8": 0, "i16": 1, "i32": 2, "i64": 3}
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                if id(statement) in self.function_table_statements:
                    continue
                parsed = self._parse_local_array_copy(statement)
                if parsed is None:
                    continue
                base, type_id, start_index, values, bare_target = parsed
                is_parameter = base in self.parameter_locals
                if base in self.function_table_targets or bare_target != is_parameter:
                    continue
                copy = LocalArrayCopy(id(statement), base, start_index, values)
                candidates.setdefault((base, type_id), []).append(copy)
                types_by_base.setdefault(base, set()).add(type_id)
            for statement in block.statements:
                if id(statement) in self.function_table_statements:
                    continue
                assignment = VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())
                if assignment is not None:
                    if (
                        id(statement) in entry_statement_ids
                        and LOCAL_EXACT_RE.fullmatch(assignment.group("name"))
                        and self._is_declaration_initializer(assignment.group("value"))
                    ):
                        scalar_initializers.setdefault(
                            assignment.group("name"), []
                        ).append((id(statement), assignment.group("value").strip()))
                    targets = set(re.findall(r"&(?P<name>local_[0-9a-fA-F]+)\b", assignment.group("value")))
                    direct_alias = strip_outer_parentheses(assignment.group("value"))
                    if LOCAL_EXACT_RE.fullmatch(direct_alias):
                        targets.add(direct_alias)
                    if targets:
                        alias_targets.setdefault(assignment.group("name"), set()).update(targets)
                for source in (statement.text, statement.condition or ""):
                    for local_name in LOCAL_RE.findall(source):
                        referenced_local_offsets[local_name] = int(
                            local_name.split("_", 1)[1], 16
                        )
                        local_statement_uses.setdefault(local_name, set()).add(
                            id(statement)
                        )
                    for match in re.finditer(
                        r"\b(?P<name>local_[0-9a-fA-F]+)\s*(?P<operator><=|<)\s*"
                        r"(?P<limit>-?(?:0[xX][0-9A-Fa-f]+|\d+))\b",
                        source,
                    ):
                        limit = int(match.group("limit"), 0)
                        count = limit + (1 if match.group("operator") == "<=" else 0)
                        if count > 0:
                            index_bounds.setdefault(match.group("name"), set()).add(count)
                    for match in re.finditer(
                        r"\b(?P<limit>-?(?:0[xX][0-9A-Fa-f]+|\d+))\s*"
                        r"(?P<operator>>=|>)\s*(?P<name>local_[0-9a-fA-F]+)\b",
                        source,
                    ):
                        limit = int(match.group("limit"), 0)
                        count = limit + (1 if match.group("operator") == ">=" else 0)
                        if count > 0:
                            index_bounds.setdefault(match.group("name"), set()).add(count)
                    for name, arguments in iter_function_calls(source):
                        for argument in arguments:
                            target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                                strip_outer_parentheses(argument)
                            )
                            if target_match is None:
                                continue
                            base = target_match.group("base")
                            bare_target = not bool(target_match.group("address"))
                            if bare_target != (base in self.parameter_locals):
                                continue
                            tail = target_match.group("tail").strip()
                            offset_expression = (
                                tail[1:].strip() if tail.startswith("+") else tail
                            )
                            stride = nonnegative_expression_stride(offset_expression)
                            byte_offset = constant_integer(offset_expression)
                            if (
                                offset_expression
                                and byte_offset is not None
                                and byte_offset >= 0
                            ):
                                dynamic_pointer_spans.append(
                                    (base, offset_expression, 1)
                                )
                            elif offset_expression and stride is not None:
                                dynamic_pointer_spans.append(
                                    (base, offset_expression, stride)
                                )
                        span_arguments: list[tuple[str, int]] = []
                        if name in {"memcpy", "memory_equals"} and len(arguments) == 3:
                            byte_count = constant_integer(arguments[2])
                            if byte_count is not None and byte_count > 0:
                                span_arguments.extend(
                                    (argument, byte_count) for argument in arguments[:2]
                                )
                                if name == "memcpy":
                                    target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                                        strip_outer_parentheses(arguments[0])
                                    )
                                    source_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                                        strip_outer_parentheses(arguments[1])
                                    )
                                    if target_match is not None and source_match is not None:
                                        memory_copy_links.append(
                                            (
                                                target_match.group("base"),
                                                source_match.group("base"),
                                            )
                                        )
                                    for copied_argument in arguments[:2]:
                                        copied_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                                            strip_outer_parentheses(copied_argument)
                                        )
                                        if copied_match is not None:
                                            copied_memory_bases.add(copied_match.group("base"))
                        elif name == "memset" and len(arguments) == 3:
                            byte_count = constant_integer(arguments[2])
                            if byte_count is not None and byte_count > 0:
                                span_arguments.append((arguments[0], byte_count))
                        elif name == "memclr" and len(arguments) == 2:
                            byte_count = constant_integer(arguments[1])
                            if byte_count is not None and byte_count > 0:
                                span_arguments.append((arguments[0], byte_count))
                        elif name == "memcpy_repeat" and len(arguments) == 4:
                            element_size = constant_integer(arguments[1])
                            repeat_count = constant_integer(arguments[2])
                            if element_size is not None and element_size > 0:
                                span_arguments.append((arguments[3], element_size))
                                if repeat_count is not None and repeat_count > 0:
                                    span_arguments.append(
                                        (arguments[0], element_size * repeat_count)
                                    )
                        elif name == "copystack" and len(arguments) >= 4:
                            type_id = constant_integer(arguments[-2])
                            count = constant_integer(arguments[-1])
                            if type_id is not None and count is not None and count > 0:
                                type_id &= 3
                                element_size = (1, 2, 4, 8)[type_id]
                                span_arguments.append(
                                    (arguments[0], element_size * count)
                                )
                                target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(
                                    strip_outer_parentheses(arguments[0])
                                )
                                if target_match is not None:
                                    types_by_base.setdefault(
                                        target_match.group("base"), set()
                                    ).add(type_id)
                        for span_target, byte_count in span_arguments:
                            target = strip_outer_parentheses(span_target)
                            target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
                            if target_match is None:
                                continue
                            base = target_match.group("base")
                            bare_target = not bool(target_match.group("address"))
                            if bare_target != (base in self.parameter_locals):
                                continue
                            tail = target_match.group("tail").strip()
                            byte_offset = constant_integer(tail) if tail else 0
                            if byte_offset is None:
                                offset_expression = tail[1:].strip() if tail.startswith("+") else tail
                                if offset_expression:
                                    dynamic_memory_spans.append(
                                        (base, offset_expression, byte_count)
                                    )
                                continue
                            if byte_offset < 0:
                                continue
                            if byte_offset + byte_count <= 8:
                                # Keep ordinary scalar-sized copies scalar;
                                # the memcpy lowering already turns 1/2/4/8
                                # byte transfers into typed assignments.
                                continue
                            addressed_bases.add(base)
                            memory_span_bytes[base] = max(
                                memory_span_bytes.get(base, 0),
                                byte_offset + byte_count,
                            )

                        for argument_index in WRITABLE_STRING_ARGUMENTS.get(name, ()):
                            if argument_index >= len(arguments):
                                continue
                            target = strip_outer_parentheses(arguments[argument_index])
                            target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
                            if (
                                target_match is not None
                                and target_match.group("address")
                                and not target_match.group("tail").strip()
                            ):
                                string_buffer_bases.add(target_match.group("base"))
                        clear_target: str | None = None
                        clear_size: str | None = None
                        if name == "memclr" and len(arguments) == 2:
                            clear_target, clear_size = arguments
                        elif (
                            name == "memset"
                            and len(arguments) == 3
                            and constant_integer(arguments[1]) == 0
                        ):
                            clear_target, clear_size = arguments[0], arguments[2]
                        if clear_target is not None and clear_size is not None:
                            byte_count = constant_integer(clear_size)
                            target = strip_outer_parentheses(clear_target)
                            target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
                            if target_match is None or byte_count is None or byte_count <= 0:
                                continue
                            base = target_match.group("base")
                            bare_target = not bool(target_match.group("address"))
                            if bare_target != (base in self.parameter_locals):
                                continue
                            tail = target_match.group("tail").strip()
                            byte_offset = constant_integer(tail) if tail else 0
                            if byte_offset is None or byte_offset < 0:
                                continue
                            addressed_bases.add(base)
                            clear_statements[id(statement)] = (base, byte_offset, byte_count)
                            cleared_bytes[base] = max(
                                cleared_bytes.get(base, 0),
                                byte_offset + byte_count,
                            )
                            continue
                        type_id: int | None = None
                        address: str | None = None
                        value: str | None = None
                        if name == "load_typed" and len(arguments) == 2:
                            try:
                                type_id = int(arguments[0], 0) & 3
                            except ValueError:
                                continue
                            address = arguments[1]
                        elif name == "load_indirect_indexed" and len(arguments) == 3:
                            base_ref = constant_integer(arguments[0])
                            index_ref = constant_integer(arguments[1])
                            if base_ref is None or index_ref is None:
                                continue
                            type_id = (index_ref >> 14) & 3
                            address = (
                                f"&local_{base_ref & 0x3FFF:04x} + "
                                f"local_{index_ref & 0x3FFF:04x} * {arguments[2]}"
                            )
                        else:
                            access = re.fullmatch(r"(?P<kind>load|store)_(?P<width>i8|i16|i32|i64)", name)
                            if access is None:
                                continue
                            expected = 1 if access.group("kind") == "load" else 2
                            if len(arguments) != expected:
                                continue
                            type_id = widths[access.group("width")]
                            address = arguments[0]
                            if expected == 2:
                                value = arguments[1]
                        element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
                        indexed = local_indexed_address(address, element_size, allow_bare=True)
                        if indexed is None:
                            continue
                        base, index_text = indexed
                        target_is_bare = not strip_outer_parentheses(address).startswith("&")
                        if not target_is_bare:
                            addressed_bases.add(base)
                        if (
                            base in self.function_table_targets
                            or (
                                target_is_bare
                                and base not in self.parameter_locals
                                and base not in alias_targets
                            )
                        ):
                            continue
                        types_by_base.setdefault(base, set()).add(type_id)
                        key = (base, type_id)
                        index_value = constant_integer(index_text)
                        if index_value is not None and index_value >= 0:
                            indexed_constants.setdefault(key, set()).add(index_value)
                        elif index_value is None:
                            indexed_expressions.setdefault(key, set()).add(index_text)
                            if LOCAL_RE.fullmatch(index_text):
                                indexed_variables.setdefault(key, set()).add(index_text)
                        if value is not None:
                            stored_values.setdefault(key, []).append(value)

        for base, offset_expression, byte_count in dynamic_memory_spans:
            maximum_offset = bounded_nonnegative_expression_max(
                offset_expression,
                index_bounds,
            )
            if maximum_offset is None or maximum_offset + byte_count <= 8:
                continue
            addressed_bases.add(base)
            memory_span_bytes[base] = max(
                memory_span_bytes.get(base, 0),
                maximum_offset + byte_count,
            )

        for base, offset_expression, byte_stride in dynamic_pointer_spans:
            maximum_offset = bounded_nonnegative_expression_max(
                offset_expression,
                index_bounds,
            )
            if maximum_offset is None or maximum_offset + byte_stride <= 8:
                continue
            addressed_bases.add(base)
            memory_span_bytes[base] = max(
                memory_span_bytes.get(base, 0),
                maximum_offset + byte_stride,
            )

        for alias, targets in alias_targets.items():
            alias_types = types_by_base.get(alias, set())
            if len(alias_types) == 1:
                for target in targets:
                    types_by_base.setdefault(target, set()).update(alias_types)

        changed = True
        while changed:
            changed = False
            for target, source in memory_copy_links:
                source_types = types_by_base.get(source, set())
                target_types = types_by_base.get(target, set())
                if len(source_types) != 1 or target_types:
                    continue
                types_by_base.setdefault(target, set()).update(source_types)
                changed = True

        # Some scripts build a table as consecutive scalar locals or as
        # several adjacent copystack chunks, then index the first slot.  Merge
        # those entry-only initializers before computing array extents.  Every
        # covered secondary local must be unused outside the selected
        # initializer statements, so independent locals are never hidden by
        # this readability recovery.
        coalesced_members: set[str] = set()
        claimed_initializer_statements: set[int] = set()
        element_sizes = {0: 1, 1: 2, 2: 4, 3: 8}
        dynamic_roots = sorted(
            indexed_expressions,
            key=lambda item: referenced_local_offsets.get(item[0], -1),
            reverse=True,
        )
        for base, type_id in dynamic_roots:
            if (
                type_id != 2
                or base in self.parameter_locals
                or base not in addressed_bases
                or len(types_by_base.get(base, set())) != 1
            ):
                continue
            root_offset = referenced_local_offsets.get(base)
            if root_offset is None:
                continue
            element_size = element_sizes[type_id]
            rebased_by_statement: dict[int, LocalArrayCopy] = {}
            coverage: dict[int, int] = {}
            ambiguous = False

            for (candidate_base, candidate_type), copies in list(candidates.items()):
                if candidate_type != type_id:
                    continue
                candidate_offset = referenced_local_offsets.get(candidate_base)
                if (
                    candidate_offset is None
                    or candidate_offset > root_offset
                    or (root_offset - candidate_offset) % element_size
                ):
                    continue
                base_index = (root_offset - candidate_offset) // element_size
                for copy in copies:
                    if (
                        copy.statement_id not in entry_statement_ids
                        or not all(
                            self._is_declaration_initializer(value)
                            for value in copy.values
                        )
                    ):
                        continue
                    rebased = LocalArrayCopy(
                        copy.statement_id,
                        base,
                        base_index + copy.start_index,
                        copy.values,
                    )
                    positions = range(
                        rebased.start_index,
                        rebased.start_index + len(rebased.values),
                    )
                    if any(
                        position in coverage
                        and coverage[position] != copy.statement_id
                        for position in positions
                    ):
                        ambiguous = True
                        break
                    rebased_by_statement[copy.statement_id] = rebased
                    for position in positions:
                        coverage[position] = copy.statement_id
                if ambiguous:
                    break
            if ambiguous:
                continue

            for name, initializers in scalar_initializers.items():
                if len(initializers) != 1:
                    continue
                offset = referenced_local_offsets.get(name)
                if (
                    offset is None
                    or offset > root_offset
                    or (root_offset - offset) % element_size
                ):
                    continue
                position = (root_offset - offset) // element_size
                statement_id, value = initializers[0]
                if position in coverage and coverage[position] != statement_id:
                    ambiguous = True
                    break
                rebased_by_statement[statement_id] = LocalArrayCopy(
                    statement_id,
                    base,
                    position,
                    [value],
                )
                coverage[position] = statement_id
            if ambiguous or 0 not in coverage:
                continue

            count = 0
            while count in coverage:
                count += 1
            if count < 2 or count > 4096:
                continue
            selected_statement_ids = {
                statement_id
                for position, statement_id in coverage.items()
                if position < count
            }
            if selected_statement_ids & claimed_initializer_statements:
                continue

            member_names: set[str] = set()
            valid = True
            for name, offset in referenced_local_offsets.items():
                distance = root_offset - offset
                if distance < 0 or distance % element_size:
                    continue
                position = distance // element_size
                if position >= count or name == base:
                    continue
                if not local_statement_uses.get(name, set()).issubset(
                    selected_statement_ids
                ):
                    valid = False
                    break
                member_names.add(name)
            if not valid:
                continue

            for key, copies in list(candidates.items()):
                remaining = [
                    copy
                    for copy in copies
                    if copy.statement_id not in selected_statement_ids
                ]
                if remaining:
                    candidates[key] = remaining
                else:
                    del candidates[key]
            selected_copies = [
                copy
                for statement_id, copy in rebased_by_statement.items()
                if statement_id in selected_statement_ids
            ]
            candidates.setdefault((base, type_id), []).extend(selected_copies)
            coalesced_members.update(member_names)
            claimed_initializer_statements.update(selected_statement_ids)

        copied_types_by_base: dict[str, set[int]] = {}
        for candidate_base, candidate_type in candidates:
            copied_types_by_base.setdefault(candidate_base, set()).add(candidate_type)

        def has_ascending_index_evidence(base: str) -> bool:
            return any(
                candidate_base == base
                for candidate_base, _ in (
                    candidates.keys()
                    | indexed_constants.keys()
                    | indexed_variables.keys()
                    | indexed_expressions.keys()
                )
            ) or any(
                span_base == base
                for span_base, _, _ in dynamic_memory_spans + dynamic_pointer_spans
            )

        descending_frame_bases = {
            base
            for base in cleared_bytes.keys() | memory_span_bytes.keys()
            if base not in self.parameter_locals
            and not has_ascending_index_evidence(base)
        }

        def frame_interval(base: str, byte_count: int) -> tuple[int, int]:
            offset = referenced_local_offsets[base]
            if base in descending_frame_bases:
                return offset - byte_count, offset
            return offset, offset + byte_count

        def is_word_overlay(base: str, byte_count: int) -> bool:
            """Recognize a byte span whose observable fields are VM words."""
            if base not in copied_memory_bases or byte_count % 4:
                return False
            base_offset = referenced_local_offsets.get(base)
            if base_offset is None:
                return False
            base_low, base_high = frame_interval(base, byte_count)
            all_spans = dict(memory_span_bytes)
            for span_base, span_count in cleared_bytes.items():
                all_spans[span_base] = max(all_spans.get(span_base, 0), span_count)
            for outer_base, outer_count in all_spans.items():
                if outer_base == base:
                    continue
                if outer_base not in referenced_local_offsets:
                    continue
                outer_low, outer_high = frame_interval(outer_base, outer_count)
                if outer_low <= base_low and base_high <= outer_high:
                    return False
            distances = [
                (
                    base_offset - offset
                    if base in descending_frame_bases
                    else offset - base_offset
                )
                for offset in referenced_local_offsets.values()
            ]
            members = [distance for distance in distances if 0 < distance < byte_count]
            return bool(members) and all(distance % 4 == 0 for distance in members)

        cleared_extents: dict[tuple[str, int], int] = {}
        spanned_extents: dict[tuple[str, int], int] = {}
        for extent_map, destination in (
            (cleared_bytes, cleared_extents),
            (memory_span_bytes, spanned_extents),
        ):
            for base, byte_count in extent_map.items():
                copied_types = copied_types_by_base.get(base, set())
                accessed_types = types_by_base.get(base, set())
                if len(copied_types) == 1:
                    type_id = next(iter(copied_types))
                elif not copied_types and len(accessed_types) == 1:
                    type_id = next(iter(accessed_types))
                elif (
                    not copied_types
                    and not accessed_types
                    and is_word_overlay(base, byte_count)
                ):
                    # A cleared/copied byte span can contain ordinary VM-word
                    # fields written through their own frame offsets.  Use a
                    # single int32_t view so a later memcpy observes those
                    # writes instead of copying an unrelated uint8_t array.
                    type_id = 2
                    types_by_base.setdefault(base, set()).add(type_id)
                else:
                    type_id = 0
                    types_by_base.setdefault(base, set()).add(type_id)
                element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
                destination[(base, type_id)] = (
                    byte_count + element_size - 1
                ) // element_size

        # BP local offsets grow toward the start of each frame allocation.  If
        # an addressed local is dynamically indexed at one unambiguous width,
        # the next referenced local below it (or offset zero) gives a
        # conservative array bound.
        # This turns byte-pointer expressions back into ordinary C subscripts
        # without treating parameters or mixed-width memory as local arrays.
        frame_extents: dict[tuple[str, int], int] = {}
        ordered_offsets = sorted(set(referenced_local_offsets.values()))
        for base, type_id in indexed_expressions:
            if (
                base in self.parameter_locals
                or base not in addressed_bases
                or len(types_by_base.get(base, set())) != 1
            ):
                continue
            offset = referenced_local_offsets.get(base)
            if offset is None:
                continue
            lower_offsets = [item for item in ordered_offsets if item < offset]
            lower_bound = lower_offsets[-1] if lower_offsets else 0
            element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
            byte_count = offset - lower_bound
            if byte_count % element_size:
                continue
            count = byte_count // element_size
            if 2 <= count <= 65536:
                frame_extents[(base, type_id)] = count

        arrays: list[LocalArray] = []
        copies_by_statement: dict[int, LocalArrayCopy] = {}
        all_keys = (
            candidates.keys()
            | indexed_constants.keys()
            | indexed_variables.keys()
            | indexed_expressions.keys()
            | cleared_extents.keys()
            | spanned_extents.keys()
            | frame_extents.keys()
        )
        for base, type_id in all_keys:
            copies = candidates.get((base, type_id), [])
            copied_types = copied_types_by_base.get(base, set())
            cleared_count = cleared_extents.get((base, type_id))
            spanned_count = spanned_extents.get((base, type_id))
            frame_count = frame_extents.get((base, type_id))
            if (
                not copies
                and cleared_count is None
                and len(types_by_base.get(base, set())) != 1
            ) or len(copied_types) > 1:
                continue
            constants = indexed_constants.get((base, type_id), set())
            bounded_counts = {
                count
                for variable in indexed_variables.get((base, type_id), set())
                for count in index_bounds.get(variable, set())
            }
            has_constant_extent = bool(constants) and (
                base in addressed_bases
                or (
                    base in self.parameter_locals
                    and len(constants) >= 2
                    and min(constants) == 0
                )
            )
            if (
                not copies
                and not has_constant_extent
                and not bounded_counts
                and cleared_count is None
                and spanned_count is None
                and frame_count is None
            ):
                continue
            count = max(
                [copy.start_index + len(copy.values) for copy in copies]
                + ([max(constants) + 1] if constants else [])
                + list(bounded_counts)
                + ([cleared_count] if cleared_count is not None else [])
                + ([spanned_count] if spanned_count is not None else [])
                + ([frame_count] if frame_count is not None else [])
            )
            if count < 2 and base not in self.parameter_locals:
                continue
            c_type = self._local_array_c_type(
                type_id,
                copies,
                stored_values.get((base, type_id)),
            )
            if (
                (cleared_count is not None or spanned_count is not None)
                and not copies
                and not constants
                and not bounded_counts
            ):
                c_type = "uint8_t" if type_id == 0 else c_type
            array = LocalArray(
                base=base,
                type_id=type_id,
                c_type=c_type,
                element_size={0: 1, 1: 2, 2: 4, 3: 8}[type_id],
                count=count,
                copies=copies,
                is_parameter=base in self.parameter_locals,
                owns_frame_storage=(
                    cleared_count is not None or spanned_count is not None
                ),
                descending_frame=base in descending_frame_bases,
            )
            arrays.append(array)
            for copy in copies:
                copies_by_statement[copy.statement_id] = copy

        arrays_by_base = {array.base: array for array in arrays}
        ordered_offsets = sorted(set(referenced_local_offsets.values()))
        for base in sorted(string_buffer_bases, key=lambda item: referenced_local_offsets[item]):
            current = arrays_by_base.get(base)
            if current is not None:
                if current.type_id == 0:
                    current.c_type = "char"
                continue
            offset = referenced_local_offsets[base]
            lower_offsets = [item for item in ordered_offsets if item < offset]
            lower_bound = lower_offsets[-1] if lower_offsets else 0
            count = offset - lower_bound
            if count < 2:
                continue
            array = LocalArray(
                base=base,
                type_id=0,
                c_type="char",
                element_size=1,
                count=count,
                copies=[],
                is_parameter=False,
                descending_frame=base in descending_frame_bases,
            )
            arrays.append(array)
            arrays_by_base[base] = array
        arrays.sort(key=lambda array: int(array.base.split("_", 1)[1], 16))
        recovered_clears = {
            statement_id: clear
            for statement_id, clear in clear_statements.items()
            if clear[0] in {array.base for array in arrays}
        }
        return arrays, copies_by_statement, recovered_clears, coalesced_members

    def _promote_surface_info_output_buffers(self) -> None:
        """Recover word fields written through ``grp1_surface_info_get``.

        The BP routine writes a small surface-info record through a pointer,
        then reads the width/height slots through overlapping frame locals.
        Those locals are not ordinary independent variables: on the
        descending BP frame they are elements 2 and 3 of the output buffer.
        Keep one typed array view so the generated C exposes the alias and
        never leaves the dimensions as zero-initialized phantom locals.
        """
        call_re = re.compile(
            r"\bgrp1_surface_info_get\(\s*&(?P<base>local_[0-9A-Fa-f]+)"
        )
        bases: set[str] = set()
        for block in self.blocks:
            for statement in block.statements:
                for source in (statement.text, statement.condition or ""):
                    bases.update(match.group("base") for match in call_re.finditer(source))
        if not bases:
            return

        arrays = {array.base: array for array in self.local_arrays}
        replacements: dict[str, str] = {}
        for base in bases:
            array = arrays.get(base)
            if array is None or not array.descending_frame:
                continue
            base_offset = int(base.split("_", 1)[1], 16)
            # SurfaceInfo stores the scaled width and height at +8/+12.
            for field_offset, index in ((8, 2), (12, 3)):
                name = f"local_{base_offset - field_offset:04x}"
                replacements[name] = f"{base}[{index}]"
            array.type_id = 2
            array.c_type = "int32_t"
            array.element_size = 4
            array.count = max(array.count // 4, 6)

        if not replacements:
            return
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(name) for name in replacements) + r")\b"
        )
        for block in self.blocks:
            for statement in block.statements:
                statement.text = pattern.sub(
                    lambda match: replacements[match.group(0)], statement.text
                )
                if statement.condition is not None:
                    statement.condition = pattern.sub(
                        lambda match: replacements[match.group(0)], statement.condition
                    )

    def _rewrite_overlapping_array_members(self) -> set[str]:
        """将重叠栈槽统一为数组视图。"""
        owners: dict[str, tuple[LocalArray, int]] = {}
        array_bases = {array.base for array in self.local_arrays}
        entry = self.blocks[0] if self.blocks else None
        overwritten_parameters = {
            assignment.group("name")
            for statement in (entry.statements if entry is not None else ())
            if id(statement) not in self.parameter_statements
            for assignment in [VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())]
            if assignment is not None
            and assignment.group("name") in self.parameter_locals
        }
        parameter_offsets = {
            int(name.split("_", 1)[1], 16)
            for name in overwritten_parameters
        }
        local_arrays = [
            array
            for array in self.local_arrays
            if not array.is_parameter
            # 仅明确占用栈帧的字宽数组可合并普通标量。
            and array.element_size >= 4
            and (
                array.owns_frame_storage
                or any(
                    0
                    < (
                        int(array.base.split("_", 1)[1], 16) - offset
                        if array.descending_frame
                        else offset - int(array.base.split("_", 1)[1], 16)
                    )
                    < array.count * array.element_size
                    and abs(offset - int(array.base.split("_", 1)[1], 16))
                    % array.element_size == 0
                    for offset in parameter_offsets
                )
            )
        ]
        for array in sorted(local_arrays, key=lambda item: item.count):
            base_offset = int(array.base.split("_", 1)[1], 16)
            for index in range(1, array.count):
                direction = -1 if array.descending_frame else 1
                offset = base_offset + direction * index * array.element_size
                if offset < 0:
                    break
                name = f"local_{offset:04x}"
                # 嵌套数组保留独立视图，只合并真正的标量槽。
                if name in array_bases and name not in self.parameter_locals:
                    continue
                owners.setdefault(name, (array, index))

        # 字节缓冲区只合并纯地址用途的窄栈槽。
        statement_sources = [
            source
            for block in self.blocks
            for statement in block.statements
            for source in (statement.text, statement.condition or "")
        ]
        referenced_names: set[str] = set()
        non_addressed_names: set[str] = set()
        for source in statement_sources:
            for match in LOCAL_RE.finditer(source):
                name = match.group(0)
                referenced_names.add(name)
                if not source[:match.start()].rstrip().endswith("&"):
                    non_addressed_names.add(name)

        def address_only(name: str) -> bool:
            return name in referenced_names and name not in non_addressed_names

        byte_arrays = [
            array
            for array in self.local_arrays
            if not array.is_parameter
            and array.element_size == 1
            and array.owns_frame_storage
        ]
        for array in sorted(byte_arrays, key=lambda item: item.count):
            base_offset = int(array.base.split("_", 1)[1], 16)
            byte_count = array.count
            for name in referenced_names:
                if name == array.base or name in array_bases:
                    continue
                offset = int(name.split("_", 1)[1], 16)
                distance = (
                    base_offset - offset
                    if array.descending_frame
                    else offset - base_offset
                )
                if (
                    distance < 0
                    or distance + 4 > byte_count
                    or distance % 4
                    or not address_only(name)
                ):
                    continue
                owners.setdefault(name, (array, distance))
        if not owners or entry is None:
            return set()

        overwrite_at: dict[str, int] = {}
        for position, statement in enumerate(entry.statements):
            if id(statement) in self.parameter_statements:
                continue
            assignment = VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())
            if assignment is None:
                continue
            name = assignment.group("name")
            if name in self.parameter_locals and name in owners:
                overwrite_at.setdefault(name, position)

        absorbed: set[str] = set()
        for block_index, block in enumerate(self.blocks):
            for statement_index, statement in enumerate(block.statements):
                if id(statement) in self.parameter_statements:
                    continue
                replacements: dict[str, str] = {}
                sources = (statement.text, statement.condition or "")
                referenced = {
                    name
                    for source in sources
                    for name in LOCAL_RE.findall(source)
                    if name in owners
                }
                for name in referenced:
                    if name in self.parameter_locals:
                        first_write = overwrite_at.get(name)
                        if first_write is None or block_index == 0 and statement_index < first_write:
                            continue
                    array, index = owners[name]
                    replacements[name] = f"{array.base}[{index}]"
                    absorbed.add(name)
                if replacements:
                    def replace(match: re.Match[str]) -> str:
                        return replacements.get(match.group(0), match.group(0))

                    statement.text = LOCAL_RE.sub(replace, statement.text)
                    if statement.condition is not None:
                        statement.condition = LOCAL_RE.sub(replace, statement.condition)

        # 参数槽被覆盖后由调用点重新推断类型。
        shadowed_parameters = set(overwrite_at)
        if shadowed_parameters:
            self.local_arrays = [
                array
                for array in self.local_arrays
                if not (array.is_parameter and array.base in shadowed_parameters)
            ]
            self.local_array_copies = {
                statement_id: copy
                for statement_id, copy in self.local_array_copies.items()
                if copy.base not in shadowed_parameters
            }
            self.local_array_clears = {
                statement_id: clear
                for statement_id, clear in self.local_array_clears.items()
                if clear[0] not in shadowed_parameters
            }
        region_replacements = {
            name: f"{array.base}[{index}]"
            for name, (array, index) in owners.items()
            if name not in self.parameter_locals or name in shadowed_parameters
        }
        self._replace_region_variable_names(self.regions, region_replacements)
        return absorbed

    def _refine_overlapping_array_types(self) -> None:
        """将混合用途的字数组降为 intptr_t。"""
        candidates = {
            array.base: array
            for array in self.local_arrays
            if array.type_id in {2, 3}
            and array.c_type in {"const char *", "char *", "BPDEntryPoint"}
        }
        if not candidates:
            return

        indexed_assignment = re.compile(
            r"^(?P<base>local_[0-9A-Fa-f]+)\[[^\]]+\]\s*=\s*(?P<value>.*);$"
        )

        def compatible(array: LocalArray, value: str) -> bool:
            token = strip_outer_parentheses(value.strip())
            if constant_integer(token) == 0:
                return True
            if array.c_type in {"const char *", "char *"}:
                if token.startswith('"') and token.endswith('"'):
                    return True
                conditional = split_top_level_ternary(token)
                if conditional is not None:
                    return all(compatible(array, branch) for branch in conditional[1:])
                # 调用或运算说明该表是通用 VM 字存储。
                return bool(
                    LOCAL_EXACT_RE.fullmatch(token)
                    or VALUE_INDEXED_TARGET_RE.fullmatch(token)
                )
            return bool(
                FUNCTION_TABLE_ENTRY_RE.fullmatch(token)
                or token.startswith("sys1_load_program(")
                or LOCAL_EXACT_RE.fullmatch(token)
                or VALUE_INDEXED_TARGET_RE.fullmatch(token)
            )

        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                assignment = indexed_assignment.fullmatch(statement.text.strip())
                if assignment is None:
                    continue
                array = candidates.get(assignment.group("base"))
                if array is not None and not compatible(array, assignment.group("value")):
                    array.c_type = "intptr_t"

    @classmethod
    def _replace_region_variable_names(
        cls,
        nodes: list[Region],
        replacements: dict[str, str],
    ) -> None:
        if not replacements:
            return

        def rewrite(text: str) -> str:
            return LOCAL_RE.sub(
                lambda match: replacements.get(match.group(0), match.group(0)),
                text,
            )

        for node in nodes:
            if isinstance(node, IfRegion):
                node.condition = rewrite(node.condition)
                cls._replace_region_variable_names(node.then_nodes, replacements)
                cls._replace_region_variable_names(node.else_nodes, replacements)
            elif isinstance(node, WhileRegion):
                node.condition = rewrite(node.condition)
                cls._replace_region_variable_names(node.body_nodes, replacements)
            elif isinstance(node, DoWhileRegion):
                node.condition = rewrite(node.condition)
                cls._replace_region_variable_names(node.body_nodes, replacements)
            elif isinstance(node, DisabledRegion):
                cls._replace_region_variable_names(node.body_nodes, replacements)
            elif isinstance(node, DispatchRegion):
                node.selector = rewrite(node.selector)
                node.tests = [
                    (kind, rewrite(value), target)
                    for kind, value, target in node.tests
                ]

    @staticmethod
    def _is_declaration_initializer(value: str) -> bool:
        token = value.strip()
        if token in {"true", "false", "NULL"} or token.startswith(('"', "'")):
            return True
        if constant_integer(token) is not None:
            return True
        return FUNCTION_TABLE_ENTRY_RE.fullmatch(token) is not None

    def _recover_entry_array_initializers(
        self,
    ) -> tuple[
        dict[str, tuple[list[str], tuple[int, ...]]],
        set[int],
        dict[int, list[tuple[int, str]]],
    ]:
        if not self.blocks:
            return {}, set(), {}

        first_block = self.blocks[0]
        positions = {id(statement): index for index, statement in enumerate(first_block.statements)}
        statements_by_id = {id(statement): statement for statement in first_block.statements}
        locations = {
            id(statement): (block_index, statement_index)
            for block_index, block in enumerate(self.blocks)
            if block_index not in self.omitted_blocks
            for statement_index, statement in enumerate(block.statements)
        }
        all_statements_by_id = {
            id(statement): statement
            for block_index, block in enumerate(self.blocks)
            if block_index not in self.omitted_blocks
            for statement in block.statements
        }
        initializers: dict[str, tuple[list[str], tuple[int, ...]]] = {}
        folded: set[int] = set()
        restores: dict[int, list[tuple[int, str]]] = {}

        for array in self.local_arrays:
            if array.is_parameter:
                continue
            copies = array.copies
            if copies and all(copy.statement_id in positions for copy in copies):
                values: list[str | None] = [None] * array.count
                valid = True
                for copy in copies:
                    for offset, value in enumerate(copy.values, start=copy.start_index):
                        if (
                            offset >= array.count
                            or values[offset] is not None
                            or not self._is_declaration_initializer(value)
                        ):
                            valid = False
                            break
                        values[offset] = value
                    if not valid:
                        break
                if valid and all(value is not None for value in values):
                    source_lines = tuple(
                        dict.fromkeys(
                            line
                            for copy in copies
                            for line in statements_by_id[copy.statement_id].source_lines
                        )
                    )
                    initializers[array.base] = (
                        [value for value in values if value is not None],
                        source_lines,
                    )
                    folded.update(copy.statement_id for copy in copies)

            # 常量表提升到声明；提前改写的项在原位置恢复。
            if array.base not in initializers and len(copies) == 1:
                copy = copies[0]
                copy_location = locations.get(copy.statement_id)
                copy_in_loop = copy_location is not None and any(
                    copy_location[0] in loop
                    for loop in self.builder.cfg.natural_loops.values()
                )
                declaration_values = all(
                    self._is_declaration_initializer(value) for value in copy.values
                )
                contains_function_entry = any(
                    FUNCTION_TABLE_ENTRY_RE.fullmatch(value.strip()) is not None
                    for value in copy.values
                )
                if (
                    copy_location is not None
                    and not copy_in_loop
                    and copy.start_index == 0
                    and len(copy.values) <= array.count
                    and declaration_values
                    and not contains_function_entry
                ):
                    copy_block, copy_position = copy_location
                    pattern = re.compile(rf"\b{re.escape(array.base)}\b")
                    indexed_pattern = re.compile(
                        rf"\b{re.escape(array.base)}\[(?P<index>\d+)\]"
                    )
                    assignment_pattern = re.compile(
                        rf"^{re.escape(array.base)}\[(?P<index>\d+)\]\s*=\s*"
                        rf"(?P<value>.+);$"
                    )

                    def same_initializer(left: str, right: str) -> bool:
                        left_value = constant_integer(left)
                        right_value = constant_integer(right)
                        if left_value is not None and right_value is not None:
                            return left_value == right_value
                        return strip_outer_parentheses(
                            left.strip()
                        ) == strip_outer_parentheses(right.strip())

                    matching_initializers: dict[
                        int, list[tuple[int, int, int]]
                    ] = {}
                    assignments: dict[int, list[tuple[int, int, int, str]]] = {}
                    for block_index, block in enumerate(self.blocks):
                        if block_index in self.omitted_blocks:
                            continue
                        for statement_index, statement in enumerate(block.statements):
                            assignment = assignment_pattern.fullmatch(statement.text.strip())
                            if assignment is None:
                                continue
                            index = int(assignment.group("index"), 10)
                            assignments.setdefault(index, []).append(
                                (
                                    block_index,
                                    statement_index,
                                    id(statement),
                                    assignment.group("value"),
                                )
                            )
                            if (
                                index >= len(copy.values)
                                or not same_initializer(
                                    assignment.group("value"), copy.values[index]
                                )
                            ):
                                continue
                            matching_initializers.setdefault(index, []).append(
                                (block_index, statement_index, id(statement))
                            )

                    def assigned_before(
                        index: int,
                        block_index: int,
                        statement_index: int,
                    ) -> bool:
                        return any(
                            self.builder.cfg.dominates(assignment_block, block_index)
                            and (
                                assignment_block != block_index
                                or assignment_position < statement_index
                            )
                            for assignment_block, assignment_position, _, _
                            in assignments.get(index, ())
                        )

                    def can_reach(source_block: int, target_block: int) -> bool:
                        pending = [source_block]
                        visited: set[int] = set()
                        while pending:
                            current = pending.pop()
                            if current == target_block:
                                return True
                            if current in visited:
                                continue
                            visited.add(current)
                            pending.extend(self.builder.cfg.successors.get(current, ()))
                        return False

                    dominates_uses = True
                    for block_index, block in enumerate(self.blocks):
                        if block_index in self.omitted_blocks:
                            continue
                        for statement_index, statement in enumerate(block.statements):
                            if id(statement) == copy.statement_id:
                                continue
                            if not any(
                                pattern.search(source) is not None
                                for source in (statement.text, statement.condition or "")
                            ):
                                continue
                            copy_dominates = self.builder.cfg.dominates(
                                copy_block, block_index
                            ) and (
                                block_index != copy_block
                                or statement_index > copy_position
                            )
                            if copy_dominates:
                                continue
                            assignment = assignment_pattern.fullmatch(
                                statement.text.strip()
                            )
                            if assignment is not None:
                                index = int(assignment.group("index"), 10)
                                if index < len(copy.values):
                                    continue
                            safe_outside_use = True
                            for source in (statement.text, statement.condition or ""):
                                if pattern.search(source) is None:
                                    continue
                                indices = [
                                    int(match.group("index"), 10)
                                    for match in indexed_pattern.finditer(source)
                                ]
                                if (
                                    not indices
                                    or pattern.search(indexed_pattern.sub("", source))
                                    is not None
                                    or not all(
                                        assigned_before(
                                            index, block_index, statement_index
                                        )
                                        for index in indices
                                    )
                                ):
                                    safe_outside_use = False
                                    break
                            if not safe_outside_use:
                                dominates_uses = False
                                break
                        if not dominates_uses:
                            break
                    if dominates_uses:
                        statement = all_statements_by_id[copy.statement_id]
                        redundant_initializers = {
                            statement_id
                            for initializers in matching_initializers.values()
                            for initializer_block, initializer_position, statement_id
                            in initializers
                            if self.builder.cfg.dominates(initializer_block, copy_block)
                            and (
                                initializer_block != copy_block
                                or initializer_position < copy_position
                            )
                        }
                        source_lines = tuple(
                            dict.fromkeys(
                                line
                                for statement_id in [
                                    copy.statement_id,
                                    *sorted(
                                        redundant_initializers,
                                        key=lambda item: locations[item],
                                    ),
                                ]
                                for line in all_statements_by_id[statement_id].source_lines
                            )
                        )
                        initializers[array.base] = (
                            list(copy.values),
                            source_lines or statement.source_lines,
                        )
                        overwritten_indices = {
                            index
                            for index, initializer in enumerate(copy.values)
                            if any(
                                not same_initializer(value, initializer)
                                and (
                                    assignment_block != copy_block
                                    or assignment_position < copy_position
                                )
                                and can_reach(assignment_block, copy_block)
                                for assignment_block, assignment_position, _, value
                                in assignments.get(index, ())
                            )
                        }
                        if overwritten_indices:
                            restores[copy.statement_id] = [
                                (index, copy.values[index])
                                for index in sorted(overwritten_indices)
                            ]
                        else:
                            folded.add(copy.statement_id)
                        folded.update(redundant_initializers)

            clear_candidates = [
                (statement_id, clear)
                for statement_id, clear in self.local_array_clears.items()
                if clear[0] == array.base
                and statement_id in positions
                and clear[1] == 0
                and clear[2] >= array.count * array.element_size
            ]
            for statement_id, _ in sorted(clear_candidates, key=lambda item: positions[item[0]]):
                clear_position = positions[statement_id]
                earlier_reference = any(
                    re.search(rf"\b{re.escape(array.base)}\b", statement.text)
                    for statement in first_block.statements[:clear_position]
                )
                copy_positions = [
                    positions[copy.statement_id]
                    for copy in copies
                    if copy.statement_id in positions
                ]
                if earlier_reference or (copy_positions and clear_position > min(copy_positions)):
                    continue
                folded.add(statement_id)
                break

        return initializers, folded, restores

    def _recover_entry_string_initializers(self) -> dict[str, tuple[str, tuple[int, ...]]]:
        if not self.blocks:
            return {}
        first_block = self.blocks[0]
        initializers: dict[str, tuple[str, tuple[int, ...]]] = {}
        for position, statement in enumerate(first_block.statements):
            text = statement.text.strip()
            if not text.startswith("strcpy(") or not text.endswith(");"):
                continue
            arguments = split_arguments(text[len("strcpy("):-2])
            if len(arguments) != 2 or not arguments[1].strip().startswith('"'):
                continue
            target = strip_outer_parentheses(arguments[0])
            match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
            if match is None:
                continue
            base = match.group("base")
            array = self.local_array_by_base.get(base)
            if array is None or array.is_parameter or array.type_id != 0:
                continue
            indexed = local_indexed_address(target, 1, allow_bare=False)
            if indexed is None or constant_integer(indexed[1]) != 0:
                continue
            if base in self.local_array_initializers or base in initializers:
                continue
            if any(
                re.search(rf"\b{re.escape(base)}\b", earlier.text)
                for earlier in first_block.statements[:position]
            ):
                continue
            initializers[base] = (arguments[1].strip(), statement.source_lines)
            self.folded_array_statements.add(id(statement))
        return initializers

    def _collect_block_depths(self, nodes: list[Region], depth: int = 0) -> dict[int, int]:
        depths: dict[int, int] = {}

        def record(index: int, value: int) -> None:
            depths[index] = min(depths.get(index, value), value)

        for node in nodes:
            if isinstance(node, BlockRegion):
                record(node.index, depth)
            elif isinstance(node, DispatchRegion):
                for header in node.headers:
                    record(header, depth)
            elif isinstance(node, IfRegion):
                record(node.header, depth)
                depths.update(self._collect_block_depths(node.then_nodes, depth + 1))
                depths.update(self._collect_block_depths(node.else_nodes, depth + 1))
            elif isinstance(node, WhileRegion):
                record(node.header, depth + (1 if node.header_has_body else 0))
                depths.update(self._collect_block_depths(node.body_nodes, depth + 1))
            elif isinstance(node, DoWhileRegion):
                depths.update(self._collect_block_depths(node.body_nodes, depth + 1))
            elif isinstance(node, DisabledRegion):
                record(node.header, depth)
        return depths

    def _infer_parameters(self) -> tuple[list[tuple[str, str | int]], set[int]]:
        parameters: list[tuple[str, str | int]] = []
        statements: set[int] = set()
        saw_parameter = False
        scanning = True
        for block in self.blocks:
            if not scanning:
                break
            for statement in block.statements:
                if statement.kind == "state":
                    continue
                text = statement.text.strip()
                if not saw_parameter and ENTRY_PARAMETER_PRELUDE_RE.match(text):
                    continue
                match = PARAM_ASSIGN_RE.fullmatch(text)
                if match is not None:
                    parameters.append(("local", match.group(1)))
                    statements.add(id(statement))
                    saw_parameter = True
                    continue
                stack_argument = PARAM_VM_ARG_RE.fullmatch(text)
                if stack_argument is not None:
                    parameters.append(("stack", int(stack_argument.group("index"))))
                    saw_parameter = True
                    continue
                scanning = False
                break
            if block.statements and not scanning:
                break
        parameters.reverse()
        return parameters, statements

    def _fold_dead_materialized_call_results(self) -> None:
        """删除不再有栈消费者的调用结果。"""
        if any(
            id(statement) not in self.parameter_statements
            and "vm_pop(vm)" in "\n".join((statement.text, statement.condition or ""))
            for block in self.blocks
            for statement in block.statements
        ):
            return
        statements = [statement for block in self.blocks for statement in block.statements]
        block_by_statement = {
            id(statement): block
            for block in self.blocks
            for statement in block.statements
        }
        references_by_name: dict[str, list[Statement]] = {}
        for statement in statements:
            names = set(MATERIALIZED_CALL_RESULT_NAME_RE.findall(statement.text))
            if statement.condition is not None:
                names.update(
                    MATERIALIZED_CALL_RESULT_NAME_RE.findall(statement.condition)
                )
            for name in names:
                references_by_name.setdefault(name, []).append(statement)

        for definition in list(statements):
            assignment = MATERIALIZED_CALL_RESULT_RE.fullmatch(definition.text.strip())
            if assignment is None:
                continue
            name = assignment.group("name")
            references = references_by_name.get(name, [])
            discards = [
                statement
                for statement in references
                if statement is not definition
                and (pushed := VM_PUSH_RE.fullmatch(statement.text.strip())) is not None
                and pushed.group("value").strip() == name
            ]
            if len(references) != 2 or len(discards) != 1:
                continue
            definition.kind = "effect"
            definition.text = f"(void)({assignment.group('value')});"
            definition.node_kind = StatementKind.UNKNOWN
            definition.effects = Effect.PURE
            definition.__post_init__()
            discarded = discards[0]
            block_by_statement[id(discarded)].statements.remove(discarded)
            statements.remove(discarded)

    def _build_variable_names(self) -> dict[str, str]:
        ordered: list[str] = list(self.parameter_locals)
        deferred_edge_values: list[str] = []
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                if id(statement) in self.function_table_statements:
                    continue
                for match in VARIABLE_RE.finditer(statement.text):
                    name = match.group(0)
                    if name.startswith("edge_phi_"):
                        if name not in deferred_edge_values:
                            deferred_edge_values.append(name)
                        continue
                    if name not in ordered:
                        ordered.append(name)
                for source in (statement.text, statement.condition or ""):
                    for call_name, arguments in iter_function_calls(source):
                        if call_name != "load_indirect_indexed" or len(arguments) != 3:
                            continue
                        try:
                            references = (int(arguments[0], 0), int(arguments[1], 0))
                        except ValueError:
                            continue
                        for reference in references:
                            name = f"local_{reference & 0x3FFF:04x}"
                            if name not in ordered:
                                ordered.append(name)
                if statement.condition:
                    for match in VARIABLE_RE.finditer(statement.condition):
                        name = match.group(0)
                        if name.startswith("edge_phi_"):
                            if name not in deferred_edge_values:
                                deferred_edge_values.append(name)
                            continue
                        if name not in ordered:
                            ordered.append(name)
        ordered.extend(
            name
            for name in sorted(
                self.overlapping_local_array_members,
                key=lambda item: int(item.split("_", 1)[1], 16),
            )
            if name not in ordered and name not in self.parameter_locals
        )
        ordered.extend(deferred_edge_values)
        script_names: dict[str, set[str]] = {}
        result_names: dict[str, str] = {}
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                if id(statement) in self.function_table_statements:
                    continue
                match = SCRIPT_ENTRY_ASSIGN_RE.search(statement.text.strip())
                if match is None:
                    continue
                script_match = SCRIPT_NAME_RE.search(match.group("args"))
                candidate = script_match.group("name").lower() + "_entry" if script_match else "script_entry"
                script_names.setdefault(match.group("name"), set()).add(sanitize_identifier(candidate))
                continue
            for statement in block.statements:
                result = RESULT_SCHEMA_RE.search(statement.text)
                if result is None:
                    continue
                for schema in MULTI_RESULT_SCHEMAS.values():
                    if schema.type_name == result.group("type"):
                        result_names[result.group("name")] = schema.variable_name
                        break

        names: dict[str, str] = {}
        used_semantic_names: set[str] = set()
        for index, name in enumerate(ordered, start=self.variable_index_start):
            candidates = script_names.get(name)
            semantic_result = result_names.get(name)
            if not candidates and semantic_result is None:
                names[name] = f"v{index}"
                continue
            if semantic_result is not None:
                base = semantic_result
            else:
                base = next(iter(candidates)) if candidates is not None and len(candidates) == 1 else "script_entry"
            semantic_name = base
            suffix = 2
            while semantic_name in used_semantic_names:
                semantic_name = f"{base}{suffix}"
                suffix += 1
            names[name] = semantic_name
            used_semantic_names.add(semantic_name)
        return names

    def _infer_stack_parameter_indices(self) -> list[int]:
        indices = {
            int(match.group("index"))
            for block_index, block in enumerate(self.blocks)
            if block_index not in self.omitted_blocks
            for statement in block.statements
            for text in (statement.text, statement.condition or "")
            for match in VM_ARGUMENT_RE.finditer(text)
        }
        return sorted(indices)

    def prune_unused_stack_parameters(self) -> bool:
        """Drop stack-slot parameters removed by direct-call normalization."""
        used = set(self._infer_stack_parameter_indices())
        previous = list(self.parameter_bindings)
        self.parameter_bindings = [
            (kind, parameter)
            for kind, parameter in self.parameter_bindings
            if kind != "stack" or parameter in used
        ]
        self.stack_parameter_indices = [
            index for index in self.stack_parameter_indices if index in used
        ]
        return self.parameter_bindings != previous

    def _infer_variable_types(self) -> dict[str, str]:
        assignments: dict[str, list[str]] = {}
        script_entries: set[str] = set()
        scalar_copy_types: dict[str, set[str]] = {}
        primitive_pointer_ids: dict[str, set[int]] = {}
        scalar_arithmetic_names: set[str] = set()
        bounded_scalar_names: set[str] = set()
        numeric_comparison_names: set[str] = set()
        scalar_literal_names: set[str] = set()
        semantic_types: dict[str, set[str]] = {}
        stack_types: dict[int, set[str]] = {}
        widths = {"i8": 0, "i16": 1, "i32": 2, "i64": 3}

        def constrain(text: str, c_type: str) -> None:
            token = strip_outer_parentheses(text)
            vm_arg = VM_ARG_EXACT_RE.fullmatch(token)
            if vm_arg is not None:
                stack_types.setdefault(int(vm_arg.group("index")), set()).add(c_type)
                return
            if LOCAL_EXACT_RE.fullmatch(token):
                semantic_types.setdefault(token, set()).add(c_type)
                return
            # An integer offset added to an absolute VM address is not a C
            # pointer variable.  Treat its varying terms as words so a call
            # such as ``strcpy(length + 0x149a64, source)`` does not turn the
            # result of ``strlen`` into ``char *`` during type solving.
            absolute_indexed = (
                absolute_pointer_index(token, 1) if c_type.endswith("*") else None
            )
            if absolute_indexed is not None:
                for name in VARIABLE_RE.findall(absolute_indexed[1]):
                    semantic_types.setdefault(name, set()).add("int32_t")
                return
            indexed = VALUE_INDEXED_TARGET_RE.fullmatch(token)
            if indexed is not None and not indexed.group("address"):
                semantic_types.setdefault(indexed.group("base"), set()).add(c_type)

        def constrain_sprintf(arguments: list[str]) -> None:
            if len(arguments) < 2:
                return
            format_text = strip_outer_parentheses(arguments[1])
            if not (format_text.startswith('"') and format_text.endswith('"')):
                return
            try:
                format_value = ast.literal_eval(format_text)
            except (SyntaxError, ValueError):
                return
            if not isinstance(format_value, str):
                return
            extra_index = 2
            for match in re.finditer(r"%(?:[-+#0 ]*\d*(?:\.\d+)?)?([diouxXfFeEgGcs])", format_value):
                if extra_index >= len(arguments):
                    break
                if match.group(1) == "s":
                    constrain(arguments[extra_index], "const char *")
                extra_index += 1

        def contains_nonzero_scalar_literal(text: str) -> bool:
            token = strip_outer_parentheses(text.strip())
            value = constant_integer(token)
            if value is not None:
                return value != 0
            conditional = split_top_level_ternary(token)
            if conditional is None:
                return False
            return any(
                contains_nonzero_scalar_literal(branch)
                for branch in conditional[1:]
            )

        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                script_entry = SCRIPT_ENTRY_ASSIGN_RE.search(statement.text.strip())
                if script_entry is not None:
                    script_entries.add(script_entry.group("name"))
                match = VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())
                if match is not None:
                    assignments.setdefault(match.group("name"), []).append(match.group("value").strip())
                    if contains_nonzero_scalar_literal(match.group("value")):
                        scalar_literal_names.add(match.group("name"))
                    if match.group("value").strip().startswith('"'):
                        scalar_copy_types.setdefault(match.group("name"), set()).add("const char *")
                    elif FUNCTION_TABLE_ENTRY_RE.fullmatch(match.group("value").strip()):
                        scalar_copy_types.setdefault(match.group("name"), set()).add(
                            "BPDEntryPoint"
                        )
                parsed_copy = self._parse_local_array_copy(statement)
                if parsed_copy is not None:
                    base, _, start_index, values, bare_target = parsed_copy
                    if start_index == 0 and len(values) == 1 and not bare_target:
                        value = values[0].strip()
                        # A one-word copystack is an ordinary assignment in
                        # the VM.  Include it in the same propagation graph
                        # so address copies do not leave an integer-typed
                        # destination or contaminate the source array type.
                        assignments.setdefault(base, []).append(value)
                        if contains_nonzero_scalar_literal(value):
                            scalar_literal_names.add(base)
                        if value.startswith('"'):
                            scalar_copy_types.setdefault(base, set()).add("const char *")
                        elif FUNCTION_TABLE_ENTRY_RE.fullmatch(value):
                            scalar_copy_types.setdefault(base, set()).add("BPDEntryPoint")
                for source in (statement.text, statement.condition or ""):
                    for arithmetic in SCALAR_ARITHMETIC_VARIABLE_RE.finditer(source):
                        scalar_arithmetic_names.add(
                            arithmetic.group("left") or arithmetic.group("right")
                        )
                    for comparison in re.finditer(
                        r"\b(?P<name>local_[0-9A-Fa-f]+|result_L\d+(?:_\d+)?)\b\s*"
                        r"(?:<=|<|>=|>)\s*-?(?:0[xX][0-9A-Fa-f]+|\d+)\b",
                        source,
                    ):
                        bounded_scalar_names.add(comparison.group("name"))
                    for comparison in re.finditer(
                        r"\b(?P<name>local_[0-9A-Fa-f]+|result_L\d+(?:_\d+)?)\b\s*"
                        r"(?:==|!=|<=|<|>=|>)\s*"
                        r"(?P<value>-?(?:0[xX][0-9A-Fa-f]+|\d+))\b",
                        source,
                    ):
                        if constant_integer(comparison.group("value")) != 0:
                            numeric_comparison_names.add(comparison.group("name"))
                    for comparison in re.finditer(
                        r"(?P<value>-?(?:0[xX][0-9A-Fa-f]+|\d+))\s*"
                        r"(?:==|!=|<=|<|>=|>)\s*"
                        r"\b(?P<name>local_[0-9A-Fa-f]+|result_L\d+(?:_\d+)?)\b",
                        source,
                    ):
                        if constant_integer(comparison.group("value")) != 0:
                            numeric_comparison_names.add(comparison.group("name"))
                    for call_name, _ in iter_function_calls(source):
                        if LOCAL_EXACT_RE.fullmatch(call_name):
                            semantic_types.setdefault(call_name, set()).add(
                                "BPDEntryPoint"
                            )
                    for call_name, arguments in iter_function_calls(source):
                        type_id: int | None = None
                        address: str | None = None
                        if call_name == "load_typed" and len(arguments) == 2:
                            try:
                                type_id = int(arguments[0], 0) & 3
                            except ValueError:
                                continue
                            address = arguments[1]
                        elif call_name == "load_indexed" and len(arguments) == 3:
                            try:
                                type_id = int(arguments[1], 0) & 3
                            except ValueError:
                                continue
                            address = f"{arguments[0]} + {arguments[2]}"
                        else:
                            access = re.fullmatch(
                                r"(?:load|store)_(?P<width>i8|i16|i32|i64)",
                                call_name,
                            )
                            if access is None or len(arguments) not in {1, 2}:
                                continue
                            type_id = widths[access.group("width")]
                            address = arguments[0]
                        target = strip_outer_parentheses(address)
                        if target.startswith("&"):
                            continue
                        indexed = value_indexed_address(
                            target,
                            {0: 1, 1: 2, 2: 4, 3: 8}[type_id],
                            allow_bare=True,
                        )
                        if indexed is not None:
                            primitive_pointer_ids.setdefault(indexed[0], set()).add(type_id)
                    for call_name, arguments in iter_function_calls(source):
                        if call_name == "sprintf":
                            constrain_sprintf(arguments)
                        for argument_index, c_type in STRING_ARGUMENT_TYPES.get(call_name, {}).items():
                            if argument_index < len(arguments):
                                constrain(arguments[argument_index], c_type)

        inferred: dict[str, str] = {name: "BPDProgramEntry" for name in script_entries}
        inferred.update(
            (name, next(iter(types)))
            for name, types in scalar_copy_types.items()
            if len(types) == 1
        )
        for name, values in assignments.items():
            if name in inferred:
                continue
            pointer_types: set[str] = set()
            for value in values:
                pointer = STRUCTURE_POINTER_RE.match(value)
                if pointer is None:
                    pointer_types.clear()
                    break
                pointer_type = structure_pointer_type(pointer.group("array"))
                if pointer_type is None:
                    pointer_types.clear()
                    break
                pointer_types.add(pointer_type)
            if len(pointer_types) == 1:
                inferred[name] = pointer_types.pop()
        primitive_types = {0: "int8_t *", 1: "int16_t *", 2: "int32_t *", 3: "int64_t *"}
        for name, type_ids in primitive_pointer_ids.items():
            if name in inferred or name in self.local_array_bases or not type_ids:
                continue
            numeric_constraints = {
                value
                for value in semantic_types.get(name, set())
                if not value.endswith("*")
            }
            if name in bounded_scalar_names and any(
                constant_integer(value) is not None
                for value in assignments.get(name, ())
            ):
                inferred[name] = "int32_t"
                continue
            if (
                name in scalar_arithmetic_names
                or (
                    name in scalar_literal_names
                    and name in numeric_comparison_names
                )
                or numeric_constraints
            ):
                # A VM word reused both as an address and as an arithmetic
                # scalar is not a C typed pointer.  ``intptr_t`` preserves
                # both roles without making expressions such as index * 104
                # invalid C.
                inferred[name] = "intptr_t"
                continue
            if len(type_ids) == 1:
                inferred[name] = primitive_types[next(iter(type_ids))]
            else:
                inferred[name] = "uint8_t *"
        for name, types in semantic_types.items():
            candidate = merge_c_types(types)
            if candidate is None:
                continue
            current = inferred.get(name)
            merged = merge_c_types([current, candidate] if current is not None else [candidate])
            if merged is not None:
                inferred[name] = merged

        # Propagate types through ordinary assignments after usage-derived
        # constraints are available.  This catches selects such as
        # ``condition ? "a" : previous_name`` as well as known pointer-return
        # functions without treating a string used inside a call as the
        # result type of the whole expression.
        for _ in range(max(2, len(assignments))):
            changed = False
            for name, values in assignments.items():
                candidates = [
                    _infer_text_c_type(
                        self._normalize_local_array_pointer_argument(value),
                        lambda token: inferred.get(token),
                        self.local_array_by_base,
                    )
                    for value in values
                ]
                candidate = merge_c_types(
                    [
                        value
                        for value in candidates
                        if value is not None and value != "int32_t"
                    ]
                )
                if candidate is None:
                    continue
                current = inferred.get(name)
                merged = merge_c_types(
                    [current, candidate] if current is not None else [candidate]
                )
                if merged is not None and merged != current:
                    inferred[name] = merged
                    changed = True
            if not changed:
                break
        for name in scalar_literal_names:
            if inferred.get(name, "").endswith("*") and (
                name in scalar_arithmetic_names
                or name in bounded_scalar_names
                or name in numeric_comparison_names
            ):
                # Call signatures may add a pointer constraint after the
                # usage pass.  A BP word that also carries a non-zero numeric
                # selector or sentinel remains an integer-sized VM value.
                inferred[name] = "intptr_t"
        self.stack_parameter_type_hints = stack_types
        return inferred

    def _refine_pointer_word_array_types(self) -> None:
        """含地址的混合字记录使用 intptr_t。"""
        candidates = {
            array.base: array
            for array in self.local_arrays
            if array.type_id in {2, 3}
            and array.c_type in {"int32_t", "int64_t"}
        }
        if not candidates:
            return
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                rendered = self._replace_local_array_accesses(statement.text).strip()
                assignment = LOCAL_ARRAY_ASSIGNMENT_RE.fullmatch(rendered)
                if assignment is None:
                    continue
                array = candidates.get(assignment.group("base"))
                if array is None:
                    continue
                value_type = _infer_text_c_type(
                    assignment.group("value"),
                    lambda name: self.variable_types.get(name),
                    self.local_array_by_base,
                    self.stack_parameter_type_hints,
                )
                if value_type is not None and (
                    value_type.endswith("*")
                    or value_type in {"BPDEntryPoint", "BPDProgramEntry"}
                ):
                    array.c_type = "intptr_t"

    def _find_inline_declarations(self) -> dict[int, str]:
        active = set(range(len(self.blocks))).difference(self.omitted_blocks)
        predecessors: dict[int, set[int]] = {index: set() for index in active}
        index_by_label = {block.label: index for index, block in enumerate(self.blocks)}
        for source_index in active:
            for label in self.blocks[source_index].successors:
                target_index = index_by_label.get(label)
                if target_index in active:
                    predecessors[target_index].add(source_index)

        dominators: dict[int, set[int]] = {
            index: ({index} if not predecessors[index] else set(active)) for index in active
        }
        changed = True
        while changed:
            changed = False
            for index in sorted(active):
                incoming = predecessors[index]
                updated = {index}
                if incoming:
                    common = set(active)
                    for predecessor in incoming:
                        common.intersection_update(dominators[predecessor])
                    updated.update(common)
                if updated != dominators[index]:
                    dominators[index] = updated
                    changed = True

        locations: dict[str, list[tuple[int, int, Statement]]] = {}
        definitions: dict[str, list[tuple[int, int, Statement]]] = {}
        for block_index in sorted(active):
            for statement_index, statement in enumerate(self.blocks[block_index].statements):
                if (
                    id(statement) in self.parameter_statements
                    or id(statement) in self.function_table_statements
                    or statement.kind == "state"
                ):
                    continue
                text = statement.condition if statement.condition is not None else statement.text
                for name in dict.fromkeys(VARIABLE_RE.findall(text)):
                    locations.setdefault(name, []).append((block_index, statement_index, statement))
                assignment = VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())
                if assignment is not None:
                    definitions.setdefault(assignment.group("name"), []).append(
                        (block_index, statement_index, statement)
                    )

        inline: dict[int, str] = {}
        candidates = [
            name
            for name in self.variable_names
            if (LOCAL_RE.fullmatch(name) or name.startswith("phi_"))
            and name not in self.parameter_locals
            and name not in self.function_table_targets
            and name not in self.local_array_bases
        ]
        for name in candidates:
            uses = locations.get(name, [])
            writes = definitions.get(name, [])
            if not uses or not writes:
                continue
            for block_index, statement_index, statement in writes:
                if (
                    self.block_depths.get(block_index, 1) != 0
                    or self.blocks[block_index].label in self.remaining_targets
                ):
                    continue
                assignment = VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())
                if assignment is None or re.search(
                    rf"\b{re.escape(name)}\b",
                    assignment.group("value"),
                ):
                    continue
                position = (block_index, statement_index)
                if any((other_block, other_index) < position for other_block, other_index, _ in uses):
                    continue
                if any(
                    block_index not in dominators.get(other_block, {other_block})
                    for other_block, _, _ in uses
                ):
                    continue
                inline[id(statement)] = name
                break
        return inline

    def downgrade_mixed_parameter_arrays(self) -> bool:
        """Use a VM-word type when a parameter is both an address and a scalar.

        Array recovery follows typed loads and callees, while BP is free to
        reuse that same incoming word for a numeric selector or sentinel.
        Declaring such a slot as a C array makes later scalar assignments and
        comparisons misleading or constraint-invalid.  Keep true pointer
        rebindings (NULL, addresses and pointer-valued selects) as arrays,
        but represent genuinely mixed slots as ``intptr_t``.
        """
        mixed: set[str] = set()
        parameter_arrays = {
            array.base: array
            for array in self.local_arrays
            if array.is_parameter
        }
        if not parameter_arrays:
            return False

        for name in parameter_arrays:
            solved = self.variable_types.get(name)
            if solved is not None and not (
                solved.endswith("*")
                or solved in {"BPDEntryPoint", "BPDProgramEntry", "intptr_t"}
            ):
                mixed.add(name)
                continue
            numeric_comparison = re.compile(
                rf"(?:\b{re.escape(name)}\b\s*(?:==|!=|<=|<|>=|>)\s*"
                rf"(?P<right>-?(?:0[xX][0-9A-Fa-f]+|\d+))\b|"
                rf"(?P<left>-?(?:0[xX][0-9A-Fa-f]+|\d+))\s*"
                rf"(?:==|!=|<=|<|>=|>)\s*\b{re.escape(name)}\b)"
            )
            if any(
                (match := numeric_comparison.search(source)) is not None
                and constant_integer(match.group("right") or match.group("left")) != 0
                for block_index, block in enumerate(self.blocks)
                if block_index not in self.omitted_blocks
                for statement in block.statements
                for source in (statement.text, statement.condition or "")
            ):
                mixed.add(name)
                continue
            for block_index, block in enumerate(self.blocks):
                if block_index in self.omitted_blocks:
                    continue
                for statement in block.statements:
                    if id(statement) in self.parameter_statements:
                        continue
                    assignment = VARIABLE_ASSIGN_RE.fullmatch(statement.text.strip())
                    if assignment is None or assignment.group("name") != name:
                        continue
                    value = assignment.group("value").strip()
                    if constant_integer(value) == 0:
                        continue
                    value_type = _infer_text_c_type(
                        value,
                        lambda token: self.variable_types.get(token),
                        self.local_array_by_base,
                        self.stack_parameter_type_hints,
                    )
                    if value_type is not None and (
                        value_type.endswith("*")
                        or value_type in {"BPDEntryPoint", "BPDProgramEntry"}
                    ):
                        continue
                    mixed.add(name)
                    break
                if name in mixed:
                    break

        if not mixed:
            return False
        removed_copy_ids = {
            statement_id
            for statement_id, copy in self.local_array_copies.items()
            if copy.base in mixed
        }
        removed_clear_ids = {
            statement_id
            for statement_id, clear in self.local_array_clears.items()
            if clear[0] in mixed
        }
        self.local_arrays = [
            array for array in self.local_arrays if array.base not in mixed
        ]
        self.local_array_by_base = {
            array.base: array for array in self.local_arrays
        }
        self.local_array_bases = set(self.local_array_by_base)
        self.local_array_copies = {
            statement_id: copy
            for statement_id, copy in self.local_array_copies.items()
            if statement_id not in removed_copy_ids
        }
        self.local_array_clears = {
            statement_id: clear
            for statement_id, clear in self.local_array_clears.items()
            if statement_id not in removed_clear_ids
        }
        self.folded_array_statements.difference_update(
            removed_copy_ids | removed_clear_ids
        )
        for statement_id in removed_copy_ids:
            self.array_copy_restores.pop(statement_id, None)
        for name in mixed:
            self.local_array_initializers.pop(name, None)
            self.local_string_initializers.pop(name, None)
        for name in mixed:
            self.variable_types[name] = "intptr_t"
        self._reference_cache.clear()
        return True

    def _recover_assignment_runs(self) -> list[AssignmentRun]:
        runs: list[AssignmentRun] = []
        assignment = re.compile(
            r"^(?P<target>.+)\[(?P<index>\d+)\]\s*=\s*(?P<value>.+);$"
        )
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            statements = [
                statement
                for statement in block.statements
                if statement.kind not in {"state", "trace"}
            ]
            parsed = [
                assignment.fullmatch(self._replace_references(statement.text).strip())
                for statement in statements
            ]
            cursor = 0
            while cursor < len(statements):
                first = parsed[cursor]
                if first is None:
                    cursor += 1
                    continue
                target = first.group("target")
                value = first.group("value")
                start_index = int(first.group("index"))
                grouped = [statements[cursor]]
                next_index = start_index + 1
                scan = cursor + 1
                while scan < len(statements):
                    current = parsed[scan]
                    if (
                        current is None
                        or current.group("target") != target
                        or current.group("value") != value
                        or int(current.group("index")) != next_index
                    ):
                        break
                    grouped.append(statements[scan])
                    next_index += 1
                    scan += 1
                if len(grouped) < 4:
                    cursor += 1
                    continue
                loop_variable = f"v{self.next_variable_index}"
                self.next_variable_index += 1
                source_lines = tuple(
                    dict.fromkeys(line for statement in grouped for line in statement.source_lines)
                )
                runs.append(
                    AssignmentRun(
                        [id(statement) for statement in grouped],
                        target,
                        start_index,
                        next_index,
                        value,
                        loop_variable,
                        source_lines,
                    )
                )
                cursor = scan
        return runs

    def _omitted_block_indices(self, nodes: list[Region]) -> set[int]:
        omitted: set[int] = set()
        for node in nodes:
            if isinstance(node, DisabledRegion):
                omitted.update(self._region_indices(node.body_nodes))
            elif isinstance(node, IfRegion):
                omitted.update(self._omitted_block_indices(node.then_nodes))
                omitted.update(self._omitted_block_indices(node.else_nodes))
            elif isinstance(node, WhileRegion):
                omitted.update(self._omitted_block_indices(node.body_nodes))
            elif isinstance(node, DoWhileRegion):
                omitted.update(self._omitted_block_indices(node.body_nodes))
        return omitted

    def _region_indices(self, nodes: list[Region]) -> set[int]:
        indices: set[int] = set()
        for node in nodes:
            if isinstance(node, BlockRegion):
                indices.add(node.index)
            elif isinstance(node, DispatchRegion):
                indices.update(node.headers)
            elif isinstance(node, IfRegion):
                indices.add(node.header)
                indices.update(self._region_indices(node.then_nodes))
                indices.update(self._region_indices(node.else_nodes))
            elif isinstance(node, WhileRegion):
                indices.add(node.header)
                indices.update(self._region_indices(node.body_nodes))
            elif isinstance(node, DoWhileRegion):
                indices.update(self._region_indices(node.body_nodes))
            elif isinstance(node, DisabledRegion):
                indices.add(node.header)
                indices.update(self._region_indices(node.body_nodes))
        return indices

    def _mark_loop_controls(
        self,
        indices: set[int],
        *,
        continue_label: str | None,
        exit_label: str | None,
    ) -> None:
        for index in indices:
            for statement in self.blocks[index].statements:
                if id(statement) in self.builder.suppressed or id(statement) in self.structured_controls:
                    continue
                if statement.kind not in {"branch", "jump"}:
                    continue
                if continue_label is not None and statement.target == continue_label:
                    self.structured_controls[id(statement)] = "continue"
                elif exit_label is not None and statement.target == exit_label:
                    self.structured_controls[id(statement)] = "break"

    def _nested_loop_indices(self, nodes: list[Region]) -> set[int]:
        indices: set[int] = set()
        for node in nodes:
            if isinstance(node, (WhileRegion, DoWhileRegion)):
                indices.update(self._region_indices([node]))
            elif isinstance(node, IfRegion):
                indices.update(self._nested_loop_indices(node.then_nodes))
                indices.update(self._nested_loop_indices(node.else_nodes))
            elif isinstance(node, DisabledRegion):
                indices.update(self._nested_loop_indices(node.body_nodes))
        return indices

    def _recover_loop_controls(self, nodes: list[Region]) -> None:
        for node in nodes:
            if isinstance(node, IfRegion):
                self._recover_loop_controls(node.then_nodes)
                self._recover_loop_controls(node.else_nodes)
            elif isinstance(node, DisabledRegion):
                self._recover_loop_controls(node.body_nodes)
            elif isinstance(node, WhileRegion):
                self._recover_loop_controls(node.body_nodes)
                self._mark_loop_controls(
                    self._region_indices(node.body_nodes).difference(
                        self._nested_loop_indices(node.body_nodes)
                    ),
                    continue_label=self.blocks[node.header].label,
                    exit_label=(
                        self.blocks[node.exit].label if node.exit is not None else None
                    ),
                )
            elif isinstance(node, DoWhileRegion):
                self._recover_loop_controls(node.body_nodes)
                self._mark_loop_controls(
                    self._region_indices(node.body_nodes).difference(
                        self._nested_loop_indices(node.body_nodes)
                    ),
                    continue_label=self.blocks[node.start].label,
                    exit_label=self.blocks[node.exit].label,
                )

    def _local_array_index(self, address: str, type_id: int) -> tuple[LocalArray, str] | None:
        if self._address_starts_with_loaded_subscript(address):
            return None
        element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
        indexed = local_indexed_address(address, element_size, allow_bare=True)
        if indexed is None:
            return None
        base, index = indexed
        array = self.local_array_by_base.get(base)
        target_is_bare = not strip_outer_parentheses(address).startswith("&")
        if (
            array is None
            or array.type_id != type_id
            or (target_is_bare and not array.is_parameter)
        ):
            return None
        return array, index

    def _primitive_pointer_index(self, address: str, type_id: int) -> tuple[str, str] | None:
        target = strip_outer_parentheses(address)
        if target.startswith("&"):
            return None
        if self._address_starts_with_loaded_subscript(target):
            return None
        try:
            root = ast.parse(target, mode="eval").body
        except SyntaxError:
            root = None
        if isinstance(root, ast.Subscript):
            # The subscript has already loaded a VM word.  A surrounding
            # load/store dereferences that word as an address; treating the
            # original array as the pointee emits both invalid ``[[index]]``
            # syntax and the wrong memory access.
            return None
        indexed = value_indexed_address(
            target,
            {0: 1, 1: 2, 2: 4, 3: 8}[type_id],
            allow_bare=True,
        )
        if indexed is None:
            return None
        base, index = indexed
        expected = {0: "int8_t *", 1: "int16_t *", 2: "int32_t *", 3: "int64_t *"}[type_id]
        pointer_type = self.variable_types.get(base)
        if pointer_type == expected or (
            type_id == 0 and pointer_type in {"char *", "const char *"}
        ):
            return base, index
        if pointer_type != "uint8_t *":
            return None
        if type_id == 0:
            return base, index
        return f"(({expected.removesuffix(' *')} *){base})", index

    def _local_array_typed_view(self, address: str, type_id: int) -> tuple[str, str] | None:
        if self._address_starts_with_loaded_subscript(address):
            return None
        element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
        target = strip_outer_parentheses(address)
        try:
            root = ast.parse(target, mode="eval").body
        except SyntaxError:
            root = None
        if isinstance(root, ast.Subscript) and not target.startswith("&"):
            return None
        indexed = local_indexed_address(address, element_size, allow_bare=True)
        if indexed is None:
            return None
        base, index = indexed
        array = self.local_array_by_base.get(base)
        target_is_bare = not strip_outer_parentheses(address).startswith("&")
        if array is None or array.type_id == type_id or (target_is_bare and not array.is_parameter):
            return None
        c_type = {0: "int8_t", 1: "int16_t", 2: "int32_t", 3: "int64_t"}[type_id]
        return f"(({c_type} *){base})", index

    @staticmethod
    def _address_starts_with_loaded_subscript(address: str) -> bool:
        """Return whether pointer arithmetic starts from an array element value."""
        try:
            root = ast.parse(strip_outer_parentheses(address), mode="eval").body
        except SyntaxError:
            return False
        for sign, node in _additive_terms(root):
            if sign < 0:
                continue
            if constant_integer(ast.unparse(node)) is not None:
                continue
            return isinstance(node, ast.Subscript)
        return False

    def _typed_dereference(self, address: str, type_id: int) -> str:
        c_type = {0: "int8_t", 1: "int16_t", 2: "int32_t", 3: "int64_t"}[type_id]
        element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
        target = strip_outer_parentheses(address)
        local = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
        if local is not None and local.group("address"):
            indexed = local_indexed_address(target, element_size)
            if (
                indexed is not None
                and constant_integer(indexed[1]) == 0
                and self.variable_types.get(local.group("base"), "int32_t") == c_type
            ):
                return local.group("base")
            tail = local.group("tail").strip()
            suffix = f" {tail}" if tail else ""
            return f"*({c_type} *)((uint8_t *)&{local.group('base')}{suffix})"
        identifiers = set(VARIABLE_RE.findall(target))
        has_pointer_base = any(
            self.variable_types.get(name, "").endswith("*")
            for name in identifiers
        )
        absolute_indexed = (
            None
            if has_pointer_base
            else absolute_pointer_index(target, element_size)
        )
        if absolute_indexed is not None:
            base, index = absolute_indexed
            return f"global_i{element_size * 8}_array_{base:08x}[{index}]"
        indexed = integer_pointer_index(target, element_size)
        if indexed is not None:
            base, index = indexed
            return f"(({c_type} *)(uintptr_t){base})[{index}]"
        absolute_indexed = absolute_pointer_index(target, element_size)
        if absolute_indexed is not None:
            base, index = absolute_indexed
            return f"global_i{element_size * 8}_array_{base:08x}[{index}]"
        absolute = constant_integer(target)
        if absolute is not None and absolute >= 0:
            return f"global_i{element_size * 8}_{absolute:08x}"
        return f"*({c_type} *)(uintptr_t)({target})"

    def _local_byte_pointer(self, argument: str, *, is_const: bool = False) -> str | None:
        """Render an addressed VM local using byte, rather than C-element, offsets."""
        target = strip_outer_parentheses(argument)
        match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
        if match is None:
            return None
        array = self.local_array_by_base.get(match.group("base"))
        if array is not None:
            indexed = local_indexed_address(
                target,
                array.element_size,
                allow_bare=array.is_parameter,
            )
            if indexed is not None:
                return f"&{indexed[0]}[{indexed[1]}]"
        if not match.group("address"):
            return None
        qualifier = "const " if is_const else ""
        tail = match.group("tail")
        if tail.lstrip().startswith("["):
            return f"({qualifier}uint8_t *)(&{match.group('base')}{tail})"
        return f"({qualifier}uint8_t *)(&{match.group('base')}){tail}"

    def _local_string_pointer(self, argument: str) -> str | None:
        indexed = local_indexed_address(argument, 1, allow_bare=True)
        if indexed is None:
            return None
        base, index = indexed
        array = self.local_array_by_base.get(base)
        if array is None or array.type_id != 0:
            return None
        return f"&{base}[{index}]"

    def _vm_string_pointer(self, argument: str, c_type: str) -> str:
        local = self._local_string_pointer(argument)
        if local is not None:
            return local
        token = strip_outer_parentheses(argument)
        local_match = LOCAL_INDEXED_TARGET_RE.fullmatch(token)
        if local_match is not None:
            array = self.local_array_by_base.get(local_match.group("base"))
            if (
                array is not None
                and array.type_id != 0
                and array.c_type not in {"char *", "const char *"}
            ):
                pointer = self._normalize_local_array_pointer_argument(argument)
                return f"({c_type}){pointer}"
        if token.startswith('"') and token.endswith('"'):
            return argument
        absolute = constant_integer(token)
        if absolute == 0:
            return argument
        if absolute is not None and absolute > 0:
            return f"global_i8_array_{absolute:08x}"
        identifiers = set(VARIABLE_RE.findall(token))
        has_pointer_base = any(
            self.variable_types.get(name, "").endswith("*")
            for name in identifiers
        )
        absolute_indexed = (
            None if has_pointer_base else absolute_pointer_index(token, 1)
        )
        if absolute_indexed is not None:
            base, index = absolute_indexed
            return f"global_i8_array_{base:08x} + {index}"
        inferred = _infer_text_c_type(
            token,
            lambda name: self.variable_types.get(name),
            self.local_array_by_base,
            self.stack_parameter_type_hints,
        )
        if inferred is not None and inferred.endswith("*"):
            return argument
        if token.startswith("&") or re.match(
            r"^\((?:const\s+)?(?:char|u?int8_t)\s*\*\)", token
        ):
            return argument
        return f"({c_type})(uintptr_t)({argument})"

    def _vm_memory_pointer(self, argument: str, *, is_const: bool = False) -> str:
        """Render a BP byte address as a legal, readable C pointer."""
        local = self._local_byte_pointer(argument, is_const=is_const)
        if local is not None:
            return local
        token = strip_outer_parentheses(argument)
        absolute = constant_integer(token)
        if absolute is not None and absolute >= 0:
            return f"global_i8_array_{absolute:08x}"
        identifiers = set(VARIABLE_RE.findall(token))
        has_pointer_base = any(
            self.variable_types.get(name, "").endswith("*")
            for name in identifiers
        )
        absolute_indexed = (
            None if has_pointer_base else absolute_pointer_index(token, 1)
        )
        if absolute_indexed is not None:
            base, index = absolute_indexed
            return f"global_i8_array_{base:08x} + {index}"
        inferred = _infer_text_c_type(
            token,
            lambda name: self.variable_types.get(name),
            self.local_array_by_base,
            self.stack_parameter_type_hints,
        )
        if inferred is not None and inferred.endswith("*"):
            return argument
        if token.startswith("&") or re.match(
            r"^\((?:const\s+)?(?:void|char|u?int(?:8|16|32|64)_t)\s*\*\)",
            token,
        ):
            return argument
        qualifier = "const " if is_const else ""
        return f"({qualifier}uint8_t *)(uintptr_t)({argument})"

    def _replace_local_array_call(self, name: str, arguments: list[str]) -> str | None:
        if name == "streq" and len(arguments) == 2:
            left = self._vm_string_pointer(arguments[0], "const char *")
            right = self._vm_string_pointer(arguments[1], "const char *")
            return f"(strcmp({left}, {right}) == 0)"
        if name == "memory_equals" and len(arguments) == 3:
            left = self._vm_memory_pointer(arguments[0], is_const=True)
            right = self._vm_memory_pointer(arguments[1], is_const=True)
            return f"(memcmp({left}, {right}, {arguments[2]}) == 0)"
        if name == "strcpy" and len(arguments) == 2:
            target = self._vm_string_pointer(arguments[0], "char *")
            source = self._vm_string_pointer(arguments[1], "const char *")
            return f"strcpy({target}, {source})"
        if name == "memcpy" and len(arguments) == 3:
            byte_count = constant_integer(arguments[2])
            type_id = {1: 0, 2: 1, 4: 2, 8: 3}.get(byte_count or -1)
            if type_id is not None:
                scalar_names: list[str | None] = []
                for argument in arguments[:2]:
                    match = re.fullmatch(
                        r"&(?P<name>local_[0-9A-Fa-f]+)",
                        strip_outer_parentheses(argument),
                    )
                    scalar_names.append(
                        match.group("name")
                        if match is not None and match.group("name") not in self.local_array_by_base
                        else None
                    )
                if any(scalar_names):
                    target = scalar_names[0] or self._typed_dereference(arguments[0], type_id)
                    value = scalar_names[1] or self._typed_dereference(arguments[1], type_id)
                    return f"{target} = {value}"
            target = self._vm_memory_pointer(arguments[0])
            source = self._vm_memory_pointer(arguments[1], is_const=True)
            return f"memcpy({target}, {source}, {arguments[2]})"
        clear_arguments: tuple[str, str] | None = None
        if name == "memclr" and len(arguments) == 2:
            clear_arguments = (arguments[0], arguments[1])
        elif (
            name == "memset"
            and len(arguments) == 3
            and constant_integer(arguments[1]) == 0
        ):
            clear_arguments = (arguments[0], arguments[2])
        if clear_arguments is not None:
            clear_target, clear_size = clear_arguments
            target = strip_outer_parentheses(clear_target)
            target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(target)
            array = (
                self.local_array_by_base.get(target_match.group("base"))
                if target_match is not None
                else None
            )
            if array is not None:
                indexed = local_indexed_address(
                    target,
                    array.element_size,
                    allow_bare=array.is_parameter,
                )
                byte_count = constant_integer(clear_size)
                if indexed is not None and byte_count is not None:
                    _, index_text = indexed
                    start_index = constant_integer(index_text)
                    if start_index is not None and start_index >= 0:
                        rendered_target = (
                            array.base
                            if start_index == 0
                            else f"&{array.base}[{start_index}]"
                        )
                        if start_index == 0 and byte_count == array.count * array.element_size:
                            size = (
                                f"sizeof({array.base}[0]) * {array.count}"
                                if array.is_parameter
                                else f"sizeof({array.base})"
                            )
                        else:
                            element_count, remainder = divmod(byte_count, array.element_size)
                            if remainder == 0 and element_count > 0:
                                size = (
                                    f"sizeof({array.base}[0])"
                                    if element_count == 1
                                    else f"sizeof({array.base}[0]) * {element_count}"
                                )
                            else:
                                size = clear_size
                        return f"memset({rendered_target}, 0, {size})"
            if (
                target_match is not None
                and target_match.group("address")
                and not target_match.group("tail").strip()
                and target_match.group("base") not in self.local_array_by_base
                and constant_integer(clear_size) == 4
            ):
                return f"{target_match.group('base')} = 0"
            return f"memset({self._vm_memory_pointer(clear_target)}, 0, {clear_size})"
        if name == "memset" and len(arguments) == 3:
            target = self._vm_memory_pointer(arguments[0])
            return f"memset({target}, {arguments[1]}, {arguments[2]})"
        string_positions = STRING_ARGUMENT_TYPES.get(name, {})
        if string_positions:
            rendered = list(arguments)
            changed = False
            for position in string_positions:
                if position >= len(arguments):
                    continue
                pointer = self._local_string_pointer(arguments[position])
                if pointer is not None:
                    rendered[position] = pointer
                    changed = True
                    continue
                token = strip_outer_parentheses(arguments[position])
                indexed = VALUE_INDEXED_TARGET_RE.fullmatch(token)
                if indexed is not None:
                    array = self.local_array_by_base.get(indexed.group("base"))
                    if array is not None and array.c_type == "intptr_t":
                        rendered[position] = (
                            f"({string_positions[position]})(uintptr_t)"
                            f"({arguments[position]})"
                        )
                        changed = True
                        continue
                normalized = self._vm_string_pointer(
                    arguments[position], string_positions[position]
                )
                if normalized != arguments[position]:
                    rendered[position] = normalized
                    changed = True
            if changed:
                return f"{name}({', '.join(rendered)})"
        if name == "load_indirect_indexed" and len(arguments) == 3:
            try:
                base_ref = int(arguments[0], 0)
                index_ref = int(arguments[1], 0)
            except ValueError:
                return None
            type_id = (index_ref >> 14) & 3
            base = f"local_{base_ref & 0x3FFF:04x}"
            index = f"local_{index_ref & 0x3FFF:04x}"
            address = f"&{base} + {index} * {arguments[2]}"
            access = self._local_array_index(address, type_id)
            if access is not None:
                return f"{access[0].base}[{access[1]}]"
            typed_view = self._local_array_typed_view(address, type_id)
            if typed_view is not None:
                return f"{typed_view[0]}[{typed_view[1]}]"
            c_type = {0: "int8_t", 1: "int16_t", 2: "int32_t", 3: "int64_t"}[type_id]
            element_size = {0: 1, 1: 2, 2: 4, 3: 8}[type_id]
            scale = constant_integer(arguments[2])
            if scale == element_size:
                return f"(({c_type} *)&{base})[{index}]"
            return f"*({c_type} *)((uint8_t *)&{base} + {index} * {arguments[2]})"
        if name == "load_indexed" and len(arguments) == 3:
            try:
                type_id = int(arguments[1], 0) & 3
            except ValueError:
                return None
            address = f"{arguments[0]} + {arguments[2]}"
            access = self._local_array_index(address, type_id)
            if access is not None:
                return f"{access[0].base}[{access[1]}]"
            typed_view = self._local_array_typed_view(address, type_id)
            if typed_view is not None:
                return f"{typed_view[0]}[{typed_view[1]}]"
            pointer_access = self._primitive_pointer_index(address, type_id)
            if pointer_access is not None:
                return f"{pointer_access[0]}[{pointer_access[1]}]"
            return self._typed_dereference(address, type_id)
        if name == "load_typed" and len(arguments) == 2:
            try:
                type_id = int(arguments[0], 0) & 3
            except ValueError:
                return None
            access = self._local_array_index(arguments[1], type_id)
            if access is not None:
                return f"{access[0].base}[{access[1]}]"
            typed_view = self._local_array_typed_view(arguments[1], type_id)
            if typed_view is not None:
                return f"{typed_view[0]}[{typed_view[1]}]"
            pointer_access = self._primitive_pointer_index(arguments[1], type_id)
            if pointer_access is not None:
                return f"{pointer_access[0]}[{pointer_access[1]}]"
            return self._typed_dereference(arguments[1], type_id)
        widths = {"i8": 0, "i16": 1, "i32": 2, "i64": 3}
        load = LOAD_WIDTH_RE.fullmatch(name)
        if load is not None and len(arguments) == 1:
            type_id = widths[load.group(1)]
            access = self._local_array_index(arguments[0], type_id)
            if access is not None:
                return f"{access[0].base}[{access[1]}]"
            typed_view = self._local_array_typed_view(arguments[0], type_id)
            if typed_view is not None:
                return f"{typed_view[0]}[{typed_view[1]}]"
            pointer_access = self._primitive_pointer_index(arguments[0], type_id)
            if pointer_access is not None:
                return f"{pointer_access[0]}[{pointer_access[1]}]"
            return self._typed_dereference(arguments[0], type_id)
        store = STORE_WIDTH_RE.fullmatch(name)
        if store is not None and len(arguments) == 2:
            access = self._local_array_index(arguments[0], widths[store.group(1)])
            if access is not None:
                return f"{access[0].base}[{access[1]}] = {arguments[1]}"
            typed_view = self._local_array_typed_view(arguments[0], widths[store.group(1)])
            if typed_view is not None:
                return f"{typed_view[0]}[{typed_view[1]}] = {arguments[1]}"
            pointer_access = self._primitive_pointer_index(arguments[0], widths[store.group(1)])
            if pointer_access is not None:
                return f"{pointer_access[0]}[{pointer_access[1]}] = {arguments[1]}"
            return f"{self._typed_dereference(arguments[0], widths[store.group(1)])} = {arguments[1]}"
        return None

    def _replace_local_array_calls(self, text: str) -> str:
        def rewrite(fragment: str) -> str:
            if CALL_IDENTIFIER_RE.search(fragment) is None:
                return fragment
            rendered: list[str] = []
            index = 0
            while index < len(fragment):
                if fragment[index] in {'"', "'"}:
                    quote = fragment[index]
                    end = index + 1
                    while end < len(fragment):
                        if fragment[end] == "\\":
                            end += 2
                            continue
                        if fragment[end] == quote:
                            end += 1
                            break
                        end += 1
                    rendered.append(fragment[index:end])
                    index = end
                    continue
                identifier = IDENTIFIER_RE.match(fragment, index)
                if identifier is None:
                    rendered.append(fragment[index])
                    index += 1
                    continue
                name = identifier.group(0)
                name_end = index + len(name)
                if name_end >= len(fragment) or fragment[name_end] != "(":
                    rendered.append(name)
                    index = name_end
                    continue
                depth = 1
                end = name_end + 1
                quote: str | None = None
                while end < len(fragment) and depth:
                    char = fragment[end]
                    if quote is not None:
                        if char == "\\":
                            end += 2
                            continue
                        if char == quote:
                            quote = None
                    elif char in {'"', "'"}:
                        quote = char
                    elif char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                    end += 1
                if depth:
                    rendered.append(fragment[index:])
                    break
                inner = rewrite(fragment[name_end + 1:end - 1])
                arguments = [
                    clarify_ambiguous_expression(argument)
                    for argument in split_arguments(inner)
                ]
                replacement = self._replace_local_array_call(name, arguments)
                if replacement is None:
                    arguments = [
                        self._normalize_local_array_pointer_argument(argument)
                        for argument in arguments
                    ]
                    replacement = self._replace_local_array_call(name, arguments)
                inner = ", ".join(arguments)
                rendered.append(replacement if replacement is not None else f"{name}({inner})")
                index = end
            return "".join(rendered)

        return rewrite(text)

    def _normalize_local_array_pointer_argument(self, text: str) -> str:
        token = strip_outer_parentheses(text)
        match = LOCAL_INDEXED_TARGET_RE.fullmatch(token)
        if match is None:
            return text
        array = self.local_array_by_base.get(match.group("base"))
        if array is None:
            return text
        tail = match.group("tail").strip()
        if not match.group("address") and not tail.startswith(("+", "-")):
            return text
        indexed = local_indexed_address(
            token,
            array.element_size,
            allow_bare=array.is_parameter,
        )
        if indexed is None:
            return text
        base, index = indexed
        return f"&{base}[{index}]"

    def _replace_local_array_accesses(self, text: str) -> str:
        text = self._replace_local_array_calls(text)

        assignment = VARIABLE_ASSIGN_RE.fullmatch(text.strip())
        if assignment is not None:
            value = self._normalize_local_array_pointer_argument(
                assignment.group("value")
            )
            if value != assignment.group("value"):
                text = f"{assignment.group('name')} = {value};"

        def replace_base(match: re.Match[str]) -> str:
            name = match.group(0)
            prefix = text[:match.start()].rstrip()
            suffix = text[match.end():].lstrip()
            array = self.local_array_by_base.get(name)
            if (
                array is not None
                and not array.is_parameter
                and not suffix.startswith("[")
                and not (prefix.endswith("[") and suffix.startswith("]"))
                and not prefix.endswith("sizeof(")
            ):
                return f"{name}[0]"
            return name

        return LOCAL_RE.sub(replace_base, text)

    @staticmethod
    def _normalize_local_address_arithmetic(text: str) -> str:
        """将 VM 栈帧偏移统一为字节偏移。"""
        replacements: list[tuple[int, int, str]] = []
        for match in LOCAL_ADDRESS_ARITHMETIC_RE.finditer(text):
            cursor = match.end()
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text) or text[cursor] not in "+-":
                continue
            prefix = text[max(0, match.start() - 32):match.start()]
            if CAST_PREFIX_RE.search(prefix):
                continue
            indexed = match.group("index") or ""
            replacements.append(
                (
                    match.start(),
                    match.end(),
                    f"(uint8_t *)&{match.group('base')}{indexed}",
                )
            )
        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
        return text

    def _replace_names(self, text: str) -> str:
        return VARIABLE_RE.sub(lambda match: self.variable_names.get(match.group(0), match.group(0)), text)

    def _normalize_pointer_assignment(self, text: str) -> str:
        assignment = re.fullmatch(
            r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>.*);",
            text.strip(),
        )
        if assignment is None:
            return text
        source_name = self.rendered_variable_sources.get(assignment.group("name"))
        target_type = self.variable_types.get(source_name or "")
        if target_type is None or not target_type.endswith("*"):
            return text

        value = assignment.group("value").strip()
        if value in {"0", "0x0", "NULL"} or (
            target_type in {"char *", "const char *"}
            and value.startswith('"')
            and value.endswith('"')
        ):
            return text

        value_source = self.rendered_variable_sources.get(strip_outer_parentheses(value))
        value_type = self.variable_types.get(value_source or "")
        if value_type is not None and value_type.endswith("*"):
            return text
        expression_type = _infer_text_c_type(
            value,
            lambda name: self.variable_types.get(
                self.rendered_variable_sources.get(name, name)
            ),
        )
        if (
            expression_type is not None
            and expression_type.endswith("*")
            and merge_c_types([target_type, expression_type]) is not None
        ):
            return text

        array_address = re.fullmatch(
            r"&(?P<name>[A-Za-z_]\w*)\[[^\]]+\]",
            value,
        )
        if array_address is not None:
            source = self.rendered_variable_sources.get(array_address.group("name"))
            array = self.local_array_by_base.get(source or "")
            if array is not None and target_type == f"{array.c_type} *":
                return text

        scalar_address = re.fullmatch(r"&(?P<name>[A-Za-z_]\w*)", value)
        if scalar_address is not None:
            source = self.rendered_variable_sources.get(scalar_address.group("name"))
            source_type = self.variable_types.get(source or "", "int32_t")
            if target_type == f"{source_type} *":
                return text

        pointee = target_type.removeprefix("const ").removesuffix(" *")
        element_size = {
            "char": 1,
            "int8_t": 1,
            "uint8_t": 1,
            "int16_t": 2,
            "uint16_t": 2,
            "int32_t": 4,
            "uint32_t": 4,
            "int64_t": 8,
            "uint64_t": 8,
        }.get(pointee)
        if element_size is not None:
            absolute = constant_integer(value)
            if absolute is not None and absolute >= 0:
                return (
                    f"{assignment.group('name')} = "
                    f"global_i{element_size * 8}_array_{absolute:08x};"
                )
            pointer_index = integer_pointer_index(value, element_size)
            if pointer_index is not None:
                base, index = pointer_index
                source = self.rendered_variable_sources.get(base)
                if self.variable_types.get(source or "") == target_type:
                    rendered_value = base if constant_integer(index) == 0 else f"{base} + {index}"
                    return f"{assignment.group('name')} = {rendered_value};"
            value_identifiers = re.findall(r"\b[A-Za-z_]\w*\b", value)
            has_pointer_base = any(
                self.variable_types.get(
                    self.rendered_variable_sources.get(name, ""),
                    "",
                ).endswith("*")
                for name in value_identifiers
            )
            absolute_indexed = (
                None
                if has_pointer_base
                else absolute_pointer_index(value, element_size)
            )
            if absolute_indexed is not None:
                base, index = absolute_indexed
                return (
                    f"{assignment.group('name')} = "
                    f"&global_i{element_size * 8}_array_{base:08x}[{index}];"
                )

        pointer_cast = re.match(
            r"^\((?:const\s+)?[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s*\*\)",
            value,
        )
        if pointer_cast is not None or value.startswith("&"):
            converted = f"({target_type})({value})"
        else:
            converted = f"({target_type})(uintptr_t)({value})"
        return f"{assignment.group('name')} = {converted};"

    def _normalize_vm_word_assignment(self, text: str) -> str:
        assignment = re.fullmatch(
            r"(?P<base>[A-Za-z_]\w*)\[(?P<index>[^\]]+)\]\s*=\s*(?P<value>.*);",
            text.strip(),
        )
        if assignment is None:
            return text
        source = self.rendered_variable_sources.get(assignment.group("base"))
        array = self.local_array_by_base.get(source or "")
        if array is None or array.c_type != "intptr_t":
            return text
        value = assignment.group("value").strip()
        value_type = _infer_text_c_type(
            value,
            lambda name: self.variable_types.get(
                self.rendered_variable_sources.get(name, name)
            ),
            self.local_array_by_base,
            self.stack_parameter_type_hints,
        )
        if value_type is None and value.startswith("sys1_load_program("):
            value_type = "BPDProgramEntry"
        if value_type is None or not (
            value_type.endswith("*")
            or value_type in {"BPDEntryPoint", "BPDProgramEntry"}
        ):
            return text
        return (
            f"{assignment.group('base')}[{assignment.group('index')}] = "
            f"(intptr_t)({value});"
        )

    def _normalize_direct_pointer_call_arguments(self, text: str) -> str:
        if not self.call_parameter_types:
            return text

        def rewrite(fragment: str) -> str:
            rendered: list[str] = []
            index = 0
            while index < len(fragment):
                if fragment[index] in {'"', "'"}:
                    quote = fragment[index]
                    end = index + 1
                    while end < len(fragment):
                        if fragment[end] == "\\":
                            end += 2
                            continue
                        if fragment[end] == quote:
                            end += 1
                            break
                        end += 1
                    rendered.append(fragment[index:end])
                    index = end
                    continue
                identifier = re.match(r"[A-Za-z_][A-Za-z0-9_]*", fragment[index:])
                if identifier is None:
                    rendered.append(fragment[index])
                    index += 1
                    continue
                name = identifier.group(0)
                name_end = index + len(name)
                if name_end >= len(fragment) or fragment[name_end] != "(":
                    rendered.append(name)
                    index = name_end
                    continue
                depth = 1
                end = name_end + 1
                quote: str | None = None
                while end < len(fragment) and depth:
                    char = fragment[end]
                    if quote is not None:
                        if char == "\\":
                            end += 2
                            continue
                        if char == quote:
                            quote = None
                    elif char in {'"', "'"}:
                        quote = char
                    elif char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                    end += 1
                if depth:
                    rendered.append(fragment[index:])
                    break
                arguments = [
                    rewrite(argument)
                    for argument in split_arguments(fragment[name_end + 1:end - 1])
                ]
                parameter_types = self.call_parameter_types.get(name, ())
                for position, (argument, parameter_type) in enumerate(
                    zip(arguments, parameter_types)
                ):
                    token = strip_outer_parentheses(argument)
                    absolute = constant_integer(token)
                    pointee = parameter_type.removeprefix("const ").removesuffix(" *")
                    width = {
                        "char": 8,
                        "int8_t": 8,
                        "uint8_t": 8,
                        "int16_t": 16,
                        "uint16_t": 16,
                        "int32_t": 32,
                        "uint32_t": 32,
                        "int64_t": 64,
                        "uint64_t": 64,
                    }.get(pointee)
                    if (
                        absolute is not None
                        and absolute >= 0
                        and width is not None
                        and parameter_type.endswith("*")
                    ):
                        arguments[position] = (
                            f"global_i{width}_array_{absolute:08x}"
                        )
                        continue
                    argument_type = _infer_text_c_type(
                        token,
                        lambda variable: self.variable_types.get(
                            self.rendered_variable_sources.get(variable, variable)
                        ),
                        self.local_array_by_base,
                        self.stack_parameter_type_hints,
                    )
                    if parameter_type.endswith("*") and argument_type == "intptr_t":
                        arguments[position] = (
                            f"({parameter_type})(uintptr_t)({argument})"
                        )
                        continue
                    if parameter_type == "intptr_t" and argument_type is not None and (
                        argument_type.endswith("*")
                        or argument_type in {"BPDEntryPoint", "BPDProgramEntry"}
                    ):
                        arguments[position] = f"(intptr_t)({argument})"
                        continue
                    byte_address = re.match(
                        r"^\((?:const\s+)?uint8_t\s*\*\)\s*&",
                        token,
                    )
                    if (
                        byte_address is not None
                        and parameter_type.endswith("*")
                        and parameter_type not in {"uint8_t *", "const uint8_t *"}
                    ):
                        arguments[position] = f"({parameter_type})({argument})"
                rendered.append(f"{name}({', '.join(arguments)})")
                index = end
            return "".join(rendered)

        return rewrite(text)

    @staticmethod
    def _clarify_statement_expression(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("return ") and stripped.endswith(";"):
            value = stripped[len("return "):-1]
            return f"return {clarify_ambiguous_expression(value)};"

        depth = 0
        quote: str | None = None
        index = 0
        while index < len(stripped) - 2:
            char = stripped[index]
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {'"', "'"}:
                quote = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(0, depth - 1)
            elif depth == 0 and stripped.startswith(" = ", index):
                if not stripped.endswith(";"):
                    return text
                value = stripped[index + 3:-1]
                return (
                    stripped[:index]
                    + " = "
                    + clarify_ambiguous_expression(value)
                    + ";"
                )
            index += 1
        return clarify_ambiguous_expression(stripped)

    @staticmethod
    def _cast_indirect_dereference_calls(text: str) -> str:
        """Cast 32-bit VM table entries before using them as C callees."""
        marker = "(*(int32_t *)"
        cursor = 0
        while True:
            start = text.find(marker, cursor)
            if start < 0:
                return text
            depth = 0
            quote: str | None = None
            closing: int | None = None
            index = start
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
                        closing = index
                        break
                index += 1
            if closing is None:
                return text
            next_index = closing + 1
            while next_index < len(text) and text[next_index].isspace():
                next_index += 1
            if next_index >= len(text) or text[next_index] != "(":
                cursor = closing + 1
                continue
            callee = text[start:closing + 1]
            replacement = f"((BPDEntryPoint)(uintptr_t){callee})"
            text = text[:start] + replacement + text[closing + 1:]
            cursor = start + len(replacement)

    def _cast_vm_word_calls(self, text: str) -> str:
        """合法转换 32 位 BP 槽中的调用目标。"""
        def cast_indexed(match: re.Match[str]) -> str:
            # 调用位置本身就是最强的函数指针约束。
            base = match.group("base")
            array = self.local_array_by_base.get(base)
            if array is not None and array.c_type == "BPDEntryPoint":
                return match.group(0)
            return f"((BPDEntryPoint)(uintptr_t)({match.group('callee')}))"

        text = INDEXED_VM_WORD_CALL_RE.sub(cast_indexed, text)
        text = BARE_INDEXED_VM_WORD_CALL_RE.sub(cast_indexed, text)
        text = GLOBAL_VM_WORD_CALL_RE.sub(
            r"((BPDEntryPoint)(uintptr_t)\g<callee>)",
            text,
        )
        return text

    def _replace_function_references(self, text: str) -> str:
        def replace_call_with_args(match: re.Match[str]) -> str:
            function = self.function_refs.get(match.group("label"))
            return match.group(0) if function is None else f"{function}("

        def replace_call(match: re.Match[str]) -> str:
            function = self.function_refs.get(match.group("label"))
            return match.group(0) if function is None else f"{function}()"

        def replace_reference(match: re.Match[str]) -> str:
            function = self.function_refs.get(match.group("label"))
            return match.group(0) if function is None else f"&{function}"

        text = DIRECT_LABEL_CALL_WITH_ARGS_RE.sub(replace_call_with_args, text)
        text = DIRECT_LABEL_CALL_RE.sub(replace_call, text)
        return LABEL_REF_RE.sub(replace_reference, text)

    def _replace_typed_pointer_access(self, text: str) -> str:
        store = TYPED_POINTER_STORE_RE.fullmatch(text.strip())
        if store is not None:
            pointer_type = self.variable_types.get(store.group("name"))
            type_id = {"i8": 0, "i16": 1, "i32": 2, "i64": 3}[store.group("width")]
            field = structure_pointer_field(pointer_type or "", 0, type_id)
            if field is not None:
                return f"{store.group('name')}->{field} = {store.group('value')};"
        load = TYPED_POINTER_LOAD_RE.fullmatch(text.strip())
        if load is not None:
            pointer_type = self.variable_types.get(load.group("name"))
            type_id = {"i8": 0, "i16": 1, "i32": 2, "i64": 3}[load.group("width")]
            field = structure_pointer_field(pointer_type or "", 0, type_id)
            if field is not None:
                return f"{load.group('name')}->{field}"
        return text

    def _replace_references(self, text: str) -> str:
        original = text
        cached = self._reference_cache.get(original)
        if cached is not None:
            return cached
        text = self._replace_typed_pointer_access(text)
        text = self._replace_local_array_accesses(text)
        text = self._normalize_local_address_arithmetic(text)
        text = self._cast_vm_word_calls(text)
        text = self._replace_names(text)
        text = self._cast_indirect_dereference_calls(text)
        text = PAREN_GLOBAL_I32_CALL_RE.sub(
            r"((BPDEntryPoint)(uintptr_t)\g<callee>)",
            text,
        )
        text = VM_ARGUMENT_RE.sub(
            lambda match: self.stack_parameter_names[int(match.group("index"))],
            text,
        )
        text = self._replace_function_references(text)
        text = self._normalize_direct_pointer_call_arguments(text)
        text = text.replace("vm_push(vm, ", "stack_push(")
        text = text.replace("vm_pop(vm)", "stack_pop()")
        text = self._normalize_pointer_assignment(text)
        text = self._normalize_vm_word_assignment(text)
        text = self._clarify_statement_expression(text)
        self._reference_cache[original] = text
        return text

    def _render_condition(self, text: str) -> str:
        return strip_outer_parentheses(
            clarify_ambiguous_expression(
                simplify_condition(self._replace_references(text))
            )
        )

    def _render_return(self, text: str) -> str:
        """Render a VM return consistently with the solved C return type."""
        if text.strip() == "return;" and self.return_type != "void":
            if self.implicit_return_binding is not None:
                kind, binding = self.implicit_return_binding
                if kind == "stack":
                    value = self.stack_parameter_names[int(binding)]
                    c_type = merge_c_types(
                        self.stack_parameter_type_hints.get(int(binding), set())
                    ) or "int32_t"
                else:
                    name = str(binding)
                    value = self.variable_names[name]
                    c_type = self.variable_types.get(name, "int32_t")
                if c_type.endswith("*") or c_type in {
                    "BPDEntryPoint",
                    "BPDProgramEntry",
                }:
                    value = f"(int32_t)(intptr_t){value}"
                return f"return {value};"
            return "return values(0);" if self.return_type == "BPDValues" else "return 0;"
        return self._replace_references(text)

    def _infer_return_type(self) -> str:
        return_texts = [
            statement.text.strip()
            for block_index, block in enumerate(self.blocks)
            if block_index not in self.omitted_blocks
            for statement in block.statements
            if statement.kind == "return"
        ]
        if any(text.startswith("return values(") for text in return_texts):
            return "BPDValues"
        value_texts = [
            text[len("return "):-1].strip()
            for text in return_texts
            if text.startswith("return ") and text.endswith(";")
        ]
        if value_texts:
            value_types = [
                _infer_text_c_type(
                    value,
                    lambda name: self.variable_types.get(name),
                    self.local_array_by_base,
                    self.stack_parameter_type_hints,
                )
                for value in value_texts
                if constant_integer(strip_outer_parentheses(value)) != 0
            ]
            inferred = merge_c_types(
                [value for value in value_types if value is not None]
            )
            if inferred is not None and (
                inferred.endswith("*")
                or inferred in {"BPDEntryPoint", "BPDProgramEntry", "intptr_t"}
            ):
                return inferred
            return "int32_t"
        return "void"

    def _remaining_targets(self) -> set[str]:
        targets: set[str] = set()
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                if id(statement) in self.builder.suppressed:
                    continue
                if id(statement) in self.structured_controls:
                    continue
                if id(statement) in self.inline_terminal_branches:
                    continue
                if statement.kind in {"branch", "jump"} and statement.target:
                    if statement.target in self.terminal_returns:
                        continue
                    targets.add(statement.target)
        targets.update(self._active_dispatch_targets(self.regions))
        return targets

    def _active_dispatch_targets(self, nodes: list[Region]) -> set[str]:
        targets: set[str] = set()
        for node in nodes:
            if isinstance(node, DispatchRegion):
                inline = self.inline_dispatch_cases.get(id(node), {})
                partition = self._shared_dispatch_partition(node)
                bypass_targets = {
                    target for _, _, target in partition[0]
                } if partition is not None else set()
                targets.update(
                    target
                    for _, _, target in node.tests
                    if target not in inline
                    and target not in bypass_targets
                    and target not in self.terminal_returns
                )
            elif isinstance(node, IfRegion):
                targets.update(self._active_dispatch_targets(node.then_nodes))
                targets.update(self._active_dispatch_targets(node.else_nodes))
            elif isinstance(node, WhileRegion):
                targets.update(self._active_dispatch_targets(node.body_nodes))
            elif isinstance(node, DoWhileRegion):
                targets.update(self._active_dispatch_targets(node.body_nodes))
        return targets

    def _shared_dispatch_partition(
        self,
        node: DispatchRegion,
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]] | None:
        inline = self.inline_dispatch_cases.get(id(node), {})
        if id(node) not in self.inline_dispatch_defaults or not inline:
            return None
        first_inline = next(
            (
                index
                for index, (_, _, target) in enumerate(node.tests)
                if target in inline
            ),
            None,
        )
        if first_inline is None or first_inline == 0:
            return None
        bypasses = node.tests[:first_inline]
        cases = node.tests[first_inline:]
        if any(kind != "case" or target not in inline for kind, _, target in cases):
            return None
        if any(kind not in {"case", "guard"} for kind, _, _ in bypasses):
            return None
        return bypasses, cases

    def _emit_dispatch_cases(
        self,
        selector: str,
        tests: list[tuple[str, str, str]],
        indent: int,
        inline_cases: dict[str, list[int]] | None = None,
        default_chain: list[int] | None = None,
    ) -> list[str]:
        if not tests:
            return []
        prefix = "    " * indent
        lines: list[str] = []
        if len(tests) < 4:
            grouped: dict[str, list[str]] = {}
            for _, value, target in tests:
                grouped.setdefault(target, []).append(value)
            for target, values in grouped.items():
                condition = " || ".join(f"{selector} == {value}" for value in values)
                chain = (inline_cases or {}).get(target)
                if chain is None:
                    lines.extend(self._emit_control_transfer(target, indent, condition))
                    continue
                lines.append(f"{prefix}if ({condition}) {{")
                for index in chain:
                    lines.extend(self._emit_block_body(index, indent + 1, include_label=False))
                lines.append(f"{prefix}}}")
            return lines

        lines.append(f"{prefix}switch ({selector}) {{")
        grouped = {}
        for _, value, target in tests:
            grouped.setdefault(target, []).append(value)
        for target, values in grouped.items():
            for value in values:
                lines.append(f"{prefix}    case {value}:")
            chain = (inline_cases or {}).get(target)
            if chain is None:
                lines.extend(self._emit_control_transfer(target, indent + 2))
                continue
            lines.append(f"{prefix}    {{")
            for index in chain:
                lines.extend(self._emit_block_body(index, indent + 2, include_label=False))
            if default_chain is not None:
                lines.append(f"{prefix}        break;")
            lines.append(f"{prefix}    }}")
        lines.append(f"{prefix}    default:")
        if default_chain is None:
            lines.append(f"{prefix}        break;")
        else:
            lines.append(f"{prefix}    {{")
            for index in default_chain:
                lines.extend(self._emit_block_body(index, indent + 2, include_label=False))
            lines.append(f"{prefix}    }}")
        lines.append(f"{prefix}}}")
        return lines

    def _emit_control_transfer(
        self,
        target: str,
        indent: int,
        condition: str | None = None,
    ) -> list[str]:
        prefix = "    " * indent
        terminal_return = self.terminal_returns.get(target)
        action = (
            self._render_return(terminal_return.text)
            if terminal_return is not None
            else f"goto {target};"
        )
        if condition is not None:
            return [f"{prefix}if ({condition}) {action}"]
        return [f"{prefix}{action}"]

    def _block_statements(self, index: int) -> list[Statement]:
        return [
            statement
            for statement in self.blocks[index].statements
            if id(statement) not in self.builder.suppressed
            and id(statement) not in self.parameter_statements
            and (
                id(statement) not in self.function_table_statements
                or id(statement)
                == self.function_table_statements[id(statement)].first_statement_id
            )
            and statement.kind != "state"
            and id(statement) not in self.folded_array_statements
        ]

    @staticmethod
    def _is_zero_value(text: str) -> bool:
        try:
            return int(strip_outer_parentheses(text), 0) == 0
        except ValueError:
            return False

    def _render_typed_value(self, c_type: str, text: str) -> str:
        rendered = self._replace_references(text)
        if c_type != "intptr_t":
            return rendered
        value_type = _infer_text_c_type(
            rendered,
            lambda name: self.variable_types.get(
                self.rendered_variable_sources.get(name, name)
            ),
        )
        if value_type is not None and value_type.endswith("*"):
            return f"(intptr_t)({rendered})"
        return rendered

    def _render_residual_copystack(
        self,
        statement: Statement,
        prefix: str,
        source: str,
    ) -> list[str] | None:
        """Render the VM bulk stack store as ordinary typed C memory writes."""
        text = statement.text.strip()
        if not text.startswith("copystack(") or not text.endswith(");"):
            return None
        arguments = split_arguments(text[len("copystack("):-2])
        if len(arguments) < 4:
            return None
        try:
            type_id = int(arguments[-2], 0) & 3
            count = int(arguments[-1], 0)
        except ValueError:
            return None
        if count < 1 or len(arguments[1:-2]) != count:
            return None

        element_size = (1, 2, 4, 8)[type_id]
        raw_target = strip_outer_parentheses(arguments[0])
        target_match = LOCAL_INDEXED_TARGET_RE.fullmatch(raw_target)
        target_array = (
            self.local_array_by_base.get(target_match.group("base"))
            if target_match is not None
            else None
        )
        normalized_target = self._normalize_local_array_pointer_argument(arguments[0])
        target = self._replace_references(normalized_target)
        values = [self._replace_references(value) for value in arguments[1:-2]]
        nonzero_values = [value for value in values if not self._is_zero_value(value)]
        function_values = bool(nonzero_values) and all(
            re.fullmatch(r"(?:\(BPDEntryPoint\))?&bpd_[A-Za-z0-9_]+", value)
            is not None
            for value in nonzero_values
        )
        string_values = bool(nonzero_values) and all(
            value.startswith('"') and value.endswith('"')
            for value in nonzero_values
        )

        parsed_local = self._parse_local_array_copy(statement)
        local_array = target_array or (
            self.local_array_by_base.get(parsed_local[0])
            if parsed_local is not None
            else None
        )
        if function_values:
            c_type = "BPDEntryPoint"
        elif string_values:
            c_type = "const char *"
        elif local_array is not None and local_array.element_size == element_size:
            c_type = local_array.c_type
        else:
            c_type = ("int8_t", "int16_t", "int32_t", "int64_t")[type_id]

        values = [
            self._render_typed_value(c_type, value)
            for value in arguments[1:-2]
        ]

        if c_type.endswith("*") or c_type == "BPDEntryPoint":
            values = ["NULL" if self._is_zero_value(value) else value for value in values]
        elif c_type in {"int8_t", "int16_t", "int32_t", "int64_t"}:
            values = [
                f"({c_type})(uintptr_t)({value})"
                if "&" in value and not self._is_zero_value(value)
                else value
                for value in values
            ]

        constant_target = constant_integer(strip_outer_parentheses(target))
        if constant_target is not None and constant_target >= 0:
            if c_type == "BPDEntryPoint":
                target = f"global_fn_array_{constant_target:08x}"
            else:
                target = f"global_i{element_size * 8}_array_{constant_target:08x}"
            direct_lvalue = f"{target}[0]"
        else:
            direct_lvalue = None
            if (
                local_array is not None
                and local_array.element_size == element_size
                and target.startswith("&")
                and re.fullmatch(r"[A-Za-z_]\w*\[[^\n]+\]", target[1:])
            ):
                direct_lvalue = target[1:]

        if direct_lvalue is not None and (
            c_type.endswith("*") or c_type == "BPDEntryPoint"
        ):
            indexed_lvalue = re.fullmatch(
                r"(?P<base>[A-Za-z_]\w*)\[(?P<index>.+)\]",
                direct_lvalue,
            )
            if indexed_lvalue is not None:
                base = indexed_lvalue.group("base")
                start = indexed_lvalue.group("index").strip()
                start_value = constant_integer(start)
                lines: list[str] = []
                for offset, value in enumerate(values):
                    if start_value is not None:
                        index = str(start_value + offset)
                    elif offset:
                        index = f"{start} + {offset}"
                    else:
                        index = start
                    line_source = source if offset == len(values) - 1 else ""
                    lines.append(f"{prefix}{base}[{index}] = {value};{line_source}")
                return lines

        if count == 1 and direct_lvalue is not None:
            return [f"{prefix}{direct_lvalue} = {values[0]};{source}"]

        initializer = f"({c_type}[]){{{', '.join(values)}}}"
        return [
            f"{prefix}memcpy({target}, {initializer}, "
            f"sizeof({c_type}) * {count});{source}"
        ]

    def _emit_statement(self, statement: Statement, indent: int) -> list[str]:
        prefix = "    " * indent
        source = format_source_lines(statement.source_lines) if self.include_source_comments else ""
        if statement.kind == "conditional_assign":
            text = statement.text.strip()
            if text.startswith("select_assign(") and text.endswith(");"):
                arguments = split_arguments(text[len("select_assign("):-2])
                if len(arguments) >= 6 and len(arguments) % 2 == 0:
                    target = self._replace_references(arguments[0])
                    lines: list[str] = []
                    for position in range(1, len(arguments) - 1, 2):
                        condition = self._render_condition(arguments[position])
                        value = self._replace_references(arguments[position + 1])
                        if position == 1:
                            lines.append(f"{prefix}if ({condition}) {{")
                        else:
                            lines[-1] += f" else if ({condition}) {{"
                        lines.append(f"{prefix}    {target} = {value};")
                        lines.append(f"{prefix}}}")
                    default_value = self._replace_references(arguments[-1])
                    if default_value != target:
                        lines[-1] += " else {"
                        lines.append(f"{prefix}    {target} = {default_value};")
                        lines.append(f"{prefix}}}")
                    lines[-1] += source
                    return lines
        if statement.kind in {"stack", "stack_preserved"} and not self.has_unresolved_stack_reads:
            pushed = VM_PUSH_RE.fullmatch(statement.text)
            if pushed is not None:
                value = self._replace_references(pushed.group("value"))
                return [f"{prefix}(void)({value});{source}"]
        array_clear = self.local_array_clears.get(id(statement))
        if array_clear is not None:
            base, byte_offset, byte_count = array_clear
            array = self.local_array_by_base[base]
            if byte_offset % array.element_size == 0 and byte_count % array.element_size == 0:
                name = self.variable_names.get(base, base)
                start = byte_offset // array.element_size
                count = byte_count // array.element_size
                end = start + count
                if count <= 4:
                    return [
                        f"{prefix}{name}[{index}] = 0;{source if index == end - 1 else ''}"
                        for index in range(start, end)
                    ]
                clear_variable = self.array_clear_variables[id(statement)]
                return [
                    f"{prefix}for (int32_t {clear_variable} = {start}; "
                    f"{clear_variable} < {end}; ++{clear_variable}) {{",
                    f"{prefix}    {name}[{clear_variable}] = 0;",
                    f"{prefix}}}{source}",
                ]
        array_copy = self.local_array_copies.get(id(statement))
        if array_copy is not None:
            array = self.local_array_by_base[array_copy.base]
            name = self.variable_names.get(array.base, array.base)
            values = [
                self._render_typed_value(array.c_type, value)
                for value in array_copy.values
            ]
            restore_values = self.array_copy_restores.get(id(statement))
            if restore_values is not None:
                return [
                    f"{prefix}{name}[{index}] = "
                    f"{self._render_typed_value(array.c_type, value)};"
                    f"{source if position == len(restore_values) - 1 else ''}"
                    for position, (index, value) in enumerate(restore_values)
                ]
            fill_variable = self.array_fill_variables.get(id(statement))
            if fill_variable is not None:
                start = array_copy.start_index
                end = start + len(values)
                return [
                    f"{prefix}for (int32_t {fill_variable} = {start}; "
                    f"{fill_variable} < {end}; ++{fill_variable}) {{",
                    f"{prefix}    {name}[{fill_variable}] = {values[0]};",
                    f"{prefix}}}{source}",
                ]
            if len(values) == 1:
                return [f"{prefix}{name}[{array_copy.start_index}] = {values[0]};{source}"]
            inline_limit = 4 if array.c_type == "BPDEntryPoint" else 8
            if len(values) <= inline_limit:
                return [
                    f"{prefix}{name}[{array_copy.start_index + offset}] = {value};"
                    f"{source if offset == len(values) - 1 else ''}"
                    for offset, value in enumerate(values)
                ]
            copy_variable = self.array_copy_variables.get(id(statement))
            if copy_variable is not None:
                initializer = f"({array.c_type}[]){{{', '.join(values)}}}"
                index = (
                    copy_variable
                    if array_copy.start_index == 0
                    else f"{array_copy.start_index} + {copy_variable}"
                )
                return [
                    f"{prefix}for (int32_t {copy_variable} = 0; "
                    f"{copy_variable} < {len(values)}; ++{copy_variable}) {{",
                    f"{prefix}    {name}[{index}] = {initializer}[{copy_variable}];",
                    f"{prefix}}}{source}",
                ]
            dynamic_copy = self.dynamic_array_copy_variables.get(id(statement))
            if dynamic_copy is not None:
                source_name, index_name = dynamic_copy
                separator = "" if array.c_type.endswith("*") else " "
                initializer = "{" + ", ".join(values) + "}"
                target_index = (
                    index_name
                    if array_copy.start_index == 0
                    else f"{array_copy.start_index} + {index_name}"
                )
                return [
                    f"{prefix}{array.c_type}{separator}{source_name}[{len(values)}] = {initializer};",
                    f"{prefix}for (int32_t {index_name} = 0; {index_name} < {len(values)}; "
                    f"++{index_name}) {{",
                    f"{prefix}    {name}[{target_index}] = {source_name}[{index_name}];",
                    f"{prefix}}}{source}",
                ]
            target = name if array_copy.start_index == 0 else f"&{name}[{array_copy.start_index}]"
            initializer = f"({array.c_type}[]){{{', '.join(values)}}}"
            return [
                f"{prefix}memcpy({target}, {initializer}, "
                f"sizeof({name}[0]) * {len(values)});{source}"
            ]
        scalar_copy = self._parse_local_array_copy(statement)
        if scalar_copy is not None and id(statement) not in self.function_table_statements:
            base, _, start_index, values, bare_target = scalar_copy
            if start_index == 0 and len(values) == 1 and not bare_target:
                name = self.variable_names.get(base, base)
                value = self._replace_references(values[0])
                return [f"{prefix}{name} = {value};{source}"]
        memory_repeat = parse_memcpy_repeat(statement.text)
        repeat_variable = self.memory_repeat_variables.get(id(statement))
        if memory_repeat is not None and repeat_variable is not None:
            raw_target, raw_element_size, raw_count, raw_source = memory_repeat
            target = self._replace_references(
                self._vm_memory_pointer(raw_target)
            )
            element_size = self._replace_references(raw_element_size)
            count = self._replace_references(raw_count)
            repeat_source = self._replace_references(
                self._vm_memory_pointer(raw_source, is_const=True)
            )
            return [
                f"{prefix}for (int32_t {repeat_variable} = 0; "
                f"{repeat_variable} < {count}; ++{repeat_variable}) {{",
                f"{prefix}    memcpy({target} + "
                f"{repeat_variable} * ({element_size}), {repeat_source}, "
                f"{element_size});",
                f"{prefix}}}{source}",
            ]
        function_table = self.function_table_statements.get(id(statement))
        if function_table is not None:
            table_source = (
                format_source_lines(function_table.source_lines)
                if self.include_source_comments
                else ""
            )
            lines = [f"{prefix}static const BPDEntryPoint {function_table.name}[] = {{"]
            for label in function_table.entries:
                if label is None:
                    lines.append(f"{prefix}    NULL,")
                    continue
                function_name = self.function_refs.get(label, sanitize_identifier(f"bpd_{label}"))
                lines.append(f"{prefix}    (BPDEntryPoint){function_name},")
            lines.append(f"{prefix}}};{table_source}")
            return lines
        dispatch_table = self.function_table_dispatches.get(id(statement))
        if dispatch_table is not None:
            index = self._replace_names(dispatch_table.index_local)
            arguments = self._replace_references(dispatch_table.call_arguments)
            return [f"{prefix}{dispatch_table.name}[{index}]({arguments});{source}"]
        keyword = self.structured_controls.get(id(statement))
        if keyword is not None:
            if statement.kind == "branch" and statement.condition:
                condition = self._render_condition(statement.condition)
                return [f"{prefix}if ({condition}) {keyword};{source}"]
            return [f"{prefix}{keyword};{source}"]
        terminal_return = self.terminal_returns.get(statement.target or "")
        if terminal_return is not None and statement.kind in {"branch", "jump"}:
            return_text = self._render_return(terminal_return.text)
            if statement.kind == "branch" and statement.condition:
                condition = self._render_condition(statement.condition)
                return [f"{prefix}if ({condition}) {return_text}{source}"]
            return [f"{prefix}{return_text}{source}"]
        terminal_chain = self.inline_terminal_branches.get(id(statement))
        if terminal_chain is not None:
            if statement.kind == "branch" and statement.condition:
                condition = self._render_condition(statement.condition)
                lines = [f"{prefix}if ({condition}) {{{source}"]
            else:
                lines = [f"{prefix}{{{source}"]
            for index in terminal_chain:
                lines.extend(self._emit_block_body(index, indent + 1, include_label=False))
            lines.append(f"{prefix}}}")
            return lines
        if statement.kind == "return":
            return [f"{prefix}{self._render_return(statement.text)}{source}"]
        if statement.kind == "branch" and statement.condition and statement.target:
            condition = self._render_condition(statement.condition)
            return [f"{prefix}if ({condition}) goto {statement.target};{source}"]
        stack_copy = self._render_residual_copystack(statement, prefix, source)
        if stack_copy is not None:
            return stack_copy
        text = self._replace_references(statement.text)
        declaration_name = self.inline_declarations.get(id(statement))
        if (
            declaration_name is None
            and RENDERED_SCALAR_SELF_ASSIGNMENT_RE.fullmatch(text) is not None
        ):
            return [f"{prefix}{source.strip()}"] if source else []
        if declaration_name is not None:
            rendered_name = self.variable_names[declaration_name]
            c_type = self.variable_types.get(declaration_name, "int32_t")
            separator = "" if c_type.endswith((" ", "*")) else " "
            text = re.sub(
                rf"^{re.escape(rendered_name)}\s*=",
                f"{c_type}{separator}{rendered_name} =",
                text,
                count=1,
            )
        return [f"{prefix}{text}{source}"]

    def _emit_block_body(self, index: int, indent: int, include_label: bool = True) -> list[str]:
        block = self.blocks[index]
        lines: list[str] = []
        if include_label:
            prefix = "    " * indent
            if block.label in self.remaining_targets:
                lines.append(f"{prefix}{block.label}:")
        for statement in self._block_statements(index):
            run = self.assignment_run_by_statement.get(id(statement))
            if run is not None:
                if id(statement) != run.statement_ids[0]:
                    continue
                prefix = "    " * indent
                source = (
                    format_source_lines(run.source_lines)
                    if self.include_source_comments
                    else ""
                )
                lines.append(
                    f"{prefix}for (int32_t {run.loop_variable} = {run.start_index}; "
                    f"{run.loop_variable} < {run.end_index}; ++{run.loop_variable}) {{"
                )
                lines.append(
                    f"{prefix}    {run.target}[{run.loop_variable}] = {run.value};"
                )
                lines.append(f"{prefix}}}{source}")
                continue
            lines.extend(self._emit_statement(statement, indent))
        return lines

    def _emit_if_region(self, node: IfRegion, indent: int) -> list[str]:
        chain = [node]
        while (
            not chain[-1].else_nodes
            and len(chain[-1].then_nodes) == 1
            and isinstance(chain[-1].then_nodes[0], IfRegion)
            # Do not flatten a nested guard that has its own else arm.  The
            # conjunction would move that else arm to the outer condition
            # and change ``if (bClear) { if (!retained) ... else ... }`` into
            # ``if (bClear && !retained) ... else ...``.
            and not chain[-1].then_nodes[0].else_nodes
            and not self._block_statements(chain[-1].then_nodes[0].header)
        ):
            chain.append(chain[-1].then_nodes[0])

        lines: list[str] = []
        for item in chain:
            lines.extend(self._emit_block_body(item.header, indent))
        terminal = chain[-1]
        conditions = [self._render_condition(item.condition) for item in chain]
        rendered_conditions = [
            f"({condition})" if split_top_level_boolean(condition, " || ") else condition
            for condition in conditions
        ]
        prefix = "    " * indent
        lines.append(f"{prefix}if ({' && '.join(rendered_conditions)}) {{")
        lines.extend(self._emit_nodes(terminal.then_nodes, indent + 1))
        if terminal.else_nodes:
            nested = (
                terminal.else_nodes[0]
                if len(terminal.else_nodes) == 1
                and isinstance(terminal.else_nodes[0], IfRegion)
                and not self._block_statements(terminal.else_nodes[0].header)
                and self.blocks[terminal.else_nodes[0].header].label
                not in self.remaining_targets
                else None
            )
            if nested is not None:
                nested_lines = self._emit_if_region(nested, indent)
                lines.append(f"{prefix}}} else {nested_lines[0][len(prefix):]}")
                lines.extend(nested_lines[1:])
                return lines
            lines.append(f"{prefix}}} else {{")
            lines.extend(self._emit_nodes(terminal.else_nodes, indent + 1))
        lines.append(f"{prefix}}}")
        return lines

    def _emit_nodes(self, nodes: list[Region], indent: int) -> list[str]:
        lines: list[str] = []
        for node in nodes:
            node_indices = self._region_indices([node])
            if node_indices and node_indices.isdisjoint(self.entry_reachable_blocks):
                continue
            if isinstance(node, BlockRegion):
                if node.index in self.inline_dispatch_blocks | self.inline_terminal_blocks:
                    continue
                lines.extend(self._emit_block_body(node.index, indent))
                continue
            if isinstance(node, DispatchRegion):
                for header in node.headers:
                    lines.extend(self._emit_block_body(header, indent))
                pending_cases: list[tuple[str, str, str]] = []
                prefix = "    " * indent
                selector = self._replace_references(node.selector)
                inline_cases = self.inline_dispatch_cases.get(id(node), {})
                default_chain = self.inline_dispatch_defaults.get(id(node))
                shared_partition = self._shared_dispatch_partition(node)
                if shared_partition is not None:
                    bypasses, cases = shared_partition
                    execute_conditions: list[str] = []
                    for kind, value, _ in bypasses:
                        condition = (
                            f"{node.selector} == {value}"
                            if kind == "case"
                            else value
                        )
                        rendered = self._render_condition(negate_condition(condition))
                        if split_top_level_boolean(rendered, " || "):
                            rendered = f"({rendered})"
                        execute_conditions.append(rendered)
                    lines.append(f"{prefix}if ({' && '.join(execute_conditions)}) {{")
                    lines.extend(
                        self._emit_dispatch_cases(
                            selector,
                            cases,
                            indent + 1,
                            inline_cases,
                            default_chain,
                        )
                    )
                    lines.append(f"{prefix}}}")
                    continue
                for test in node.tests:
                    kind, value, target = test
                    if kind == "case":
                        pending_cases.append(test)
                        continue
                    lines.extend(
                        self._emit_dispatch_cases(
                            selector,
                            pending_cases,
                            indent,
                            inline_cases,
                            default_chain,
                        )
                    )
                    pending_cases.clear()
                    lines.extend(
                        self._emit_control_transfer(
                            target,
                            indent,
                            self._render_condition(value),
                        )
                    )
                lines.extend(
                    self._emit_dispatch_cases(
                        selector,
                        pending_cases,
                        indent,
                        inline_cases,
                        default_chain,
                    )
                )
                continue
            if isinstance(node, IfRegion):
                lines.extend(self._emit_if_region(node, indent))
                continue
            if isinstance(node, DoWhileRegion):
                prefix = "    " * indent
                lines.append(f"{prefix}do {{")
                lines.extend(self._emit_nodes(node.body_nodes, indent + 1))
                lines.append(f"{prefix}}} while ({self._render_condition(node.condition)});")
                continue
            if isinstance(node, WhileRegion):
                prefix = "    " * indent
                if node.header_has_body:
                    lines.append(f"{prefix}while (true) {{")
                    lines.extend(self._emit_block_body(node.header, indent + 1))
                    exit_condition = negate_condition(self._render_condition(node.condition))
                    lines.append(f"{prefix}    if ({exit_condition}) break;")
                    lines.extend(self._emit_nodes(node.body_nodes, indent + 1))
                    lines.append(f"{prefix}}}")
                else:
                    header_label = self.blocks[node.header].label
                    body_indices = self._region_indices(node.body_nodes)
                    if header_label in self.remaining_targets and node.header not in body_indices:
                        lines.append(f"{prefix}{header_label}:")
                    lines.append(f"{prefix}while ({self._render_condition(node.condition)}) {{")
                    lines.extend(self._emit_nodes(node.body_nodes, indent + 1))
                    lines.append(f"{prefix}}}")
                continue
            if isinstance(node, DisabledRegion):
                lines.extend(self._emit_block_body(node.header, indent))
        return lines

    def function_signature(self, function_name: str) -> str:
        parameters: list[str] = []
        for parameter_kind, parameter in self.parameter_bindings:
            if parameter_kind == "stack":
                c_type = (
                    merge_c_types(self.stack_parameter_type_hints.get(parameter, set()))
                    or "int32_t"
                )
                separator = "" if c_type.endswith("*") else " "
                parameters.append(
                    f"{c_type}{separator}{self.stack_parameter_names[parameter]}"
                )
                continue
            name = parameter
            array = self.local_array_by_base.get(name)
            if array is None:
                c_type = self.variable_types.get(name, "int32_t")
                separator = "" if c_type.endswith("*") else " "
                parameters.append(f"{c_type}{separator}{self.variable_names[name]}")
                continue
            separator = "" if array.c_type.endswith("*") else " "
            parameters.append(
                f"{array.c_type}{separator}{self.variable_names[name]}[{array.count}]"
            )
        if not parameters:
            parameters.append("void")
        return f"static {self.return_type} {function_name}({', '.join(parameters)})"

    def parameter_c_types(self) -> tuple[str, ...]:
        result: list[str] = []
        for parameter_kind, parameter in self.parameter_bindings:
            if parameter_kind == "stack":
                result.append(
                    merge_c_types(self.stack_parameter_type_hints.get(parameter, set()))
                    or "int32_t"
                )
                continue
            array = self.local_array_by_base.get(parameter)
            if array is None:
                result.append(self.variable_types.get(parameter, "int32_t"))
            else:
                result.append(f"{array.c_type.rstrip()} *")
        return tuple(result)

    def _uses_vm_context(self) -> bool:
        for block_index, block in enumerate(self.blocks):
            if block_index in self.omitted_blocks:
                continue
            for statement in block.statements:
                if id(statement) in self.parameter_statements or statement.kind == "state":
                    continue
                text = re.sub(r"vm_(?:push|pop|arg)\(vm(?:, [^)]*)?\)", "", statement.text)
                condition = re.sub(
                    r"vm_(?:push|pop|arg)\(vm(?:, [^)]*)?\)",
                    "",
                    statement.condition or "",
                )
                if re.search(r"\bvm\b", text):
                    return True
                if re.search(r"\bvm\b", condition):
                    return True
        return False

    def emit_function(self, function_name: str) -> list[str]:
        arrays_found = [
            self.local_array_by_base[name]
            for name in self.variable_names
            if name in self.local_array_bases and name not in self.parameter_locals
        ]
        locals_found = [
            name
            for name in self.variable_names
            if (
                LOCAL_RE.fullmatch(name)
                or name.startswith(("phi_", "edge_phi_", "result_L"))
            )
            and name not in self.parameter_locals
            and name not in self.function_table_targets
            and name not in self.local_array_bases
            and name not in self.coalesced_local_array_members
            and name not in self.predeclared_variables
            and name not in self.inline_declarations.values()
        ]
        lines = [
            self.function_signature(function_name),
            "{",
        ]
        if self._uses_vm_context():
            lines.append("    BPVM *vm = bpvm_current();")
            lines.append("")
        for array in arrays_found:
            separator = "" if array.c_type.endswith("*") else " "
            initializer = self.local_array_initializers.get(array.base)
            string_initializer = self.local_string_initializers.get(array.base)
            if string_initializer is not None:
                rendered_type = "char"
                rendered_initializer = self._replace_references(string_initializer[0])
                source = (
                    format_source_lines(string_initializer[1])
                    if self.include_source_comments
                    else ""
                )
            elif initializer is None:
                rendered_type = array.c_type
                rendered_initializer = "{0}"
                source = ""
            else:
                rendered_type = array.c_type
                rendered_values = [
                    self._render_typed_value(array.c_type, value)
                    for value in initializer[0]
                ]
                if array.c_type.endswith("*") or array.c_type == "BPDEntryPoint":
                    rendered_values = [
                        "NULL" if self._is_zero_value(value) else value
                        for value in rendered_values
                    ]
                rendered_initializer = "{" + ", ".join(rendered_values) + "}"
                source = (
                    format_source_lines(initializer[1])
                    if self.include_source_comments
                    else ""
                )
            lines.append(
                f"    {rendered_type}{separator}{self.variable_names[array.base]}[{array.count}] "
                f"= {rendered_initializer};{source}"
            )
        position = 0
        while position < len(locals_found):
            name = locals_found[position]
            c_type = self.variable_types.get(name, "int32_t")
            if c_type.endswith("*"):
                lines.append(f"    {c_type}{self.variable_names[name]} = 0;")
                position += 1
                continue
            group = [name]
            cursor = position + 1
            while (
                cursor < len(locals_found)
                and len(group) < 4
                and self.variable_types.get(locals_found[cursor], "int32_t") == c_type
            ):
                group.append(locals_found[cursor])
                cursor += 1
            declarations = ", ".join(f"{self.variable_names[item]} = 0" for item in group)
            separator = "" if c_type.endswith(" ") else " "
            lines.append(f"    {c_type}{separator}{declarations};")
            position = cursor
        if arrays_found or locals_found:
            lines.append("")
        rendered_body = self._emit_nodes(self.regions, 1)
        rendered_body = fold_adjacent_rendered_stack_operations(rendered_body)
        rendered_body = lower_dead_rendered_stack_pushes(rendered_body)
        rendered_body = fold_redundant_rendered_gotos(rendered_body)
        rendered_body = fold_single_entry_rendered_guards(rendered_body)
        rendered_body = fold_redundant_rendered_gotos(rendered_body)
        rendered_body = legalize_rendered_labels(rendered_body)
        lines.extend(clarify_rendered_control_expressions(rendered_body))
        lines.append("}")
        return lines


def propagate_called_array_types(emitters: list[StructuredEmitter]) -> None:
    def unified_element_type(left: str, right: str, type_id: int) -> str:
        if left == right:
            return left
        default = {0: "int8_t", 1: "int16_t", 2: "int32_t", 3: "int64_t"}[type_id]
        if left == default:
            return right
        if right == default:
            return left
        if "intptr_t" in {left, right}:
            return "intptr_t"
        merged = merge_c_types([left, right])
        if merged is not None:
            return merged
        return "intptr_t" if type_id in {2, 3} else default

    by_label = {
        emitter.blocks[0].label: emitter
        for emitter in emitters
        if emitter.blocks
    }
    changed = True
    while changed:
        changed = False
        for caller in emitters:
            for block_index, block in enumerate(caller.blocks):
                if block_index in caller.omitted_blocks:
                    continue
                for statement in block.statements:
                    for name, arguments in iter_function_calls(statement.text):
                        if name != "call_direct" or not arguments:
                            continue
                        target = FUNCTION_TABLE_ENTRY_RE.fullmatch(arguments[0].strip())
                        if target is None:
                            continue
                        callee = by_label.get(target.group("label"))
                        if callee is None:
                            continue
                        for position, (parameter_kind, parameter) in enumerate(
                            callee.parameter_bindings
                        ):
                            if parameter_kind != "local":
                                continue
                            array = callee.local_array_by_base.get(parameter)
                            argument_index = position + 1
                            if array is None or argument_index >= len(arguments):
                                continue
                            argument = strip_outer_parentheses(arguments[argument_index])
                            indexed = local_indexed_address(
                                argument,
                                array.element_size,
                                allow_bare=True,
                            )
                            if indexed is None:
                                continue
                            base, start_text = indexed
                            is_bare = not argument.startswith("&")
                            if is_bare and base not in caller.parameter_locals:
                                continue
                            start_index = constant_integer(start_text)
                            if start_index is None or start_index < 0:
                                continue
                            required_count = start_index + array.count
                            current = caller.local_array_by_base.get(base)
                            if current is not None:
                                if current.type_id != array.type_id:
                                    continue
                                c_type = unified_element_type(
                                    current.c_type,
                                    array.c_type,
                                    array.type_id,
                                )
                                if current.c_type != c_type:
                                    current.c_type = c_type
                                    caller._reference_cache.clear()
                                    changed = True
                                if array.c_type != c_type:
                                    array.c_type = c_type
                                    callee._reference_cache.clear()
                                    changed = True
                                if current.count < required_count:
                                    current.count = required_count
                                    caller._reference_cache.clear()
                                    changed = True
                                continue
                            propagated = LocalArray(
                                base=base,
                                type_id=array.type_id,
                                c_type=array.c_type,
                                element_size=array.element_size,
                                count=required_count,
                                copies=[],
                                is_parameter=base in caller.parameter_locals,
                            )
                            caller.local_arrays.append(propagated)
                            caller.local_array_by_base[base] = propagated
                            caller.local_array_bases.add(base)
                            caller.inline_declarations = {
                                statement_id: name
                                for statement_id, name in caller.inline_declarations.items()
                                if name != base
                            }
                            caller._reference_cache.clear()
                            changed = True


def _expression_c_type(emitter: StructuredEmitter, text: str) -> str | None:
    return _infer_text_c_type(
        text,
        lambda name: emitter.variable_types.get(name),
        emitter.local_array_by_base,
        emitter.stack_parameter_type_hints,
    )


def _infer_text_c_type(
    text: str,
    variable_type: Callable[[str], str | None],
    local_arrays: dict[str, LocalArray] | None = None,
    stack_types: dict[int, set[str]] | None = None,
) -> str | None:
    """Infer an expression type from its outer operation and solved leaves."""
    token = strip_outer_parentheses(text)
    if token.startswith('"') and token.endswith('"'):
        return "const char *"
    if token in {"true", "false"} or constant_integer(token) is not None:
        return "int32_t"
    conditional = split_top_level_ternary(token)
    if conditional is not None:
        branch_types = [
            _infer_text_c_type(branch, variable_type, local_arrays, stack_types)
            for branch in conditional[1:]
            if constant_integer(strip_outer_parentheses(branch)) != 0
        ]
        return merge_c_types([value for value in branch_types if value is not None])
    pointer_cast = re.match(
        r"^\((?P<type>(?:const\s+)?[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s*\*)\)",
        token,
    )
    if pointer_cast is not None:
        return re.sub(r"\s+", " ", pointer_cast.group("type")).replace(" *", " *")
    vm_arg = VM_ARG_EXACT_RE.fullmatch(token)
    if vm_arg is not None:
        index = int(vm_arg.group("index"))
        return merge_c_types((stack_types or {}).get(index, set()))
    if LOCAL_EXACT_RE.fullmatch(token):
        array = (local_arrays or {}).get(token)
        if array is not None:
            return (
                "uint8_t *"
                if array.c_type.endswith("*")
                else f"{array.c_type} *"
            )
        return variable_type(token)
    if re.fullmatch(r"[A-Za-z_]\w*", token):
        return variable_type(token)
    outer_call = re.match(r"^(?P<name>[A-Za-z_]\w*)\s*\(", token)
    if outer_call is not None:
        # The type of a function pointer is not the type returned by calling
        # it.  In particular, ``entry(...)`` must not make the destination an
        # entry-point pointer merely because ``entry`` itself has that type.
        return {
            "streq": "int32_t",
            "strlen": "int32_t",
            "bpd_strstr_index": "int32_t",
            "message": "int32_t",
        }.get(outer_call.group("name"))
    indexed = VALUE_INDEXED_TARGET_RE.fullmatch(token)
    if indexed is not None:
        base = indexed.group("base")
        is_subscript = indexed.group("tail").lstrip().startswith("[")
        array = (local_arrays or {}).get(base)
        if array is not None:
            if indexed.group("address"):
                return (
                    "uint8_t *"
                    if array.c_type.endswith("*")
                    else f"{array.c_type} *"
                )
            return array.c_type
        base_type = variable_type(base)
        if is_subscript and base_type is not None and base_type.endswith("*"):
            # Subscript expressions yield the pointed-to element, whereas
            # taking their address preserves the original pointer type.
            if indexed.group("address"):
                return base_type
            return base_type[:-1].rstrip()
        if indexed.group("address"):
            return f"{base_type} *" if base_type is not None else "uint8_t *"
        return base_type
    if token.startswith("&"):
        return "uint8_t *"
    return None


def _set_emitter_type(emitter: StructuredEmitter, name: str, c_type: str) -> bool:
    current = emitter.variable_types.get(name)
    merged = merge_c_types([current, c_type] if current is not None else [c_type])
    if merged is None or merged == current:
        return False
    emitter.variable_types[name] = merged
    emitter._reference_cache.clear()
    return True


def _set_stack_parameter_type(
    emitter: StructuredEmitter,
    index: int,
    c_type: str,
) -> bool:
    hints = emitter.stack_parameter_type_hints.setdefault(index, set())
    if c_type in hints:
        return False
    hints.add(c_type)
    return True


def _normalize_direct_calls(text: str, arities: dict[str, int]) -> str:
    """Drop VM-preserved lower values from direct calls in any expression context."""
    rendered = text
    while True:
        changed = False
        for name, arguments in iter_function_calls(rendered):
            if name != "call_direct" or not arguments:
                continue
            target = FUNCTION_TABLE_ENTRY_RE.fullmatch(arguments[0].strip())
            if target is None or target.group("label") not in arities:
                continue
            arity = arities[target.group("label")]
            if len(arguments) - 1 <= arity:
                continue
            call_arguments = arguments[-arity:] if arity else []
            original = f"call_direct({', '.join(arguments)})"
            replacement = f"call_direct({', '.join([arguments[0], *call_arguments])})"
            if original not in rendered:
                continue
            rendered = rendered.replace(original, replacement, 1)
            changed = True
            break
        if not changed:
            return rendered


def _normalize_region_direct_calls(
    nodes: list[Region],
    arities: dict[str, int],
) -> None:
    """Keep structured conditions in sync with normalized IR statements."""
    for node in nodes:
        if isinstance(node, IfRegion):
            node.condition = _normalize_direct_calls(node.condition, arities)
            _normalize_region_direct_calls(node.then_nodes, arities)
            _normalize_region_direct_calls(node.else_nodes, arities)
            continue
        if isinstance(node, WhileRegion):
            node.condition = _normalize_direct_calls(node.condition, arities)
            _normalize_region_direct_calls(node.body_nodes, arities)
            continue
        if isinstance(node, DoWhileRegion):
            node.condition = _normalize_direct_calls(node.condition, arities)
            _normalize_region_direct_calls(node.body_nodes, arities)
            continue
        if isinstance(node, DisabledRegion):
            _normalize_region_direct_calls(node.body_nodes, arities)
            continue
        if isinstance(node, DispatchRegion):
            node.selector = _normalize_direct_calls(node.selector, arities)
            node.tests = [
                (kind, _normalize_direct_calls(value, arities), target)
                for kind, value, target in node.tests
            ]


MULTI_DIRECT_RESULT_RE = re.compile(
    r"^values\((?P<destinations>[^()]*)\)\s*=\s*"
    r"(?P<call>call_direct\(label_ref\((?P<label>[A-Za-z_]\w*)\)(?P<tail>.*)\));$"
)
RETURN_VALUES_RE = re.compile(r"^return values\((?P<values>.*)\);$")


def _prune_dead_direct_call_results(emitters: list[StructuredEmitter]) -> None:
    """Shrink direct-call result tuples using actual caller-side liveness."""
    by_label = {
        emitter.blocks[0].label: emitter
        for emitter in emitters
        if emitter.blocks
    }
    candidates: dict[
        str,
        list[tuple[StructuredEmitter, BasicBlock, Statement, list[str], str]],
    ] = {}
    direct_call_counts: Counter[str] = Counter()
    address_taken: set[str] = set()

    for caller in emitters:
        for block in caller.blocks:
            for statement in block.statements:
                for name, arguments in iter_function_calls(statement.text):
                    if name != "call_direct" or not arguments:
                        continue
                    target = FUNCTION_TABLE_ENTRY_RE.fullmatch(arguments[0].strip())
                    if target is not None:
                        direct_call_counts[target.group("label")] += 1
                match = MULTI_DIRECT_RESULT_RE.fullmatch(statement.text.strip())
                if match is not None:
                    destinations = [
                        value.strip()
                        for value in split_arguments(match.group("destinations"))
                    ]
                    candidates.setdefault(match.group("label"), []).append(
                        (caller, block, statement, destinations, match.group("call"))
                    )
                for label in re.findall(r"label_ref\(([A-Za-z_]\w*)\)", statement.text):
                    if f"call_direct(label_ref({label})" not in statement.text:
                        address_taken.add(label)

    for label, calls in candidates.items():
        callee = by_label.get(label)
        if (
            callee is None
            or label in address_taken
            or len(calls) != direct_call_counts[label]
        ):
            continue

        demanded: set[int] = set()
        dead_pushes: dict[int, set[int]] = {}
        for caller, _, definition, destinations, _ in calls:
            for position, destination in enumerate(destinations):
                live = False
                for block in caller.blocks:
                    for statement in block.statements:
                        if statement is definition:
                            continue
                        if (
                            (
                                statement.kind in {"stack", "stack_preserved"}
                                and statement.text.strip()
                                == f"vm_push(vm, {destination});"
                            )
                            or (
                                statement.kind == "effect"
                                and statement.text.strip()
                                == f"(void)({destination});"
                            )
                        ):
                            dead_pushes.setdefault(id(block), set()).add(id(statement))
                            continue
                        if re.search(rf"\b{re.escape(destination)}\b", statement.text) or re.search(
                            rf"\b{re.escape(destination)}\b",
                            statement.condition or "",
                        ):
                            live = True
                            break
                    if live:
                        break
                if live:
                    demanded.add(position)
        if len(demanded) > 1:
            continue
        selected = next(iter(demanded), None)

        return_sites: list[tuple[BasicBlock, Statement, list[str]]] = []
        safe = True
        for block in callee.blocks:
            for statement in block.statements:
                if statement.kind != "return":
                    continue
                match = RETURN_VALUES_RE.fullmatch(statement.text.strip())
                if match is None:
                    safe = False
                    break
                values = split_arguments(match.group("values"))
                if selected is not None and selected >= len(values):
                    safe = False
                    break
                selected_calls = (
                    list(iter_function_calls(values[selected]))
                    if selected is not None
                    else []
                )
                if selected_calls:
                    safe = False
                    break
                return_sites.append((block, statement, values))
            if not safe:
                break
        if not safe or not return_sites:
            continue

        for block, statement, values in return_sites:
            position = block.statements.index(statement)
            effects = [
                Statement("effect", f"(void)({value});", statement.source_lines)
                for index, value in enumerate(values)
                if index != selected and any(iter_function_calls(value))
            ]
            block.statements[position:position] = effects
            statement.text = (
                f"return {values[selected]};"
                if selected is not None
                else "return;"
            )
        callee.return_type = "int32_t" if selected is not None else "void"
        callee._reference_cache.clear()

        for caller, _, statement, destinations, call_text in calls:
            statement.text = (
                f"{destinations[selected]} = {call_text};"
                if selected is not None
                else f"{call_text};"
            )
            statement.kind = "store" if selected is not None else "call"
            removed_names = {
                destination
                for index, destination in enumerate(destinations)
                if index != selected
            }
            for block in caller.blocks:
                removed = dead_pushes.get(id(block), set())
                if removed:
                    block.statements = [
                        item for item in block.statements if id(item) not in removed
                    ]
            for name in removed_names:
                caller.variable_names.pop(name, None)
                caller.variable_types.pop(name, None)
                caller.predeclared_variables.discard(name)
            caller._reference_cache.clear()


def solve_calling_conventions(emitters: list[StructuredEmitter]) -> None:
    """Solve scalar parameter types and stack-slot types across direct calls."""
    _prune_dead_direct_call_results(emitters)
    by_label = {
        emitter.blocks[0].label: emitter
        for emitter in emitters
        if emitter.blocks
    }
    for _ in range(4):
        arities = {
            label: len(emitter.parameter_bindings)
            for label, emitter in by_label.items()
        }
        for emitter in emitters:
            for block_index, block in enumerate(emitter.blocks):
                if block_index in emitter.omitted_blocks:
                    continue
                for statement in block.statements:
                    statement.text = _normalize_direct_calls(statement.text, arities)
                    if statement.condition is not None:
                        statement.condition = _normalize_direct_calls(statement.condition, arities)
            _normalize_region_direct_calls(emitter.regions, arities)
        # Run pruning for every function in the round.  ``any(generator)``
        # short-circuits after the first change and used to leave the
        # remaining emitters unprocessed until a later (or missing) round.
        pruned = [emitter.prune_unused_stack_parameters() for emitter in emitters]
        if not any(pruned):
            break

    # A BP call can leave its oldest argument/result word available to the
    # caller even when the callee ends in a bare ``ret``.  Chained helper
    # calls rely on this convention.  If a nominally-void direct call is
    # consumed as an expression (or nested inside another direct call), make
    # that implicit word explicit in the C signature and return statements.
    implicit_result_labels: set[str] = set()
    for caller in emitters:
        for block_index, block in enumerate(caller.blocks):
            if block_index in caller.omitted_blocks:
                continue
            for statement in block.statements:
                targets = [
                    target.group("label")
                    for name, arguments in iter_function_calls(statement.text)
                    if name == "call_direct" and arguments
                    for target in [FUNCTION_TABLE_ENTRY_RE.fullmatch(arguments[0].strip())]
                    if target is not None
                ]
                if targets and (statement.kind != "call" or len(targets) > 1):
                    implicit_result_labels.update(targets)
                if statement.condition is not None:
                    implicit_result_labels.update(
                        target.group("label")
                        for name, arguments in iter_function_calls(statement.condition)
                        if name == "call_direct" and arguments
                        for target in [
                            FUNCTION_TABLE_ENTRY_RE.fullmatch(arguments[0].strip())
                        ]
                        if target is not None
                    )
    for label in implicit_result_labels:
        callee = by_label.get(label)
        if callee is None or callee.return_type != "void":
            continue
        callee.return_type = "int32_t"
        if callee.parameter_bindings:
            callee.implicit_return_binding = callee.parameter_bindings[0]

    for _ in range(max(2, len(emitters) * 2)):
        changed = False
        for caller in emitters:
            for block_index, block in enumerate(caller.blocks):
                if block_index in caller.omitted_blocks:
                    continue
                for statement in block.statements:
                    for name, arguments in iter_function_calls(statement.text):
                        if name != "call_direct" or len(arguments) < 1:
                            continue
                        target = FUNCTION_TABLE_ENTRY_RE.fullmatch(arguments[0].strip())
                        if target is None:
                            continue
                        callee = by_label.get(target.group("label"))
                        if callee is None:
                            continue
                        for position, (parameter_kind, parameter) in enumerate(
                            callee.parameter_bindings
                        ):
                            argument_index = position + 1
                            if argument_index >= len(arguments):
                                break
                            argument = arguments[argument_index]
                            argument_type = _expression_c_type(caller, argument)
                            if argument_type is not None and argument_type != "int32_t":
                                if parameter_kind == "local":
                                    current_parameter_type = callee.variable_types.get(parameter)
                                    if not (
                                        current_parameter_type == "const char *"
                                        and argument_type == "char *"
                                    ):
                                        changed |= _set_emitter_type(
                                            callee,
                                            parameter,
                                            argument_type,
                                        )
                                else:
                                    changed |= _set_stack_parameter_type(
                                        callee,
                                        parameter,
                                        argument_type,
                                    )
                            callee_type = (
                                callee.variable_types.get(parameter)
                                if parameter_kind == "local"
                                else merge_c_types(
                                    callee.stack_parameter_type_hints.get(parameter, set())
                                )
                            )
                            if callee_type is None:
                                continue
                            argument_name = strip_outer_parentheses(argument)
                            if LOCAL_EXACT_RE.fullmatch(argument_name):
                                changed |= _set_emitter_type(caller, argument_name, callee_type)
                            elif VM_ARG_EXACT_RE.fullmatch(argument_name):
                                index = int(VM_ARG_EXACT_RE.fullmatch(argument_name).group("index"))
                                changed |= _set_stack_parameter_type(
                                    caller,
                                    index,
                                    callee_type,
                                )
        if not changed:
            break


def infer_function_ranges(module: Module) -> list[tuple[int, int]]:
    blocks = module.blocks
    if not blocks:
        return []
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
    referenced_labels: set[str] = set()
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target = index_by_label.get(successor)
            if target is not None:
                predecessors[target].add(source_index)
        for statement in block.statements:
            referenced_labels.update(re.findall(r"label_ref\(([A-Za-z_][A-Za-z0-9_]*)\)", statement.text))

    roots = {0}
    for index in range(1, len(blocks)):
        if blocks[index].label in referenced_labels:
            roots.add(index)
            continue
        previous_control = last_control(blocks[index - 1])
        original_terminator = blocks[index - 1].terminator_opcode
        # A folded jump to a common return can precede an unreachable case
        # inside the same function. Only a bytecode return/indirect transfer
        # provides evidence for this otherwise unreferenced function boundary.
        original_function_end = (
            original_terminator is None
            or original_terminator == "ret"
            or (original_terminator == "jmp" and previous_control is not None and previous_control.kind == "jump_dynamic")
        )
        if (
            not predecessors[index]
            and previous_control is not None
            and previous_control.kind in {"return", "jump_dynamic"}
            and original_function_end
        ):
            roots.add(index)

    ordered = sorted(roots)
    return [(start, ordered[pos + 1] if pos + 1 < len(ordered) else len(blocks)) for pos, start in enumerate(ordered)]


def function_blocks_with_shared_tails(
    module: Module,
    start: int,
    end: int,
    ownership: FunctionOwnership | None = None,
) -> list[BasicBlock]:
    """补入线性切分遗漏的共享尾块。"""
    if ownership is not None:
        root_label = module.blocks[start].label
        selected = set(range(start, end))
        selected.update(ownership.reachable_by_root.get(root_label, ()))
        base_indices = list(range(start, end))
        shared_indices = sorted(selected.difference(base_indices))
        return [module.blocks[index] for index in base_indices + shared_indices]

    index_by_label = {block.label: index for index, block in enumerate(module.blocks)}
    selected = set(range(start, end))
    pending = list(selected)
    while pending:
        source_index = pending.pop()
        block = module.blocks[source_index]
        targets = list(block.successors)
        targets.extend(
            statement.target
            for statement in block.statements
            if statement.kind in {"branch", "jump"} and statement.target is not None
        )
        for label in targets:
            target_index = index_by_label.get(label)
            if target_index is None or target_index in selected:
                continue
            selected.add(target_index)
            pending.append(target_index)

    base_indices = list(range(start, end))
    shared_indices = sorted(selected.difference(base_indices))
    return [module.blocks[index] for index in base_indices + shared_indices]


def clone_function_blocks(blocks: list[BasicBlock]) -> list[BasicBlock]:
    """仅复制发射阶段会修改的块和语句。"""
    return [
        BasicBlock(
            label=block.label,
            source_line=block.source_line,
            statements=[
                Statement(
                    kind=statement.kind,
                    text=statement.text,
                    source_lines=statement.source_lines,
                    target=statement.target,
                    condition=statement.condition,
                    node_kind=statement.node_kind,
                    effects=statement.effects,
                    control_target=statement.control_target,
                    operands=statement.operands,
                )
                for statement in block.statements
            ],
            successors=list(block.successors),
            synthetic=block.synthetic,
            stack_in=block.stack_in,
            stack_out=block.stack_out,
            stack_phis=block.stack_phis,
            terminator_opcode=block.terminator_opcode,
        )
        for block in blocks
    ]


FIXED_POINTER_CALL_RE = re.compile(
    r"\bcall_pointer\(\s*(?P<address>0[xX][0-9A-Fa-f]+)"
    r"\s*(?P<separator>,\s*)?"
)
MULTI_RESULT_CALL_PREFIX_RE = re.compile(
    r"(?:values\([^()\n]*\)|BPD[A-Za-z0-9_]+\s+[A-Za-z_]\w*)\s*=\s*$"
)


def lower_fixed_pointer_calls(source: str) -> str:
    """Name fixed native entries and keep their addresses out of call sites."""
    kinds: dict[int, set[str]] = {}
    for match in FIXED_POINTER_CALL_RE.finditer(source):
        address = int(match.group("address"), 0)
        line_start = source.rfind("\n", 0, match.start()) + 1
        prefix = source[line_start:match.start()]
        kind = (
            "values"
            if MULTI_RESULT_CALL_PREFIX_RE.search(prefix) is not None
            else "scalar"
        )
        kinds.setdefault(address, set()).add(kind)

    aliases = {
        address: (
            f"bpd_native_values_{address:08x}"
            if kind_set == {"values"}
            else f"bpd_native_{address:08x}"
        )
        for address, kind_set in kinds.items()
        if len(kind_set) == 1
    }
    if not aliases:
        return source

    def replace(match: re.Match[str]) -> str:
        alias = aliases.get(int(match.group("address"), 0))
        return match.group(0) if alias is None else f"{alias}("

    rendered = FIXED_POINTER_CALL_RE.sub(replace, source)
    source_lines = rendered.splitlines()
    insertion = max(
        (
            index + 1
            for index, line in enumerate(source_lines)
            if line.startswith("#include ")
        ),
        default=0,
    )
    declarations = [
        "",
        "/* 固定 BP 原生入口别名。 */",
        "typedef intptr_t (*BPDNativeEntry)();",
    ]
    if any(kind_set == {"values"} for kind_set in kinds.values()):
        declarations.append("typedef BPDValues (*BPDNativeValuesEntry)();")
    declarations.append("")
    for address in sorted(aliases):
        c_type = (
            "BPDNativeValuesEntry"
            if kinds[address] == {"values"}
            else "BPDNativeEntry"
        )
        declarations.append(
            f"#define {aliases[address]} "
            f"(({c_type})(uintptr_t)0x{address:08x})"
        )
    source_lines[insertion:insertion] = declarations
    return "\n".join(source_lines)


def emit_c(module: Module, *, include_source_comments: bool = True) -> str:
    base_name = sanitize_identifier(f"bpd_{module.source_path.stem}")
    ranges = infer_function_ranges(module)
    function_names: list[str] = []
    function_refs: dict[str, str] = {}
    for index, (start, _) in enumerate(ranges):
        label = module.blocks[start].label
        function_name = f"{base_name}_entry" if index == 0 else sanitize_identifier(f"{base_name}_{label}")
        function_names.append(function_name)
        function_refs[label] = function_name
    ownership = compute_function_ownership(
        module.blocks,
        [module.blocks[start].label for start, _ in ranges],
    )

    emitters: list[StructuredEmitter] = []
    next_variable_index = 1
    for start, end in ranges:
        emitter = StructuredEmitter(
            module,
            clone_function_blocks(
                function_blocks_with_shared_tails(module, start, end, ownership)
            ),
            function_refs,
            variable_index_start=next_variable_index,
            include_source_comments=include_source_comments,
        )
        emitters.append(emitter)
        next_variable_index = emitter.next_variable_index

    solve_calling_conventions(emitters)
    propagate_called_array_types(emitters)
    downgraded_arrays = [
        emitter.downgrade_mixed_parameter_arrays() for emitter in emitters
    ]
    if any(downgraded_arrays):
        solve_calling_conventions(emitters)
    call_parameter_types = {
        name: emitter.parameter_c_types()
        for name, emitter in zip(function_names, emitters)
    }
    for emitter in emitters:
        emitter.call_parameter_types = call_parameter_types
        emitter._reference_cache.clear()

    # 先渲染函数，再按最终 C 文本补充函数指针类型。
    rendered_functions = [
        emitter.emit_function(name)
        for name, emitter in zip(function_names, emitters)
    ]
    rendered_text = "\n".join(
        line for function in rendered_functions for line in function
    )
    needs_entrypoint_typedef = "BPDEntryPoint" in rendered_text

    lines = [
        "/*",
        f" * {module.source_path.name} 的结构化 BP VM 反编译结果。",
        f" * BP engine: {module.engine.name} (generation {module.engine.generation}).",
        " * 操作码栈效果来自内置语义表。",
        " */",
        '#include "bp_vm_runtime.h"',
        "#include <string.h>",
    ]
    lines.append("")
    module_text = "\n".join(
        "\n".join((statement.text, statement.condition or ""))
        for block in module.blocks
        for statement in block.statements
    )
    if "bpd_strstr_index(" in module_text:
        lines.extend([
            "static int32_t bpd_strstr_index(const char *haystack, const char *needle)",
            "{",
            "    const char *match = strstr(haystack, needle);",
            "    return match != NULL ? (int32_t)(match - haystack) : -1;",
            "}",
            "",
        ])
    if any("BPDProgramEntry" in emitter.variable_types.values() for emitter in emitters):
        lines.extend([
            "typedef intptr_t (*BPDProgramEntry)();",
            "",
        ])
    if needs_entrypoint_typedef or any(
        emitter.function_table_groups
        or any(array.c_type == "BPDEntryPoint" for array in emitter.local_arrays)
        or "BPDEntryPoint" in emitter.variable_types.values()
        or any(
            re.search(
                r"(?:\((?:load_typed|load_i32|load_indexed|load_indirect_indexed)\(.*\)\)|"
                r"\((?:global_[0-9A-Fa-f]+|(?:local_[0-9A-Fa-f]+|result_L\d+(?:_\d+)?)"
                r"\s*\[[^\]]+\])\))\s*\(",
                statement.text,
            )
            for block in emitter.blocks
            for statement in block.statements
        )
        for emitter in emitters
    ):
        lines.extend([
            "typedef intptr_t (*BPDEntryPoint)();",
            "",
        ])
    for declaration in structure_declarations(module_text):
        lines.extend(declaration.splitlines())
        lines.append("")
    for declaration in result_declarations(module_text):
        lines.extend(declaration.splitlines())
        lines.append("")
    for name, emitter in zip(function_names, emitters):
        lines.append(emitter.function_signature(name) + ";")
    if function_names:
        lines.append("")

    for position, function_lines in enumerate(rendered_functions):
        if position:
            lines.append("")
        lines.extend(function_lines)
    lines.append("")
    source = "\n".join(lines)
    source = lower_fixed_pointer_calls(source)
    source = optimize_binary_resource_source(source)
    absolute_globals = re.search(
        r"\bglobal_(?:i(?:8|16|32|64)_(?:array_)?|fn_array_)[0-9a-f]{8}\b",
        source,
    )
    if absolute_globals:
        source_lines = source.splitlines()
        insertion = max(
            (
                index + 1
                for index, line in enumerate(source_lines)
                if line.startswith("#include ")
            ),
            default=0,
        )
        declarations = [
            "",
            "/* global_i* 和 global_fn_array_* 是 BP 全局内存的类型化视图。 */",
        ]
        source_lines[insertion:insertion] = declarations
        source = "\n".join(source_lines)
    residual_pop_count = source.count("stack_pop()")
    residual_push_count = source.count("stack_push(")
    module.stack_diagnostics = []
    if residual_pop_count or residual_push_count:
        module.stack_diagnostics.append(
            f"rendered stack protocol remains: {residual_push_count} pushes, "
            f"{residual_pop_count} pops"
        )
    return source
