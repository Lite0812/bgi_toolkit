from __future__ import annotations

import ast
import json
import re
from ..bp_engine import engine_scope, get_profile

from .asm_parser import AsmInstruction, AsmLabel, AssemblyModule, split_arguments
from .cfg import ControlFlowGraph, analyze_stack_ssa
from .instruction_sizes import instruction_size
from .ir import (
    BasicBlock,
    Effect,
    Expression,
    ExpressionKind,
    IndirectTarget,
    InvalidBoundaryTarget,
    LiftStatistics,
    Module,
    RelativeDisplacementTarget,
    Statement,
    StatementKind,
    ValueType,
)
from .opcode_semantics import get_opcode_semantics
from .result_semantics import multi_result_schema
from .structure_semantics import (
    recovered_copy_size,
    recover_structure_arithmetic,
    recover_structure_field_load,
    recover_structure_store_target,
    recover_structure_string_address,
)


INT_RE = re.compile(r"^-?(?:0[xX][0-9A-Fa-f]+|\d+)$")
LABEL_REF_RE = re.compile(r"^label_ref\((?P<label>[A-Za-z_][A-Za-z0-9_]*)\)$")
LOCAL_ADDRESS_RE = re.compile(r"^&local_(?P<offset>[0-9A-Fa-f]+)(?P<tail>.*)$")
ADDRESS_DELTA_RE = re.compile(r"\s*(?P<operator>[+-])\s*(?P<delta>0[xX][0-9A-Fa-f]+|\d+)")

BINARY_OPS: dict[str, tuple[str, int]] = {
    "add": ("+", 60),
    "sub": ("-", 60),
    "mul": ("*", 70),
    "div": ("/", 70),
    "mod": ("%", 70),
    "and": ("&", 40),
    "or": ("|", 30),
    "xor": ("^", 35),
    "shl": ("<<", 50),
    "shr": (">>", 50),
    "sar": (">>", 50),
    "eq": ("==", 45),
    "neq": ("!=", 45),
    "leq": ("<=", 45),
    "geq": (">=", 45),
    "lt": ("<", 45),
    "gt": (">", 45),
    "bool_and": ("&&", 20),
    "bool_or": ("||", 10),
}

UNARY_OPS: dict[str, str] = {"not": "~", "bool_zero": "!"}

FLAG_OPS: dict[int, tuple[str, int]] = {
    0: ("==", 45),
    1: ("!=", 45),
    2: ("<", 45),
    3: ("<=", 45),
    4: (">=", 45),
    5: (">", 45),
    6: ("&", 40),
    7: ("|", 30),
    8: ("&&", 20),
    9: ("||", 10),
}

STORE_ARITHMETIC_OPS: dict[int, tuple[str, int]] = {
    0: ("+", 60),
    1: ("-", 60),
    2: ("*", 70),
    3: ("/", 70),
    4: ("%", 70),
    5: ("&", 40),
    6: ("|", 30),
    7: ("^", 35),
    8: ("<<", 50),
    9: (">>", 50),
    10: (">>", 50),
}

DIRECT_JUMP_OPS = {
    "cmp_imm_jc",
    "jcmp",
    "jc_cmp_code_ref_imm",
    "jc_cmp_code_ref_indexed_imm",
    "jc_test_code_ref",
    "jc_test_code_ref_indexed",
}

CONSTANT_GROUP_RESULTS = {
    "sys2_push_true": "true",
    "snd1_return_constant_20": "20",
}

DEFERRED_RESULT_OPCODES = {
    "grp1_resource_named_object_create",
    "grp1_resource_rect_object_create",
    "grp1_sprite_create_basic",
    "grp1_sprite_create_extended",
    "grp1_sprite_create_scaled",
    "grp3_resource_draw_object_create",
    "sys1_create_encode_struct_process",
    "sys1_wait_time_key",
}

SCRIPT_COMMAND_NAMES: dict[str, dict[int, str]] = {
    "environ": {
        0: "environ_initialize",
        1: "environ_calculate_transition_step",
        2: "environ_set_object_rotation",
        3: "environ_set_display_mode",
        4: "environ_apply_display_size",
        5: "environ_set_fullscreen_mode",
        6: "environ_apply_bgm_volumes",
        7: "environ_bind_default_input",
        8: "environ_noop",
        9: "environ_apply_audio_mix",
        10: "environ_set_frame_rate",
        11: "environ_preload_system_sounds",
        12: "environ_apply_effect_movie_volumes",
        13: "environ_apply_voice_volumes",
        14: "environ_apply_special_se_volume",
        15: "environ_set_cursor_active_range",
        16: "environ_set_background_process_enabled",
    },
}

BLOCK_TERMINATORS = DIRECT_JUMP_OPS | {"jc", "jc_legacy", "jmp", "jump_rel16", "ret"}


def parse_int(text: str) -> int | None:
    token = text.strip()
    if not INT_RE.fullmatch(token):
        return None
    try:
        return int(token, 0)
    except ValueError:
        return None


def c_int(text: str) -> str:
    token = text.strip()
    return token if parse_int(token) is not None else token


def code_ref_name(text: str) -> str:
    value = parse_int(text)
    if value is None:
        return f"code_ref_load(vm, {text.strip()})"
    return f"local_{value & 0x3FFF:04x}"


def code_ref_type_id(text: str) -> int:
    value = parse_int(text)
    return ((value or 0) >> 14) & 3


def code_address_name(text: str) -> str:
    return "&" + code_ref_name(text)


def frame_address_name(text: str) -> str:
    # Opcode 04 carries a full u16 offset, unlike typed local descriptors
    # whose upper two bits encode the access width.
    value = parse_int(text)
    if value is None:
        return f"frame_address(vm, {text.strip()})"
    return f"&local_{value & 0xFFFF:04x}"


def global_ref_name(text: str) -> str:
    value = parse_int(text)
    if value is None:
        return f"vm_load_ref(vm, {text.strip()})"
    return f"global_{value & ((1 << get_profile().tag_shift) - 1):08x}"


def store_text(target: Expression, value: Expression, type_id: str = "0x2") -> str:
    address = target.text.strip()
    width = parse_int(type_id)
    normalized_type_id = (width or 0) & 3
    structure_target = recover_structure_store_target(address, normalized_type_id)
    if structure_target is not None:
        return f"{structure_target} = {value.text};"
    direct_local = local_slot_name(address) if normalized_type_id == 2 else None
    if direct_local is not None:
        return f"{direct_local} = {value.text};"
    absolute = parse_int(address)
    if (normalized_type_id == 2 and absolute is not None and absolute >= 0
            and (not get_profile().historical or absolute < (1 << get_profile().tag_shift))):
        return f"{global_ref_name(str(absolute))} = {value.text};"
    if re.fullmatch(
        r"&[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*)?",
        address,
    ):
        return f"{address[1:]} = {value.text};"
    type_name = {0: "i8", 1: "i16", 2: "i32", 3: "i64"}.get(normalized_type_id, "typed")
    return f"store_{type_name}({address}, {value.text});"


def local_slot_name(address: str) -> str | None:
    match = LOCAL_ADDRESS_RE.fullmatch(address.strip())
    if match is None:
        return None
    offset = int(match.group("offset"), 16)
    tail = match.group("tail")
    position = 0
    while position < len(tail):
        delta = ADDRESS_DELTA_RE.match(tail, position)
        if delta is None:
            return None
        value = int(delta.group("delta"), 0)
        offset += value if delta.group("operator") == "+" else -value
        position = delta.end()
    return f"local_{offset & 0x7FFF:04x}"


def direct_load_text(address: str, type_id: int) -> str | None:
    """Recover loads whose address names a concrete scalar or known field."""
    structure_value = recover_structure_field_load(address, 0, type_id)
    if structure_value is not None:
        return structure_value
    if type_id != 2:
        return None

    local = local_slot_name(address)
    if local is not None:
        return local

    absolute = parse_int(address)
    if absolute is not None and absolute >= 0:
        if get_profile().historical and absolute >= (1 << get_profile().tag_shift):
            return None  # Code/data/heap tags must not become global variables.
        return global_ref_name(str(absolute))

    direct = re.fullmatch(
        r"&(?P<value>[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*)?)",
        address.strip(),
    )
    return direct.group("value") if direct is not None else None


def c_string(text: str) -> str:
    token = text.strip()
    try:
        value = ast.literal_eval(token)
    except (SyntaxError, ValueError):
        value = token[1:-1] if len(token) >= 2 else token
    return json.dumps(str(value), ensure_ascii=False)


def parse_list(text: str) -> list[str]:
    token = text.strip()
    if not (token.startswith("[") and token.endswith("]")):
        return []
    return split_arguments(token[1:-1])


def format_raw_arg(text: str) -> str:
    token = text.strip()
    if not token:
        return "0"
    if token[0:1] in {'"', "'"}:
        return c_string(token)
    if token.startswith("[") and token.endswith("]"):
        values = parse_list(token)
        return "BPD_VALUES(" + ", ".join(format_raw_arg(item) for item in values) + ")"
    if re.fullmatch(r"L[0-9A-Fa-f]+", token):
        return f"label_ref({token})"
    return c_int(token)


def merge_lines(*groups: tuple[int, ...] | list[int] | int) -> tuple[int, ...]:
    merged: list[int] = []
    for group in groups:
        values = (group,) if isinstance(group, int) else group
        for value in values:
            if value not in merged:
                merged.append(value)
    return tuple(merged)


def wrap(expr: Expression, precedence: int, right: bool = False) -> str:
    needs_parentheses = expr.precedence < precedence or (right and expr.precedence == precedence)
    return f"({expr.text})" if needs_parentheses else expr.text


def wrap_binary_operand(
    expr: Expression,
    parent_operator: str,
    precedence: int,
    *,
    right: bool = False,
) -> str:
    """Preserve semantics and make precedence-sensitive combinations explicit."""
    rendered = wrap(expr, precedence, right=right)
    if rendered.startswith("(") and rendered.endswith(")"):
        return rendered

    child_operator = expr.operator
    if child_operator is None:
        return rendered

    arithmetic = {"+", "-", "*", "/", "%"}
    bitwise = {"&", "^", "|"}
    comparisons = {"==", "!=", "<", "<=", ">", ">="}
    logical = {"&&", "||"}
    needs_clarity = (
        parent_operator in {"<<", ">>"}
        and child_operator in arithmetic | bitwise
    ) or (
        parent_operator in bitwise
        and child_operator in comparisons | logical
    ) or (
        parent_operator in logical
        and child_operator in logical
        and child_operator != parent_operator
    )
    return f"({rendered})" if needs_clarity else rendered


def binary_expr(left: Expression, op: str, right: Expression, precedence: int) -> Expression:
    left_value = parse_int(left.text)
    right_value = parse_int(right.text)
    lines = merge_lines(left.source_lines, right.source_lines)
    if op == "+" and right_value == 1 and left.operator == "~" and len(left.operands) == 1:
        result = unary_expr("-", left.operands[0])
        result.source_lines = lines
        return result
    if op == "+" and left_value == 1 and right.operator == "~" and len(right.operands) == 1:
        result = unary_expr("-", right.operands[0])
        result.source_lines = lines
        return result
    if op == "+" and right.operator == "-" and len(right.operands) == 1:
        return binary_expr(left, "-", right.operands[0], precedence)
    if op == "-" and right.operator == "-" and len(right.operands) == 1:
        return binary_expr(left, "+", right.operands[0], precedence)
    if op == "+" and left_value == 0:
        return Expression(right.text, right.precedence, lines, right.operator, right.operands)
    if op == "-" and right_value == 0:
        return Expression(left.text, left.precedence, lines, left.operator, left.operands)
    if op == "*" and (left_value == -1 or right_value == -1):
        operand = right if left_value == -1 else left
        result = unary_expr("-", operand)
        result.source_lines = lines
        return result
    if op == "+":
        recovered_address = recover_structure_arithmetic(left.text, right.text)
        if recovered_address is not None:
            return Expression(recovered_address, 100, lines)
    if op == "+" and right_value == 0:
        return Expression(left.text, left.precedence, lines)
    if op == "+" and right_value is not None and right_value < 0:
        positive = Expression(str(abs(right_value)), 100, right.source_lines)
        return Expression(f"{wrap(left, precedence)} - {wrap(positive, precedence, right=True)}", precedence, lines)
    if op == "*" and right_value == 1:
        return Expression(left.text, left.precedence, lines)
    if op == "*" and left_value == 1:
        return Expression(right.text, right.precedence, lines, right.operator, right.operands)
    if op == "*" and (left_value == 0 or right_value == 0):
        return Expression("0", 100, lines)
    if op == ">>" and right_value in {16, 24} and left.operator == "<<" and len(left.operands) == 2:
        left_shift = parse_int(left.operands[1].text)
        if left_shift == right_value:
            c_type = "int16_t" if right_value == 16 else "int8_t"
            value = left.operands[0]
            return Expression(f"({c_type}){wrap(value, 80)}", 80, lines)
    if op in {"&&", "||"}:
        left_condition = constant_condition(left.text)
        right_condition = constant_condition(right.text)
        if op == "&&":
            if left_condition is False or right_condition is False:
                return Expression("false", 100, lines)
            if left_condition is True:
                return Expression(right.text, right.precedence, lines, right.operator, right.operands)
            if right_condition is True:
                return Expression(left.text, left.precedence, lines, left.operator, left.operands)
        else:
            if left_condition is True or right_condition is True:
                return Expression("true", 100, lines)
            if left_condition is False:
                return Expression(right.text, right.precedence, lines, right.operator, right.operands)
            if right_condition is False:
                return Expression(left.text, left.precedence, lines, left.operator, left.operands)
    return Expression(
        f"{wrap_binary_operand(left, op, precedence)} {op} "
        f"{wrap_binary_operand(right, op, precedence, right=True)}",
        precedence,
        lines,
        op,
        (left, right),
    )


def ternary_expr(
    condition: Expression,
    true_value: Expression,
    false_value: Expression,
) -> Expression:
    true_text = (
        f"({true_value.text})" if true_value.operator == "?:" else true_value.text
    )
    false_text = (
        f"({false_value.text})" if false_value.operator == "?:" else false_value.text
    )
    return Expression(
        f"{wrap(condition, 5)} ? {true_text} : {false_text}",
        5,
        merge_lines(
            condition.source_lines,
            true_value.source_lines,
            false_value.source_lines,
        ),
        "?:",
        (condition, true_value, false_value),
    )


def unary_expr(op: str, value: Expression) -> Expression:
    constant = constant_condition(value.text)
    if op == "!" and constant is not None:
        return Expression("false" if constant else "true", 100, value.source_lines)
    if op == "!" and value.operator == "!" and len(value.operands) == 1:
        return value.operands[0]
    if op == "!" and value.operator in {"==", "!=", "<", "<=", ">", ">="} and len(value.operands) == 2:
        inverse = {"==": "!=", "!=": "==", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}
        return binary_expr(value.operands[0], inverse[value.operator], value.operands[1], value.precedence)
    if op == "-" and value.operator == "-" and len(value.operands) == 1:
        return value.operands[0]
    return Expression(f"{op}{wrap(value, 80)}", 80, value.source_lines, op, (value,))


def constant_condition(text: str) -> bool | None:
    token = text.strip()
    while token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()
    if token == "true":
        return True
    if token == "false":
        return False
    if token.startswith("!") and not token.startswith("!="):
        inner = constant_condition(token[1:])
        return None if inner is None else not inner
    value = parse_int(token)
    if value is not None:
        return value != 0
    comparison = re.fullmatch(r"(-?(?:0[xX][0-9A-Fa-f]+|\d+))\s*(==|!=|<=|>=|<|>)\s*(-?(?:0[xX][0-9A-Fa-f]+|\d+))", token)
    if comparison:
        left = int(comparison.group(1), 0)
        right = int(comparison.group(3), 0)
        operator = comparison.group(2)
        return {
            "==": left == right,
            "!=": left != right,
            "<=": left <= right,
            ">=": left >= right,
            "<": left < right,
            ">": left > right,
        }[operator]
    return None


def call_expr(name: str, args: list[Expression], source_line: int | None = None) -> Expression:
    lines: tuple[int, ...] = ()
    for arg in args:
        lines = merge_lines(lines, arg.source_lines)
    if source_line is not None:
        lines = merge_lines(lines, source_line)
    result_type = {
        "streq": ValueType.I32,
        "bpd_strstr_index": ValueType.I32,
        "strlen": ValueType.I32,
    }.get(name, ValueType.UNKNOWN)
    return Expression(
        f"{name}({', '.join(arg.text for arg in args)})",
        100,
        lines,
        kind=ExpressionKind.CALL,
        operands=tuple(args),
        value_type=result_type,
        effects=Effect.CALL,
    )


def imm_expr(text: str, line_no: int) -> Expression:
    return Expression(c_int(text), 100, (line_no,))


class CallTargetMap(dict[int, str]):
    """Address-based call targets plus repairs for unanchored call sites."""

    def __init__(
        self,
        targets: dict[int, str],
        *,
        by_line: dict[int, str] | None = None,
    ) -> None:
        super().__init__(targets)
        self.by_line = by_line or {}


def _instruction_boundary_labels(module: AssemblyModule) -> set[str]:
    """Return labels that can safely become C control-flow targets.

    A relocatable BPD symbol normally names the instruction immediately after
    it.  ``#label-addend`` is the exception: its symbol value can point inside
    an instruction, where creating a C block or function would be incorrect.
    """
    result: set[str] = set()
    pending: list[AsmLabel] = []
    for item in module.items:
        if isinstance(item, AsmLabel):
            pending.append(item)
            continue
        result.update(label.name for label in pending if label.addend == 0)
        pending.clear()
    return result


def _repair_unanchored_instruction_addresses(module: AssemblyModule) -> None:
    """Recover addresses only after the parser lost them.

    Numeric labels are reliable synchronization points in BPD output.  Do
    not rebase already-known addresses, because a few legacy files contain
    labels whose printed offset differs from the parser's decoded offset by a
    byte or two.  This narrow repair is used for relative calls in regions
    that otherwise could never be named.
    """
    current: int | None = 0
    for item in module.items:
        if isinstance(item, AsmLabel):
            encoded = re.fullmatch(r"L(?P<address>[0-9A-Fa-f]+)", item.name)
            if encoded is not None and item.address is None:
                current = int(encoded.group("address"), 16)
            elif item.address is not None:
                current = item.address
            continue

        address = item.address
        if address is None and current is not None:
            address = current
            if item.opcode == "call_base_rel":
                item.address = address
        if address is None:
            current = None
            continue
        size = instruction_size(item.opcode, item.args, module.engine)
        current = address + size if size is not None else None


def direct_call_target_labels(module: AssemblyModule) -> CallTargetMap:
    direct_symbol_labels = _instruction_boundary_labels(module)
    labels_by_address: dict[int, list[str]] = {}
    encoded_labels_by_address: dict[int, list[str]] = {}
    instruction_addresses: set[int] = set()
    targets: set[int] = set()
    target_by_line: dict[int, int] = {}
    symbolic_target_by_line: dict[int, str] = {}
    _repair_unanchored_instruction_addresses(module)

    for item in module.items:
        if isinstance(item, AsmLabel):
            if item.address is not None:
                labels_by_address.setdefault(item.address, []).append(item.name)
            encoded = re.fullmatch(r"L(?P<address>[0-9A-Fa-f]+)", item.name)
            if encoded is not None:
                encoded_labels_by_address.setdefault(
                    int(encoded.group("address"), 16), []
                ).append(item.name)
            continue
        if item.opcode == "call_base_rel" and item.args:
            symbolic_target = item.args[0].strip()
            if symbolic_target in direct_symbol_labels:
                symbolic_target_by_line[item.line_no] = symbolic_target
                continue
        if item.address is None:
            continue
        instruction_addresses.add(item.address)
        if item.opcode == "call_base_rel" and item.args:
            relative = parse_int(item.args[0])
            if relative is not None:
                targets.add(item.address + relative)
                target_by_line[item.line_no] = item.address + relative

    result: dict[int, str] = {}
    for address in targets:
        labels = labels_by_address.get(address)
        encoded_labels = encoded_labels_by_address.get(address)
        if labels and address in instruction_addresses:
            result[address] = labels[-1]
        elif encoded_labels:
            result[address] = encoded_labels[-1]
        elif address in instruction_addresses and address == 0:
            result[address] = "entry"
        elif address in instruction_addresses:
            result[address] = f"BPD_F{address:05X}"

    # Labels retain their encoded VM offset even when an earlier size is
    # unknown or slightly mis-sized. A unique nearby encoded label is enough
    # to resolve the local function entry, but never invent a target for a
    # cross-module call that is farther away.
    for address in targets:
        if address in result:
            continue
        nearby = {
            name
            for delta in (-2, -1, 1, 2)
            for name in encoded_labels_by_address.get(address + delta, ())
        }
        if len(nearby) == 1:
            result[address] = next(iter(nearby))
    by_line = {
        line_no: result[target]
        for line_no, target in target_by_line.items()
        if target in result
    }
    by_line.update(symbolic_target_by_line)
    return CallTargetMap(result, by_line=by_line)


def direct_base_jump_target_labels(module: AssemblyModule) -> CallTargetMap:
    """Name direct control targets encoded by ``jump_rel16``.

    Opcode 0x13 replaces both execution offsets with ``instruction_ip +
    rel16``.  Targets often land on an instruction immediately before a
    printed label, so address-based synthetic labels are required to prevent
    the physical disassembly order from becoming false C fallthrough.
    """
    direct_symbol_labels = _instruction_boundary_labels(module)
    labels_by_address: dict[int, list[str]] = {}
    instruction_addresses: set[int] = set()
    targets: set[int] = set()
    target_by_line: dict[int, int] = {}
    symbolic_target_by_line: dict[int, str] = {}
    _repair_unanchored_instruction_addresses(module)

    for item in module.items:
        if isinstance(item, AsmLabel):
            if item.address is not None:
                labels_by_address.setdefault(item.address, []).append(item.name)
            encoded = re.fullmatch(r"L(?P<address>[0-9A-Fa-f]+)", item.name)
            if encoded is not None:
                labels_by_address.setdefault(
                    int(encoded.group("address"), 16), []
                ).append(item.name)
            continue
        if item.opcode == "jump_rel16" and item.args:
            symbolic_target = item.args[0].strip()
            if symbolic_target in direct_symbol_labels:
                symbolic_target_by_line[item.line_no] = symbolic_target
                continue
        if item.address is None:
            continue
        instruction_addresses.add(item.address)
        if item.opcode != "jump_rel16" or not item.args:
            continue
        relative = parse_int(item.args[0])
        if relative is None:
            continue
        target = item.address + relative
        targets.add(target)
        target_by_line[item.line_no] = target

    result: dict[int, str] = {}
    for address in targets:
        labels = labels_by_address.get(address)
        if labels:
            result[address] = labels[-1]
        elif address == 0 and address in instruction_addresses:
            result[address] = "entry"
        elif address in instruction_addresses:
            result[address] = f"BPD_J{address:05X}"

    by_line = {
        line_no: result[target]
        for line_no, target in target_by_line.items()
        if target in result
    }
    by_line.update(symbolic_target_by_line)
    return CallTargetMap(result, by_line=by_line)


CALL_OPS = {"call", "call_base_rel", "call_ptr"}
RESULT_STORE_OPS = {"store_base_typed"}
RESULT_POINTER_OPS = {
    "load_affine_imm",
    "load_affine_stack",
    "load_code_ref32",
    "load_base_typed",
    "load_typed_ptr",
    "push_base_offset",
    "push_byte",
    "push_dword",
    "push_word",
}


def _pointer_result_store_end(
    items: list[AsmInstruction | AsmLabel],
    start: int,
) -> int | None:
    """识别仅使用新压入地址的间接结果写入。"""
    depth = 0
    cursor = start
    while cursor < len(items):
        item = items[cursor]
        if isinstance(item, AsmLabel):
            return None
        if item.opcode == "store_ptr_typed":
            return cursor + 1 if depth == 1 else None

        if item.opcode in RESULT_POINTER_OPS:
            consumed, produced = 0, 1
        elif item.opcode in {"load", "load_base_scaled_add", "load_indexed_typed"}:
            consumed, produced = 1, 1
        elif item.opcode in BINARY_OPS:
            consumed, produced = 2, 1
        elif item.opcode in UNARY_OPS:
            consumed, produced = 1, 1
        else:
            return None
        if depth < consumed:
            return None
        depth += produced - consumed
        cursor += 1
    return None


def immediate_call_result_counts(module: AssemblyModule) -> dict[int, int]:
    """根据相邻写入精确推断调用结果数量。"""
    result: dict[int, int] = {}
    items = module.items
    for index, item in enumerate(items):
        if not isinstance(item, AsmInstruction) or (
            item.opcode not in CALL_OPS
            and get_opcode_semantics(item.opcode, module.engine) is None
        ):
            continue
        count = 0
        cursor = index + 1
        while cursor < len(items):
            current = items[cursor]
            if isinstance(current, AsmLabel):
                break
            if current.opcode in RESULT_STORE_OPS:
                count += 1
                cursor += 1
                continue
            if current.opcode == "store_multi" and current.args:
                values = parse_list(current.args[0])
                if not values:
                    break
                count += len(values)
                cursor += 1
                continue
            pointer_store_end = _pointer_result_store_end(items, cursor)
            if pointer_store_end is not None:
                count += 1
                cursor = pointer_store_end
                continue
            break
        if not count and cursor < len(items):
            consumer = items[cursor]
            semantics = get_opcode_semantics(item.opcode, module.engine)
            clears_input_stack = item.opcode in CALL_OPS or (
                semantics is not None and semantics.argument_count is None
            )
            if (
                isinstance(consumer, AsmInstruction)
                and consumer.opcode == "jc"
                and clears_input_stack
            ):
                count = 1
        # A BP call result can remain on the VM evaluation stack while the
        # next callee pointer is loaded.  In that form there is no explicit
        # store between the two calls, so the old pass treated the first
        # result as void and lost the implicit argument.  Recognize the
        # common ``call(); load_typed_ptr(...); call();`` chain.  The second
        # call consumes the first result as its prepared stack argument.
        if not count and cursor + 1 < len(items):
            first = items[cursor]
            second = items[cursor + 1]
            if (
                isinstance(first, AsmInstruction)
                and first.opcode == "load_typed_ptr"
                and isinstance(second, AsmInstruction)
                and second.opcode == "call"
            ):
                count = 1
        if count:
            result[item.line_no] = count
    return result


def partition_blocks(
    module: AssemblyModule,
    address_targets: dict[int, str] | None = None,
) -> list[tuple[str, int, list[AsmInstruction], bool]]:
    blocks: list[tuple[str, int, list[AsmInstruction], bool]] = []
    current_label: str | None = None
    current_line = 1
    current_instructions: list[AsmInstruction] = []
    current_synthetic = False

    def finish() -> None:
        nonlocal current_label, current_line, current_instructions, current_synthetic
        if current_label is not None:
            blocks.append((current_label, current_line, current_instructions, current_synthetic))
        current_label = None
        current_instructions = []
        current_synthetic = False

    for item in module.items:
        if isinstance(item, AsmLabel):
            finish()
            current_label = item.name
            current_line = item.line_no
        else:
            target_label = (address_targets or {}).get(item.address) if item.address is not None else None
            if target_label is not None and current_label != target_label:
                finish()
                current_label = target_label
                current_line = item.line_no
                current_synthetic = True
            if current_label is None:
                current_line = item.line_no
                current_label = "entry" if not blocks else f"BPD_L{item.line_no:05d}"
                current_synthetic = bool(blocks)
            current_instructions.append(item)
            if item.opcode in BLOCK_TERMINATORS:
                finish()

    finish()
    if not blocks:
        blocks.append(("entry", 1, [], False))
    return blocks


class BlockLifter:
    def __init__(
        self,
        block: BasicBlock,
        stats: LiftStatistics,
        initial_stack: list[Expression] | None = None,
        call_targets: dict[int, str] | None = None,
        call_targets_by_line: dict[int, str] | None = None,
        base_jump_targets: dict[int, str] | None = None,
        base_jump_targets_by_line: dict[int, str] | None = None,
        call_result_counts_by_line: dict[int, int] | None = None,
        direct_call_argument_counts: dict[str, int] | None = None,
        direct_call_result_counts: dict[str, int] | None = None,
        dynamic_call_hints_by_line: dict[int, tuple[int, int]] | None = None,
    ) -> None:
        self.block = block
        self.stats = stats
        self.stack: list[Expression] = list(initial_stack or [])
        self.call_targets = call_targets or {}
        self.call_targets_by_line = call_targets_by_line or {}
        self.base_jump_targets = base_jump_targets or {}
        self.base_jump_targets_by_line = base_jump_targets_by_line or {}
        self.call_result_counts_by_line = call_result_counts_by_line or {}
        self.direct_call_argument_counts = direct_call_argument_counts or {}
        self.direct_call_result_counts = direct_call_result_counts or {}
        self.dynamic_call_hints_by_line = dynamic_call_hints_by_line or {}

    def pop(self) -> Expression:
        if self.stack:
            return self.stack.pop()
        return Expression("vm_pop(vm)", 100)

    def push(self, expr: Expression) -> None:
        self.stack.append(expr)

    def materialize_pending_call_values(self) -> None:
        """Evaluate deferred call results before a later observable statement.

        Scalar call results normally stay as expressions until their first
        consumer, which produces compact assignments.  If such a value remains
        below unrelated arguments, however, moving its call past a later store
        or call changes VM side-effect order.  Spill only those pending call
        expressions and keep their pure result names on the simulated stack.
        """
        if not any(value.effects & Effect.CALL for value in self.stack):
            return
        occupied = "\n".join(statement.text for statement in self.block.statements)
        for index, value in enumerate(self.stack):
            if not value.effects & Effect.CALL:
                continue
            source_line = max(value.source_lines, default=self.block.source_line)
            base = f"result_L{source_line}"
            name = base
            suffix = 0
            while re.search(rf"\b{re.escape(name)}\b", occupied):
                name = f"{base}_{suffix}"
                suffix += 1
            self.block.statements.append(
                Statement(
                    "assign",
                    f"{name} = {value.text};",
                    value.source_lines,
                    operands=(value,),
                )
            )
            occupied += f"\n{name}"
            self.stack[index] = Expression(
                name,
                100,
                value.source_lines,
                value_type=value.value_type,
            )

    def statement(
        self,
        kind: str,
        text: str,
        instr: AsmInstruction,
        *exprs: Expression,
        target: str | None = None,
        condition: str | None = None,
    ) -> None:
        if kind not in {"state", "trace"}:
            self.materialize_pending_call_values()
        lines: tuple[int, ...] = ()
        for expr in exprs:
            lines = merge_lines(lines, expr.source_lines)
        lines = merge_lines(lines, instr.line_no)
        self.block.statements.append(
            Statement(
                kind,
                text,
                lines,
                target,
                condition,
                operands=tuple(exprs),
            )
        )

    def sync_stack(self) -> None:
        if not self.stack:
            return
        for expr in self.stack:
            self.block.statements.append(
                Statement("stack", f"vm_push(vm, {expr.text});", expr.source_lines)
            )
            self.stats.stack_sync_count += 1
        self.stack.clear()

    def opaque(self, instr: AsmInstruction) -> None:
        stack_args = list(self.stack)
        self.stack.clear()
        args = [expr.text for expr in stack_args]
        args.extend(format_raw_arg(arg) for arg in instr.args)
        self.statement("opaque", f"{instr.opcode}({', '.join(args)});", instr, *stack_args)
        self.stats.opaque_count += 1

    def call(
        self,
        instr: AsmInstruction,
        text: str,
        *args: Expression,
        result_count: int | None = None,
    ) -> None:
        if result_count is None:
            result_count = self.call_result_counts_by_line.get(instr.line_no, 0)
        expression = text[:-1] if text.endswith(";") else text
        if result_count == 1:
            self.push(Expression(expression, 100, merge_lines(*(arg.source_lines for arg in args), instr.line_no)))
            return
        if result_count > 1:
            result_slots = multi_result_slot_names(instr.line_no, result_count)
            self.statement(
                "effect",
                f"values({', '.join(result_slots)}) = {expression};",
                instr,
                *args,
            )
            for result_slot in result_slots:
                self.push(Expression(result_slot, 100, (instr.line_no,)))
            return
        self.statement("call", text, instr, *args)

    def branch(self, instr: AsmInstruction, condition: Expression, target: str) -> None:
        self.sync_stack()
        constant = constant_condition(condition.text)
        if constant is True:
            self.statement("jump", f"goto {target};", instr, condition, target=target, condition="true")
        elif constant is False:
            self.statement("trace", f"/* 到 {target} 的分支恒不成立 */", instr, condition)
        else:
            self.statement(
                "branch",
                f"if ({condition.text}) goto {target};",
                instr,
                condition,
                target=target,
                condition=condition.text,
            )

    def lift_push(self, instr: AsmInstruction) -> bool:
        op = instr.opcode
        args = instr.args
        if op in {"push_byte", "push_word", "push_dword"} and args:
            self.push(imm_expr(args[0], instr.line_no))
            return True
        if op == "push_string" and args:
            self.push(Expression(c_string(args[0]), 100, (instr.line_no,)))
            return True
        if op == "push_offset" and args:
            self.push(Expression(f"label_ref({args[0].strip()})", 100, (instr.line_no,)))
            return True
        if op == "push_base_offset" and args:
            self.push(Expression(frame_address_name(args[0]), 100, (instr.line_no,)))
            return True
        if op == "pushx" and len(args) >= 2:
            values = parse_list(args[1])
            if values:
                for value in values:
                    self.push(imm_expr(value, instr.line_no))
                return True
            self.push(imm_expr(args[1], instr.line_no))
            return True
        return False

    def lift_load(self, instr: AsmInstruction) -> bool:
        op, args = instr.opcode, instr.args
        if op == "load" and args:
            address = self.pop()
            encoded_type = parse_int(args[0])
            type_id = (encoded_type or 0) & 3
            direct = direct_load_text(address.text, type_id) if encoded_type is not None else None
            if direct is not None:
                self.push(Expression(direct, 100, merge_lines(address.source_lines, instr.line_no)))
            else:
                self.push(call_expr("load_typed", [imm_expr(args[0], instr.line_no), address], instr.line_no))
            return True
        if op == "load_typed_ptr" and args:
            encoded = parse_int(args[0])
            if get_profile().historical and encoded is not None:
                address = imm_expr(hex(encoded & 0x3fffffff), instr.line_no)
                width = (encoded >> 30) & 3
                direct = direct_load_text(address.text, width)
                self.push(Expression(direct, 100, (instr.line_no,)) if direct is not None else
                          call_expr('load_typed', [imm_expr(str(width), instr.line_no), address], instr.line_no))
                return True
            self.push(Expression(global_ref_name(args[0]), 100, (instr.line_no,)))
            return True
        if op in {"load_base_typed", "load_code_ref32"} and args:
            self.push(Expression(code_ref_name(args[0]), 100, (instr.line_no,)))
            return True
        if op in {"load_base_typed_add", "load_code_ref_add_imm"} and len(args) >= 2:
            base = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            self.push(binary_expr(base, "+", imm_expr(args[1], instr.line_no), 60))
            return True
        if op == "shift_base_typed" and len(args) >= 2:
            value = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            amount = parse_int(args[1])
            if amount is None:
                self.push(call_expr("shift_value", [value, imm_expr(args[1], instr.line_no)], instr.line_no))
            else:
                symbol = "<<" if amount >= 0 else ">>"
                self.push(binary_expr(value, symbol, Expression(str(abs(amount)), 100), 50))
            return True
        if op == "load_code_ref_indexed_typed" and len(args) >= 3:
            self.push(Expression(
                f"load_indexed({code_ref_name(args[0])}, {c_int(args[1])}, {c_int(args[2])})",
                100,
                (instr.line_no,),
            ))
            return True
        if op == "load_code_ref_scaled_add" and len(args) >= 2:
            base = self.pop()
            index = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            scaled = binary_expr(index, "*", imm_expr(args[1], instr.line_no), 70)
            self.push(binary_expr(base, "+", scaled, 60))
            return True
        if op == "load_ptr_offset_typed" and args:
            base = self.pop()
            encoded_offset = parse_int(args[0])
            if encoded_offset is None:
                self.push(call_expr("load_ptr_offset", [base, imm_expr(args[0], instr.line_no)], instr.line_no))
                return True
            type_id = encoded_offset & 3
            byte_offset = encoded_offset >> 2
            field = recover_structure_field_load(base.text, byte_offset, type_id)
            if field is not None:
                self.push(Expression(field, 100, merge_lines(base.source_lines, instr.line_no)))
                return True
            address = binary_expr(base, "+", Expression(str(byte_offset), 100, (instr.line_no,)), 60)
            direct = direct_load_text(address.text, type_id)
            if direct is not None:
                self.push(Expression(direct, 100, merge_lines(address.source_lines, instr.line_no)))
                return True
            loader = {0: "load_i8", 1: "load_i16", 2: "load_i32"}.get(type_id, "load_i8")
            self.push(call_expr(loader, [address], instr.line_no))
            return True
        if op == "load_multi" and args:
            for offset in parse_list(args[0]):
                self.push(Expression(code_ref_name(offset), 100, (instr.line_no,)))
            return True
        if op == "load_affine_imm" and len(args) >= 3:
            base = imm_expr(args[0], instr.line_no)
            index = Expression(code_ref_name(args[1]), 100, (instr.line_no,))
            scaled = binary_expr(index, "*", imm_expr(args[2], instr.line_no), 70)
            self.push(binary_expr(base, "+", scaled, 60))
            return True
        if op == "load_affine_rel" and len(args) >= 3:
            base = self.pop()
            indexed_base = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            address = binary_expr(indexed_base, "+", imm_expr(args[1], instr.line_no), 60)
            type_id = code_ref_type_id(args[0])
            loader = {0: "load_i8", 1: "load_i16", 2: "load_i32", 3: "load_i64"}[type_id]
            index = call_expr(loader, [address], instr.line_no)
            scaled = binary_expr(index, "*", imm_expr(args[2], instr.line_no), 70)
            self.push(binary_expr(base, "+", scaled, 60))
            return True
        if op == "load_affine_stack" and len(args) >= 4:
            base = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            base = binary_expr(base, "+", imm_expr(args[1], instr.line_no), 60)
            index = Expression(code_ref_name(args[2]), 100, (instr.line_no,))
            scaled = binary_expr(index, "*", imm_expr(args[3], instr.line_no), 70)
            self.push(binary_expr(base, "+", scaled, 60))
            return True
        if op == "load_base_scaled_add" and len(args) >= 2:
            # This opcode computes an address from the current stack base and
            # a frame-relative index.  Keep it as an expression so a following
            # load/store can be rendered as normal pointer arithmetic.
            base = self.pop()
            index = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            scaled = binary_expr(index, "*", imm_expr(args[1], instr.line_no), 70)
            self.push(binary_expr(base, "+", scaled, 60))
            return True
        if op == "load_rel_add" and len(args) >= 3:
            encoded_type = parse_int(args[1])
            base = parse_int(args[0])
            offset = parse_int(args[2])
            if encoded_type is not None and base is not None and offset is not None:
                type_id = encoded_type & 3
                # The code-ref slot contains a BP VM address.  This opcode
                # dereferences that value plus the immediate byte offset; it
                # does not address the frame slot itself.
                address = code_ref_name(args[0])
                if offset:
                    sign = "+" if offset > 0 else "-"
                    address = f"{address} {sign} {abs(offset)}"
                direct = direct_load_text(address, type_id)
                if direct is not None:
                    self.push(Expression(direct, 100, (instr.line_no,)))
                else:
                    loader = {0: "load_i8", 1: "load_i16", 2: "load_i32", 3: "load_i64"}[type_id]
                    self.push(call_expr(loader, [Expression(address, 100, (instr.line_no,))], instr.line_no))
                return True
        if op == "load_ptr_indexed" and len(args) >= 3:
            encoded_ref = parse_int(args[1])
            if encoded_ref is not None:
                type_id = (encoded_ref >> 14) & 3
                index = Expression(code_ref_name(str(encoded_ref & 0x3FFF)), 100, (instr.line_no,))
                base = imm_expr(args[0], instr.line_no)
                scaled = binary_expr(index, "*", imm_expr(args[2], instr.line_no), 70)
                address = binary_expr(base, "+", scaled, 60)
                field = recover_structure_field_load(address.text, 0, type_id)
                if field is not None:
                    self.push(Expression(field, 100, merge_lines(address.source_lines, instr.line_no)))
                    return True
                direct = direct_load_text(address.text, type_id)
                if direct is not None:
                    self.push(Expression(direct, 100, merge_lines(address.source_lines, instr.line_no)))
                    return True
                loader = {0: "load_i8", 1: "load_i16", 2: "load_i32"}.get(type_id, "load_i8")
                self.push(call_expr(loader, [address], instr.line_no))
                return True
        if op == "load_indexed_typed" and len(args) >= 3:
            base = self.pop()
            index = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            scaled = binary_expr(index, "*", imm_expr(args[2], instr.line_no), 70)
            address = binary_expr(base, "+", scaled, 60)
            type_id = (parse_int(args[1]) or 0) & 3
            loader = {0: "load_i8", 1: "load_i16", 2: "load_i32", 3: "load_i64"}[type_id]
            self.push(call_expr(loader, [address], instr.line_no))
            return True
        if op in {"load_rel_add", "load_ptr_indexed", "load_indirect_indexed"}:
            rendered = ", ".join(format_raw_arg(arg) for arg in args)
            self.push(Expression(f"{op}({rendered})", 100, (instr.line_no,)))
            return True
        return False

    def lift_store(self, instr: AsmInstruction) -> bool:
        op, args = instr.opcode, instr.args
        if op == "store_inline" and len(args) == 1:
            from ..asdis.bp.bpop import _inline_payload
            raw = _inline_payload(args)
            target = self.pop()
            literal = '"' + ''.join('\\x%02x' % byte for byte in raw) + '"'
            self.statement("store", f"memcpy({target.text}, {literal}, {len(raw)});", instr, target)
            return True
        if op in {"move", "store_keep"} and args:
            value, target = self.pop(), self.pop()
            if op == 'store_keep' and parse_int(value.text) is None:
                saved = f'stored_L{instr.line_no}'
                self.statement('assign', f'{saved} = {value.text};', instr, value)
                value = Expression(saved, 100, value.source_lines)
            self.statement("store", store_text(target, value, args[0]), instr, target, value)
            if op == 'store_keep':
                self.push(value)
            return True
        if op == "store_ptr_typed" and args:
            target, value = self.pop(), self.pop()
            self.statement("store", store_text(target, value, args[0]), instr, target, value)
            return True
        if op == "store_typed_imm" and len(args) >= 2:
            target = self.pop()
            value = imm_expr(args[1], instr.line_no)
            self.statement("store", store_text(target, value, args[0]), instr, target, value)
            return True
        if op == "cmp_store" and args:
            right, left, target = self.pop(), self.pop(), self.pop()
            value = store_operation_expr(left, right, args[0], instr.line_no)
            type_id = str(((parse_int(args[0]) or 0) >> 6) & 3)
            self.statement("store", store_text(target, value, type_id), instr, target, left, right)
            return True
        if op == "cmp_store_imm" and len(args) >= 2:
            left, target = self.pop(), self.pop()
            value = store_operation_expr(left, imm_expr(args[1], instr.line_no), args[0], instr.line_no)
            type_id = str(((parse_int(args[0]) or 0) >> 6) & 3)
            self.statement("store", store_text(target, value, type_id), instr, target, left)
            return True
        if op == "store_base_typed" and args:
            value = self.pop()
            self.statement("store", f"{code_ref_name(args[0])} = {value.text};", instr, value)
            return True
        if op == "store_rel_typed_imm" and len(args) >= 3:
            self.statement("store", f"{code_ref_name(args[0])} = {c_int(args[2])};", instr)
            return True
        if op == "store_code_ref_indexed_typed" and len(args) >= 4:
            value = self.pop()
            rendered = ", ".join(c_int(arg) for arg in args[:4])
            self.statement("store", f"code_ref_indexed_store({rendered}, {value.text});", instr, value)
            return True
        if op == "store_indexed_value" and len(args) >= 3:
            base_value, target = self.pop(), self.pop()
            indexed_value = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            scaled = binary_expr(indexed_value, "*", imm_expr(args[2], instr.line_no), 70)
            value = binary_expr(base_value, "+", scaled, 60)
            self.statement("store", store_text(target, value, args[1]), instr, target, base_value)
            return True
        if op == "store_multi" and args:
            offsets = parse_list(args[0])
            values = [self.pop() for _ in offsets]
            for offset, value in zip(offsets, values):
                self.statement("store", f"{code_ref_name(offset)} = {value.text};", instr, value)
            return True
        if op == "copystack" and len(args) >= 2:
            count = parse_int(args[1])
            if count is None or count < 0:
                return False
            values = [self.pop() for _ in range(count)]
            target = self.pop()
            rendered = ", ".join(
                [target.text]
                + [value.text for value in reversed(values)]
                + [c_int(args[0]), c_int(args[1])]
            )
            self.statement("effect", f"copystack({rendered});", instr, target, *values)
            return True
        if op == "store_rel_add32" and len(args) >= 3:
            source = Expression(code_ref_name(args[1]), 100, (instr.line_no,))
            value = binary_expr(source, "+", imm_expr(args[2], instr.line_no), 60)
            self.statement("store", f"{code_ref_name(args[0])} = {value.text};", instr, value)
            return True
        if op == "store_rel_add_value" and len(args) >= 3:
            target = self.pop()
            source = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            value = binary_expr(source, "+", imm_expr(args[2], instr.line_no), 60)
            self.statement("store", store_text(target, value, args[1]), instr, target, value)
            return True
        if op == "store_affine_imm" and len(args) >= 4:
            source = Expression(code_ref_name(args[2]), 100, (instr.line_no,))
            scaled = binary_expr(source, "*", imm_expr(args[3], instr.line_no), 70)
            value = binary_expr(imm_expr(args[1], instr.line_no), "+", scaled, 60)
            self.statement("store", f"{code_ref_name(args[0])} = {value.text};", instr, value)
            return True
        if op == "store_affine_rel" and len(args) >= 5:
            base = Expression(code_ref_name(args[1]), 100, (instr.line_no,))
            source = Expression(code_ref_name(args[3]), 100, (instr.line_no,))
            scaled = binary_expr(source, "*", imm_expr(args[4], instr.line_no), 70)
            value = binary_expr(binary_expr(base, "+", imm_expr(args[2], instr.line_no), 60), "+", scaled, 60)
            self.statement("store", f"{code_ref_name(args[0])} = {value.text};", instr, value)
            return True
        if op == "store_indirect_imm" and len(args) >= 3:
            base = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            target = binary_expr(base, "+", imm_expr(args[1], instr.line_no), 60)
            value = imm_expr(args[2], instr.line_no)
            self.statement(
                "store",
                store_text(target, value, str(code_ref_type_id(args[0]))),
                instr,
                target,
                value,
            )
            return True
        if op == "store_indirect_add32" and len(args) >= 4:
            base = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            target = binary_expr(base, "+", imm_expr(args[1], instr.line_no), 60)
            source = Expression(code_ref_name(args[2]), 100, (instr.line_no,))
            value = binary_expr(source, "+", imm_expr(args[3], instr.line_no), 60)
            self.statement(
                "store",
                store_text(target, value, str(code_ref_type_id(args[0]))),
                instr,
                target,
                value,
            )
            return True
        if op == "store_ptr_add" and len(args) >= 3:
            target = imm_expr(args[0], instr.line_no)
            source = Expression(code_ref_name(args[1]), 100, (instr.line_no,))
            value = binary_expr(source, "+", imm_expr(args[2], instr.line_no), 60)
            self.statement(
                "store",
                store_text(target, value, str(code_ref_type_id(args[1]))),
                instr,
                target,
                value,
            )
            return True
        if op == "store_indirect" and len(args) >= 2:
            value = self.pop()
            base = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            target = binary_expr(base, "+", imm_expr(args[1], instr.line_no), 60)
            self.statement(
                "store",
                store_text(target, value, str(code_ref_type_id(args[0]))),
                instr,
                target,
                value,
            )
            return True
        if op == "select_store" and args:
            false_value, true_value, condition = self.pop(), self.pop(), self.pop()
            selected = ternary_expr(condition, true_value, false_value)
            self.statement("store", f"{code_ref_name(args[0])} = {selected.text};", instr, selected)
            return True
        return False

    def lift_arithmetic(self, instr: AsmInstruction) -> bool:
        op, args = instr.opcode, instr.args
        if op in BINARY_OPS:
            right, left = self.pop(), self.pop()
            symbol, precedence = BINARY_OPS[op]
            self.push(binary_expr(left, symbol, right, precedence))
            return True
        if op in UNARY_OPS:
            self.push(unary_expr(UNARY_OPS[op], self.pop()))
            return True
        if op in {"add_imm", "mul_imm", "div_imm"} and args:
            symbol, precedence = {"add_imm": ("+", 60), "mul_imm": ("*", 70), "div_imm": ("/", 70)}[op]
            self.push(binary_expr(self.pop(), symbol, imm_expr(args[0], instr.line_no), precedence))
            return True
        if op == "mad_imm" and args:
            right, left = self.pop(), self.pop()
            product = binary_expr(imm_expr(args[0], instr.line_no), "*", right, 70)
            self.push(binary_expr(left, "+", product, 60))
            return True
        if op == "shift_imm" and args:
            amount = parse_int(args[0])
            value = self.pop()
            if amount is not None:
                symbol = "<<" if amount >= 0 else ">>"
                self.push(binary_expr(value, symbol, Expression(str(abs(amount)), 100, (instr.line_no,)), 50))
            else:
                self.push(call_expr("vm_shift", [value, imm_expr(args[0], instr.line_no)], instr.line_no))
            return True
        if op in {"mul_base_typed", "div_base_typed"} and args:
            left = self.pop()
            right = Expression(code_ref_name(args[0]), 100, (instr.line_no,))
            symbol = "*" if op == "mul_base_typed" else "/"
            self.push(binary_expr(left, symbol, right, 70))
            return True
        if op == "cmp_imm" and len(args) >= 2:
            self.push(flag_expr(self.pop(), imm_expr(args[1], instr.line_no), args[0], instr.line_no))
            return True
        if op == "cmp_code_ref_imm" and len(args) >= 3:
            value = Expression(code_ref_name(args[1]), 100, (instr.line_no,))
            self.push(flag_expr(value, imm_expr(args[2], instr.line_no), args[0], instr.line_no))
            return True
        if op == "cmp_code_ref_indexed_imm" and len(args) >= 5:
            value = Expression(
                f"load_indexed({code_ref_name(args[1])}, {c_int(args[2])}, {c_int(args[3])})",
                100,
                (instr.line_no,),
            )
            self.push(flag_expr(value, imm_expr(args[4], instr.line_no), args[0], instr.line_no))
            return True
        if op == "ternary":
            false_value, true_value, condition = self.pop(), self.pop(), self.pop()
            self.push(ternary_expr(condition, true_value, false_value))
            return True
        if op in {"strlen", "sin", "cos"}:
            self.push(call_expr(op, [self.pop()], instr.line_no))
            return True
        if op in {"streq", "strstr"}:
            right, left = self.pop(), self.pop()
            left = Expression(recover_structure_string_address(left.text), left.precedence, left.source_lines)
            right = Expression(recover_structure_string_address(right.text), right.precedence, right.source_lines)
            name = "bpd_strstr_index" if op == "strstr" else op
            self.push(call_expr(name, [left, right], instr.line_no))
            return True
        if op == "concat":
            right, left, target = self.pop(), self.pop(), self.pop()
            self.statement("effect", f"concat({target.text}, {left.text}, {right.text});", instr, target, left, right)
            return True
        if op == "strreplace":
            replacement, pattern, source, target = self.pop(), self.pop(), self.pop(), self.pop()
            self.push(call_expr("strreplace", [target, source, pattern, replacement], instr.line_no))
            return True
        if op == "memcmp":
            length, right, left = self.pop(), self.pop(), self.pop()
            self.push(call_expr("memory_equals", [left, right, length], instr.line_no))
            return True
        if op == "sprintf":
            values = list(self.stack)
            self.stack.clear()
            if len(values) >= 2:
                # The fixed target/format operands are pushed last, while the
                # variadic values are consumed from the VM stack top first.
                # Render those values in pop order so they line up with the
                # format string's left-to-right conversion specifiers.
                extras = list(reversed(values[:-2]))
                target, format_string = values[-2], values[-1]
                call_args = [target, format_string] + extras
            else:
                call_args = values
            self.statement("effect", f"sprintf({', '.join(arg.text for arg in call_args)});", instr, *call_args)
            return True
        if op == "strlower_inplace":
            value = self.pop()
            self.statement("effect", f"strlower_inplace({value.text});", instr, value)
            return True
        if op in {"message", "confirm", "getgamestate"}:
            value = self.pop()
            self.push(call_expr(op, [value], instr.line_no))
            return True
        if op in {"memcpy", "memclr", "memset", "strcpy"}:
            arity = {"memcpy": 3, "memclr": 2, "memset": 3, "strcpy": 2}[op]
            popped = [self.pop() for _ in range(arity)]
            call_args = list(reversed(popped))
            if op == "strcpy":
                call_args = [
                    Expression(recover_structure_string_address(arg.text), arg.precedence, arg.source_lines)
                    for arg in call_args
                ]
            self.statement("effect", f"{op}({', '.join(arg.text for arg in call_args)});", instr, *call_args)
            return True
        if op == "memcpy_stack" and args:
            source, target = self.pop(), self.pop()
            byte_count = parse_int(args[0])
            size = c_int(args[0]) if byte_count is None else recovered_copy_size(target.text, byte_count)
            self.statement("effect", f"memcpy({target.text}, {source.text}, {size});", instr, target, source)
            return True
        if op == "memcpy_rel_stack" and len(args) >= 2:
            target = self.pop()
            source = Expression(code_address_name(args[0]), 100, (instr.line_no,))
            byte_count = parse_int(args[1])
            size = c_int(args[1]) if byte_count is None else recovered_copy_size(target.text, byte_count)
            self.statement("effect", f"memcpy({target.text}, {source.text}, {size});", instr, target, source)
            return True
        if op == "memcpy_repeat":
            values = [self.pop() for _ in range(4)]
            call_args = list(reversed(values))
            self.statement(
                "effect",
                f"memcpy_repeat({', '.join(value.text for value in call_args)});",
                instr,
                *call_args,
            )
            return True
        if op == "normalizevec":
            values = [self.pop() for _ in range(2)]
            call_args = list(reversed(values))
            self.statement(
                "effect",
                f"normalizevec({', '.join(value.text for value in call_args)});",
                instr,
                *call_args,
            )
            return True
        if op in {"assert_number", "setclipboardtext"}:
            value = self.pop()
            self.statement("effect", f"{op}({value.text});", instr, value)
            return True
        if op == "pop_discard":
            value = self.pop()
            self.statement("effect", f"(void)({value.text});", instr, value)
            return True
        return False

    def lift_control(self, instr: AsmInstruction) -> bool:
        op, args = instr.opcode, instr.args
        if op in {"jc", "jc_legacy"} and args:
            flag = parse_int(args[0])
            target_expr: Expression | None = None
            if op == 'jc' and len(args) >= 2 and (flag is None or flag & 8):
                target = args[1].strip()
            else:
                target_expr = self.pop()
                target_match = LABEL_REF_RE.fullmatch(target_expr.text)
                target = target_match.group("label") if target_match is not None else ""
            condition = self.pop()
            if target_expr is not None:
                condition.source_lines = merge_lines(condition.source_lines, target_expr.source_lines)
            if op == 'jc_legacy' and flag is not None and flag not in range(6):
                condition = call_expr('jc_reserved_predicate', [imm_expr(args[0], instr.line_no), condition], instr.line_no)
            else:
                condition = jc_condition_expr(condition, args[0], instr.line_no)
            if target:
                self.branch(instr, condition, target)
            else:
                self.sync_stack()
                self.statement(
                    "branch_dynamic",
                    f"if ({condition.text}) jump_to({target_expr.text});",
                    instr,
                    condition,
                    target_expr,
                    condition=condition.text,
                )
            return True
        if op == "jcmp" and len(args) >= 2:
            right, left = self.pop(), self.pop()
            condition = jump_flag_expr(left, right, args[0], instr.line_no)
            target = args[1].strip()
            self.branch(instr, condition, target)
            return True
        if op == "cmp_imm_jc" and len(args) >= 3:
            left = self.pop()
            condition = jump_flag_expr(left, imm_expr(args[2], instr.line_no), args[0], instr.line_no)
            target = args[1].strip()
            self.branch(instr, condition, target)
            return True
        if op == "jc_cmp_code_ref_imm" and len(args) >= 4:
            left = Expression(code_ref_name(args[2]), 100, (instr.line_no,))
            condition = jump_flag_expr(left, imm_expr(args[3], instr.line_no), args[0], instr.line_no)
            target = args[1].strip()
            self.branch(instr, condition, target)
            return True
        if op == "jc_cmp_code_ref_indexed_imm" and len(args) >= 6:
            left = Expression(
                f"load_indexed({code_ref_name(args[2])}, {c_int(args[3])}, {c_int(args[4])})",
                100,
                (instr.line_no,),
            )
            condition = jump_flag_expr(left, imm_expr(args[5], instr.line_no), args[0], instr.line_no)
            target = args[1].strip()
            self.branch(instr, condition, target)
            return True
        if op in {"jc_test_code_ref", "jc_test_code_ref_indexed"} and args:
            target = args[0].strip()
            if op == "jc_test_code_ref" and len(args) >= 2:
                condition = Expression(code_ref_name(args[1]), 100, (instr.line_no,))
            elif len(args) >= 4:
                condition = Expression(
                    f"load_indexed({code_ref_name(args[1])}, {c_int(args[2])}, {c_int(args[3])})",
                    100,
                    (instr.line_no,),
                )
            else:
                rendered = ", ".join(c_int(arg) for arg in args[1:])
                condition = Expression(f"{op}_condition(vm{', ' if rendered else ''}{rendered})", 100, (instr.line_no,))
            self.branch(instr, condition, target)
            return True
        if op == "jmp":
            target_expr = self.pop()
            match = LABEL_REF_RE.fullmatch(target_expr.text)
            self.sync_stack()
            if match:
                target = match.group("label")
                self.statement("jump", f"goto {target};", instr, target_expr, target=target)
            else:
                self.statement("jump_dynamic", f"vm_jump(vm, {target_expr.text});", instr, target_expr)
                self.block.statements[-1].control_target = IndirectTarget(target_expr.text)
            return True
        if op == "call":
            target_expr = self.pop()
            target_match = LABEL_REF_RE.fullmatch(target_expr.text)
            if target_match:
                target = target_match.group("label")
                argument_count = self.direct_call_argument_counts.get(target)
                if argument_count is None:
                    call_args = list(self.stack)
                    self.stack.clear()
                else:
                    take = min(len(self.stack), argument_count)
                    call_args = self.stack[-take:] if take else []
                    if take:
                        del self.stack[-take:]
                    missing = argument_count - take
                    if missing:
                        call_args = [
                            Expression(f"vm_arg(vm, {index})", 100)
                            for index in range(missing)
                        ] + call_args
                rendered = ", ".join(arg.text for arg in call_args)
                separator = ", " if rendered else ""
                self.call(
                    instr,
                    f"call_direct(label_ref({target}){separator}{rendered});",
                    target_expr,
                    *call_args,
                    result_count=self.direct_call_result_counts.get(target),
                )
            else:
                hint = self.dynamic_call_hints_by_line.get(instr.line_no)
                if hint is None:
                    call_args = list(self.stack)
                    self.stack.clear()
                    result_count = None
                else:
                    argument_count, result_count = hint
                    take = min(len(self.stack), argument_count)
                    call_args = self.stack[-take:] if take else []
                    if take:
                        del self.stack[-take:]
                    missing = argument_count - take
                    if missing:
                        call_args = [
                            Expression(f"vm_arg(vm, {index})", 100)
                            for index in range(missing)
                        ] + call_args
                rendered = ", ".join(arg.text for arg in call_args)
                callee = target_expr.text if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_expr.text) else f"({target_expr.text})"
                self.call(
                    instr,
                    f"{callee}({rendered});",
                    target_expr,
                    *call_args,
                    result_count=result_count,
                )
            return True
        if op == "call_base_rel" and args:
            relative = parse_int(args[0])
            target = None if instr.address is None or relative is None else self.call_targets.get(instr.address + relative)
            if target is None:
                target = self.call_targets_by_line.get(instr.line_no)
            if target is not None:
                argument_count = self.direct_call_argument_counts.get(target)
                if argument_count is None:
                    call_args = list(self.stack)
                    self.stack.clear()
                else:
                    take = min(len(self.stack), argument_count)
                    call_args = self.stack[-take:] if take else []
                    if take:
                        del self.stack[-take:]
                    missing = argument_count - take
                    if missing:
                        call_args = [
                            Expression(f"vm_arg(vm, {index})", 100)
                            for index in range(missing)
                        ] + call_args
                rendered = ", ".join(arg.text for arg in call_args)
                separator = ", " if rendered else ""
                self.call(
                    instr,
                    f"call_direct(label_ref({target}){separator}{rendered});",
                    *call_args,
                    result_count=self.direct_call_result_counts.get(target),
                )
            else:
                call_args = list(self.stack)
                self.stack.clear()
                rendered = ", ".join([c_int(args[0])] + [arg.text for arg in call_args])
                self.call(instr, f"call_relative({rendered});", *call_args)
            return True
        if op == "call_ptr" and args:
            hint = self.dynamic_call_hints_by_line.get(instr.line_no)
            if hint is None:
                call_args = list(self.stack)
                self.stack.clear()
                result_count = None
            else:
                argument_count, result_count = hint
                take = min(len(self.stack), argument_count)
                call_args = self.stack[-take:] if take else []
                if take:
                    del self.stack[-take:]
                missing = argument_count - take
                if missing:
                    call_args = [
                        Expression(f"vm_arg(vm, {index})", 100)
                        for index in range(missing)
                    ] + call_args
            rendered = ", ".join([c_int(args[0])] + [arg.text for arg in call_args])
            self.call(
                instr,
                f"call_pointer({rendered});",
                *call_args,
                result_count=result_count,
            )
            return True
        if op == "ret":
            return_values = list(self.stack)
            self.stack.clear()
            if not return_values:
                text = "return;"
            elif len(return_values) == 1:
                text = f"return {return_values[0].text};"
            else:
                text = f"return values({', '.join(value.text for value in return_values)});"
            self.statement("return", text, instr, *return_values)
            return True
        return False

    def lift_semantic_opcode(self, instr: AsmInstruction) -> bool:
        semantics = get_opcode_semantics(instr.opcode)
        if semantics is None:
            return False

        if semantics.argument_count is None:
            call_args = list(self.stack)
            self.stack.clear()
        else:
            take = min(len(self.stack), semantics.argument_count)
            call_args = self.stack[-take:] if take else []
            if take:
                del self.stack[-take:]
            missing = semantics.argument_count - take
            if missing:
                call_args = [Expression(f"vm_arg(vm, {index})", 100) for index in range(missing)] + call_args

        if not semantics.is_group:
            call_args.extend(
                Expression(format_raw_arg(arg), 100, (instr.line_no,))
                for arg in instr.args
            )

        call = call_expr(instr.opcode, call_args, instr.line_no)
        # Adjacent stores can consume values prepared before this opcode.
        # They cannot increase an independently verified VM result count.
        result_count = semantics.result_count
        if not semantics.result_count_verified:
            result_count = max(result_count, self.call_result_counts_by_line.get(instr.line_no, 0))
        if result_count == 0:
            self.statement("effect", f"{call.text};", instr, *call_args)
            return True
        if result_count == 1:
            constant = CONSTANT_GROUP_RESULTS.get(instr.opcode)
            self.push(Expression(constant, 100, call.source_lines) if constant is not None else call)
            return True

        result_name = f"result_L{instr.line_no}"
        schema = multi_result_schema(instr.opcode)
        if schema is not None:
            self.statement("effect", f"{schema.type_name} {result_name} = {call.text};", instr, *call_args)
            for field in schema.fields[:result_count]:
                self.push(Expression(f"{result_name}.{field}", 100, (instr.line_no,)))
            return True

        result_slots = multi_result_slot_names(instr.line_no, result_count)
        self.statement(
            "effect",
            f"values({', '.join(result_slots)}) = {call.text};",
            instr,
            *call_args,
        )
        for result_slot in result_slots:
            self.push(Expression(result_slot, 100, (instr.line_no,)))
        return True

    def lift_misc(self, instr: AsmInstruction) -> bool:
        op, args = instr.opcode, instr.args
        if op == "jump_rel16" and args:
            relative = parse_int(args[0])
            target = (
                None
                if instr.address is None or relative is None
                else self.base_jump_targets.get(instr.address + relative)
            )
            if target is None:
                target = self.base_jump_targets_by_line.get(instr.line_no)
            self.sync_stack()
            if target is not None:
                self.statement("jump", f"goto {target};", instr, target=target)
            else:
                self.statement(
                    "jump_dynamic",
                    f"jump_relative({c_int(args[0])});",
                    instr,
                )
                if relative is None:
                    self.block.statements[-1].control_target = InvalidBoundaryTarget(
                        args[0].strip(),
                        "symbol does not resolve to an instruction boundary",
                    )
                else:
                    self.block.statements[-1].control_target = RelativeDisplacementTarget(relative)
            return True
        if op == "add_frame_imm" and args:
            self.statement("state", f"vm_adjust_frame(vm, {c_int(args[0])});", instr)
            return True
        if op == "load_frame_pointer":
            self.push(Expression("vm_get_frame_pointer(vm)", 100, (instr.line_no,)))
            return True
        if op == "store_frame_pointer":
            value = self.pop()
            self.statement("state", f"vm_set_frame_pointer(vm, {value.text});", instr, value)
            return True
        if op == 'debug_hook':
            self.statement('effect', 'debug_hook();', instr)
            return True
        return False

    def lift(self, instr: AsmInstruction) -> None:
        self.stats.instruction_count += 1
        statement_start = len(self.block.statements)
        key = (instr.opcode, len(instr.args))
        cached_index = _LIFT_HANDLER_INDEX.get(key)
        handled = False
        if cached_index is not None:
            handled = _LIFT_HANDLERS[cached_index](self, instr)
        if not handled:
            for index, handler in enumerate(_LIFT_HANDLERS):
                if index == cached_index:
                    continue
                if handler(self, instr):
                    _LIFT_HANDLER_INDEX[key] = index
                    handled = True
                    break
        if not handled:
            self.opaque(instr)
            return

        represented = any(
            instr.line_no in statement.source_lines
            for statement in self.block.statements[statement_start:]
        ) or any(instr.line_no in expr.source_lines for expr in self.stack)
        if not represented:
            if self.stack:
                self.stack[-1].source_lines = merge_lines(
                    self.stack[-1].source_lines, instr.line_no
                )
            else:
                self.block.statements.append(
                    Statement("trace", f"/* {instr.text} */", (instr.line_no,))
                )
        self.stats.lifted_count += 1

_LIFT_HANDLERS = (
    BlockLifter.lift_push,
    BlockLifter.lift_load,
    BlockLifter.lift_store,
    BlockLifter.lift_arithmetic,
    BlockLifter.lift_control,
    BlockLifter.lift_misc,
    BlockLifter.lift_semantic_opcode,
)
_LIFT_HANDLER_INDEX: dict[tuple[str, int], int] = {}


def jc_condition_expr(value: Expression, flag_text: str, source_line: int) -> Expression:
    """Decode opcode 0x15's unary predicate; bit 3 only selects target encoding."""
    flag = parse_int(flag_text)
    if flag is None:
        return call_expr("jc_flag_test", [imm_expr(flag_text, source_line), value], source_line)

    predicate = flag & 7
    if predicate == 0:
        return value
    if predicate == 1:
        return unary_expr("!", value)
    comparisons = {
        2: ">",
        3: ">=",
        4: "<=",
        5: "<",
    }
    operator = comparisons.get(predicate)
    if operator is not None:
        return binary_expr(value, operator, Expression("0", 100, (source_line,)), 45)
    return call_expr(
        "jc_reserved_predicate",
        [Expression(str(predicate), 100, (source_line,)), value],
        source_line,
    )


def flag_expr(left: Expression, right: Expression, flag_text: str, source_line: int) -> Expression:
    flag = parse_int(flag_text)
    operation = FLAG_OPS.get((flag or 0) & 0x0F) if flag is not None else None
    if operation is None:
        return call_expr("vm_flag_op", [imm_expr(flag_text, source_line), left, right], source_line)
    symbol, precedence = operation
    return binary_expr(left, symbol, right, precedence)


def store_operation_expr(
    left: Expression,
    right: Expression,
    flag_text: str,
    source_line: int,
) -> Expression:
    flag = parse_int(flag_text)
    if flag is None:
        return call_expr("encoded_store_op", [imm_expr(flag_text, source_line), left, right], source_line)
    operation_id = flag & 0x1F
    operations = FLAG_OPS if flag & 0x20 else STORE_ARITHMETIC_OPS
    operation = operations.get(operation_id)
    if operation is None:
        return call_expr("encoded_store_op", [imm_expr(flag_text, source_line), left, right], source_line)
    symbol, precedence = operation
    if not flag & 0x20 and operation_id == 9:
        left = Expression(
            f"(uint32_t){wrap(left, 80)}",
            80,
            left.source_lines,
        )
    return binary_expr(left, symbol, right, precedence)


def jump_flag_expr(left: Expression, right: Expression, flag_text: str, source_line: int) -> Expression:
    result = flag_expr(left, right, flag_text, source_line)
    flag = parse_int(flag_text)
    if flag is None:
        return call_expr("vm_jump_flag", [imm_expr(flag_text, source_line), left, right], source_line)
    return result if flag & 0x80 else unary_expr("!", result)


def is_terminator(statement: Statement) -> bool:
    return statement.kind in {"jump", "jump_dynamic", "return"}


def build_successors(blocks: list[BasicBlock]) -> None:
    ControlFlowGraph.assign_successors(blocks)


def _replace_first_pop(text: str, replacement: str) -> str:
    return text.replace("vm_pop(vm)", f"({replacement})", 1)


VM_ARG_RE = re.compile(r"vm_arg\(vm, (?P<index>\d+)\)")


def _stack_read_count(text: str) -> int:
    return text.count("vm_pop(vm)") + len(VM_ARG_RE.findall(text))


def _replace_next_stack_read(text: str, replacement: str) -> str:
    if "vm_pop(vm)" in text:
        return _replace_first_pop(text, replacement)

    matches = list(VM_ARG_RE.finditer(text))
    if not matches:
        return text
    highest = max(int(match.group("index")) for match in matches)
    for match in matches:
        if int(match.group("index")) != highest:
            continue
        return text[: match.start()] + f"({replacement})" + text[match.end() :]
    return text


def _next_stack_read(text: str) -> str | None:
    if "vm_pop(vm)" in text:
        return "vm_pop(vm)"
    matches = list(VM_ARG_RE.finditer(text))
    if not matches:
        return None
    highest = max(int(match.group("index")) for match in matches)
    return next(
        match.group(0)
        for match in matches
        if int(match.group("index")) == highest
    )


DIRECT_CALL_RE = re.compile(
    r"\bcall_direct\(label_ref\((?P<label>[A-Za-z_][A-Za-z0-9_]*)\)"
)


def _direct_call_target(text: str) -> str | None:
    match = DIRECT_CALL_RE.search(text)
    return match.group("label") if match else None


def _return_value_count(statement: Statement) -> int | None:
    text = statement.text.strip()
    if statement.kind != "return" or not text.startswith("return"):
        return None
    if text == "return;":
        return 0
    match = re.fullmatch(r"return values\((.*)\);", text)
    if match is not None:
        return len(split_arguments(match.group(1)))
    return 1


def infer_direct_call_result_counts(
    blocks: list[BasicBlock],
    function_labels: set[str],
) -> dict[str, int]:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    result: dict[str, int] = {}
    for label in function_labels:
        root = index_by_label.get(label)
        if root is None:
            continue
        pending = [root]
        visited: set[int] = set()
        counts: set[int] = set()
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            block = blocks[index]
            for statement in block.statements:
                count = _return_value_count(statement)
                if count is not None:
                    counts.add(count)
            for successor in block.successors:
                target = index_by_label.get(successor)
                if target is None or (blocks[target].label in function_labels and target != root):
                    continue
                pending.append(target)
        if len(counts) == 1:
            result[label] = counts.pop()
    return result


ENTRY_LOCAL_PARAM_RE = re.compile(r"^local_[0-9A-Fa-f]+ = vm_pop\(vm\);$")
ENTRY_NONLOCAL_PARAM_RE = re.compile(
    r"^(?P<target>(?!local_[0-9A-Fa-f]+\b).+?) = vm_pop\(vm\);$"
)
ENTRY_NONLOCAL_ARGUMENT_RE = re.compile(
    r"^(?P<target>(?!local_[0-9A-Fa-f]+\b).+?) = "
    r"vm_arg\(vm, (?P<index>\d+)\);$"
)
ENTRY_PARAMETER_PRELUDE_RE = re.compile(r"^(?:memclr|memset)\(")
LOOP_STACK_COUNTER_RE = re.compile(
    r"^(?P<counter>local_[0-9A-Fa-f]+) = (?P=counter) \+ (?:1|0x1);$"
)
LOOP_STACK_LIMIT_RE = re.compile(
    r"^(?P<counter>local_[0-9A-Fa-f]+) < (?P<limit>-?(?:0x[0-9A-Fa-f]+|\d+))$"
)


def recover_residual_stack_argument_aliases(blocks: list[BasicBlock]) -> None:
    """Unify a late pop with the same implicit slot used by a following call."""
    for block in blocks:
        for index, statement in enumerate(block.statements):
            source = statement.condition or statement.text
            if source.count("vm_pop(vm)") != 1:
                continue
            following_indices = {
                int(match.group("index"))
                for later in block.statements[index + 1:]
                for text in (later.text, later.condition or "")
                for match in VM_ARG_RE.finditer(text)
            }
            if len(following_indices) != 1:
                continue
            replacement = f"vm_arg(vm, {next(iter(following_indices))})"
            if statement.condition is not None:
                statement.condition = statement.condition.replace("vm_pop(vm)", replacement, 1)
                statement.text = f"if ({statement.condition}) goto {statement.target};"
            else:
                statement.text = statement.text.replace("vm_pop(vm)", replacement, 1)


def recover_counted_loop_stack_parameters(blocks: list[BasicBlock]) -> None:
    """Lift a fixed-count VM-pop loop into explicit scalar stack parameters."""
    for block in blocks:
        if block.label not in block.successors:
            continue
        pop_statements = [
            statement
            for statement in block.statements
            if "vm_pop(vm)" in (statement.condition or statement.text)
        ]
        if len(pop_statements) != 1:
            continue
        counter: str | None = None
        for statement in block.statements:
            increment = LOOP_STACK_COUNTER_RE.fullmatch(statement.text.strip())
            if increment is not None:
                counter = increment.group("counter")
                break
        branch = next(
            (
                statement
                for statement in reversed(block.statements)
                if statement.kind == "branch" and statement.condition
            ),
            None,
        )
        if counter is None or branch is None:
            continue
        limit_match = LOOP_STACK_LIMIT_RE.fullmatch(branch.condition.strip())
        if limit_match is None or limit_match.group("counter") != counter:
            continue
        limit = parse_int(limit_match.group("limit"))
        if limit is None or limit <= 0 or limit > 64:
            continue
        slots = ", ".join(f"vm_arg(vm, {index})" for index in range(limit))
        replacement = f"((int32_t[]){{{slots}}})[{limit - 1} - {counter}]"
        statement = pop_statements[0]
        if statement.condition is not None:
            statement.condition = statement.condition.replace("vm_pop(vm)", replacement, 1)
            statement.text = f"if ({statement.condition}) goto {statement.target};"
        else:
            statement.text = statement.text.replace("vm_pop(vm)", replacement, 1)


def recover_entry_stack_parameters(
    blocks: list[BasicBlock],
    function_labels: set[str],
) -> dict[str, int]:
    """Recover entry pops, including parameters stored directly to globals.

    Local entry pops are removed later by the emitter.  Non-local destinations
    need an explicit ``vm_arg`` placeholder so they become real C parameters
    instead of residual ``stack_pop`` calls.
    """
    recover_residual_stack_argument_aliases(blocks)
    recover_counted_loop_stack_parameters(blocks)
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    roots = set(function_labels)
    if blocks:
        roots.add(blocks[0].label)
    counts: dict[str, int] = {}
    for label in roots:
        root = index_by_label.get(label)
        if root is None:
            continue
        parameter_count = 0
        local_parameter_count = 0
        stack_parameter_index = 0
        saw_parameter = False
        scanning = True
        for block in blocks[root:]:
            if not scanning:
                break
            for statement in block.statements:
                if statement.kind == "state":
                    continue
                text = statement.text.strip()
                if not saw_parameter and ENTRY_PARAMETER_PRELUDE_RE.match(text):
                    continue
                if ENTRY_LOCAL_PARAM_RE.fullmatch(text):
                    parameter_count += 1
                    local_parameter_count += 1
                    saw_parameter = True
                    continue
                existing_argument = ENTRY_NONLOCAL_ARGUMENT_RE.fullmatch(text)
                if existing_argument is not None:
                    parameter_count += 1
                    saw_parameter = True
                    stack_parameter_index = max(
                        stack_parameter_index,
                        int(existing_argument.group("index")) + 1,
                    )
                    continue
                nonlocal_param = ENTRY_NONLOCAL_PARAM_RE.fullmatch(text)
                if nonlocal_param is not None:
                    statement.text = (
                        f"{nonlocal_param.group('target')} = "
                        f"vm_arg(vm, {stack_parameter_index});"
                    )
                    parameter_count += 1
                    stack_parameter_index += 1
                    saw_parameter = True
                    continue
                scanning = False
                break

        # A direct call can consume a caller-owned value that is still below
        # the values produced in the current block.  The lifter represents
        # that underflow as ``vm_arg`` so CFG stack propagation gets a chance
        # to replace it.  If it survives, it is a real function parameter and
        # must participate in the next calling-convention iteration.
        pending = [root]
        visited: set[int] = set()
        maximum_stack_parameter = -1
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            block = blocks[index]
            for statement in block.statements:
                for source in (statement.text, statement.condition or ""):
                    maximum_stack_parameter = max(
                        maximum_stack_parameter,
                        max(
                            (
                                int(match.group("index"))
                                for match in VM_ARG_RE.finditer(source)
                            ),
                            default=-1,
                        ),
                    )
            for successor in block.successors:
                target = index_by_label.get(successor)
                if target is None:
                    continue
                if blocks[target].label in roots and target != root:
                    continue
                pending.append(target)

        inferred_count = local_parameter_count + maximum_stack_parameter + 1
        counts[label] = max(parameter_count, inferred_count)
    return counts


def normalize_direct_call_arguments(
    blocks: list[BasicBlock],
    argument_counts: dict[str, int],
) -> None:
    """Trim VM-preserved lower stack values from direct C call arguments."""
    for block in blocks:
        for statement in block.statements:
            if statement.kind != "call":
                continue
            text = statement.text.strip()
            if not text.startswith("call_direct(") or not text.endswith(");"):
                continue
            arguments = split_arguments(text[len("call_direct(") : -2])
            if not arguments:
                continue
            target_match = LABEL_REF_RE.fullmatch(arguments[0])
            if target_match is None:
                continue
            count = argument_counts.get(target_match.group("label"))
            if count is None or len(arguments) - 1 <= count:
                continue
            call_arguments = arguments[-count:] if count else []
            rendered = ", ".join([arguments[0], *call_arguments])
            statement.text = f"call_direct({rendered});"


DIRECT_MULTI_STORE_RE = re.compile(
    r"^(?P<target>local_[0-9A-Fa-f]+) = vm_pop\(vm\);$"
)


def multi_result_slot_names(source_line: int, result_count: int) -> list[str]:
    return [f"result_L{source_line}_{index}" for index in range(result_count)]


def fold_direct_multi_store_results(
    blocks: list[BasicBlock],
    result_counts: dict[str, int],
) -> None:
    """Render direct tuple returns as one high-level multi-assignment."""
    for block in blocks:
        index = 0
        while index < len(block.statements):
            call = block.statements[index]
            target = _direct_call_target(call.text) if call.kind == "call" else None
            result_count = result_counts.get(target or "", 0)
            if result_count <= 1 or index + 1 >= len(block.statements):
                index += 1
                continue

            consumers: list[Statement] = []
            matches: list[re.Match[str]] = []
            for statement in block.statements[index + 1 : index + result_count + 1]:
                match = DIRECT_MULTI_STORE_RE.fullmatch(statement.text)
                if match is None:
                    break
                consumers.append(statement)
                matches.append(match)
            if not matches:
                index += 1
                continue

            destinations = [match.group("target") for match in matches]
            discarded = ["_"] * (result_count - len(destinations))
            call_expression = call.text[:-1]
            call.kind = "store"
            call.text = (
                f"values({', '.join(discarded + list(reversed(destinations)))}) = "
                f"{call_expression};"
            )
            call.source_lines = merge_lines(
                call.source_lines,
                *(statement.source_lines for statement in consumers),
            )
            del block.statements[index + 1 : index + len(consumers) + 1]
            index += 1


def fold_multi_call_results(blocks: list[BasicBlock], result_counts: dict[str, int]) -> None:
    for block in blocks:
        for call_index, call in enumerate(block.statements):
            if call.kind != "call" or not call.text.endswith(";"):
                continue
            target = _direct_call_target(call.text)
            result_count = result_counts.get(target or "", 0)
            if result_count <= 1:
                continue

            result_line = call.source_lines[-1] if call.source_lines else call_index
            result_slots = multi_result_slot_names(result_line, result_count)
            remaining = result_count
            updates: list[tuple[Statement, str, str | None]] = []
            for consumer in block.statements[call_index + 1 :]:
                condition = consumer.condition
                text = condition if condition else consumer.text
                while remaining and _stack_read_count(text):
                    text = _replace_next_stack_read(text, result_slots[remaining - 1])
                    remaining -= 1
                if condition:
                    rendered = f"if ({text}) goto {consumer.target};"
                    updates.append((consumer, rendered, text))
                else:
                    updates.append((consumer, text, None))
                if not remaining or _blocks_stack_propagation(consumer):
                    break
            if remaining:
                continue

            call_expression = call.text[:-1]
            call.kind = "effect"
            call.text = f"values({', '.join(result_slots)}) = {call_expression};"
            for consumer, text, condition in updates:
                consumer.text = text
                consumer.condition = condition
                consumer.source_lines = merge_lines(call.source_lines, consumer.source_lines)


STACK_MULTI_STORE_RE = re.compile(
    r"^(?P<target>local_[0-9A-Fa-f]+) = vm_pop\(vm\);$"
)


def fold_store_multi_call_results(blocks: list[BasicBlock]) -> None:
    """Recover the exact result width encoded by a following store_multi."""
    for block in blocks:
        index = 0
        while index + 2 < len(block.statements):
            call = block.statements[index]
            if call.kind not in {"call", "opaque"} or not call.text.endswith(";"):
                index += 1
                continue

            first_store = block.statements[index + 1]
            if not first_store.source_lines:
                index += 1
                continue
            store_lines = first_store.source_lines
            stores: list[tuple[Statement, re.Match[str]]] = []
            cursor = index + 1
            while cursor < len(block.statements):
                statement = block.statements[cursor]
                match = STACK_MULTI_STORE_RE.fullmatch(statement.text)
                if statement.kind != "store" or statement.source_lines != store_lines or match is None:
                    break
                stores.append((statement, match))
                cursor += 1

            # One assembly instruction can emit several stores only for
            # store_multi, so this run gives an exact return-value count.
            if len(stores) < 2:
                index += 1
                continue

            result_line = call.source_lines[-1] if call.source_lines else index
            call_expression = call.text[:-1]
            call.kind = "effect"
            result_count = len(stores)
            result_slots = multi_result_slot_names(result_line, result_count)
            call.text = f"values({', '.join(result_slots)}) = {call_expression};"
            for result_index, (statement, _) in enumerate(stores):
                statement.text = _replace_next_stack_read(
                    statement.text,
                    result_slots[result_count - result_index - 1],
                )
                statement.source_lines = merge_lines(call.source_lines, statement.source_lines)
            index = cursor


def fold_call_results(
    blocks: list[BasicBlock],
    result_counts: dict[str, int] | None = None,
) -> None:
    for block in blocks:
        index = 0
        while index + 1 < len(block.statements):
            call = block.statements[index]
            consumer = block.statements[index + 1]
            if call.kind not in {"call", "opaque"} or not call.text.endswith(";"):
                index += 1
                continue
            target = _direct_call_target(call.text)
            if (
                result_counts is not None
                and target is not None
                and result_counts.get(target) != 1
            ):
                index += 1
                continue
            call_expression = call.text[:-1]
            condition_has_read = consumer.condition and _stack_read_count(consumer.condition)
            if condition_has_read:
                consumer.condition = _replace_next_stack_read(consumer.condition, call_expression)
                consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
            elif _stack_read_count(consumer.text):
                consumer.text = _replace_next_stack_read(consumer.text, call_expression)
            else:
                index += 1
                continue
            consumer.source_lines = merge_lines(call.source_lines, consumer.source_lines)
            del block.statements[index]
            index = max(0, index - 1)


def fold_deferred_call_results(blocks: list[BasicBlock]) -> None:
    """Pair a suspending opcode with its immediately resumed completion value."""
    opcode_pattern = "|".join(re.escape(opcode) for opcode in sorted(DEFERRED_RESULT_OPCODES))
    deferred_call = re.compile(rf"^(?:{opcode_pattern})\(.*\);$")
    for block in blocks:
        index = 0
        while index + 1 < len(block.statements):
            call = block.statements[index]
            consumer = block.statements[index + 1]
            if call.kind != "effect" or deferred_call.fullmatch(call.text) is None:
                index += 1
                continue

            call_expression = call.text[:-1]
            if consumer.condition and _stack_read_count(consumer.condition):
                consumer.condition = _replace_next_stack_read(consumer.condition, call_expression)
                if consumer.kind == "branch":
                    consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
            elif _stack_read_count(consumer.text):
                consumer.text = _replace_next_stack_read(consumer.text, call_expression)
            else:
                index += 1
                continue
            consumer.source_lines = merge_lines(call.source_lines, consumer.source_lines)
            del block.statements[index]
            index = max(0, index - 1)


def fold_delayed_call_results(
    blocks: list[BasicBlock],
    result_counts: dict[str, int] | None = None,
) -> None:
    for block in blocks:
        for call_index, call in enumerate(block.statements):
            if call.kind not in {"call", "opaque"} or not call.text.endswith(";"):
                continue
            target = _direct_call_target(call.text)
            if (
                result_counts is not None
                and target is not None
                and result_counts.get(target) != 1
            ):
                continue

            for consumer_index in range(call_index + 1, len(block.statements)):
                consumer = block.statements[consumer_index]
                text = consumer.condition if consumer.condition else consumer.text
                if _stack_read_count(text):
                    if consumer_index == call_index + 1:
                        break
                    result_name = f"result_L{call.source_lines[-1] if call.source_lines else call_index}"
                    call_expression = call.text[:-1]
                    call.kind = "effect"
                    call.text = f"int32_t {result_name} = {call_expression};"
                    if consumer.condition:
                        consumer.condition = _replace_next_stack_read(consumer.condition, result_name)
                        consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
                    else:
                        consumer.text = _replace_next_stack_read(consumer.text, result_name)
                    consumer.source_lines = merge_lines(call.source_lines, consumer.source_lines)
                    break
                if _blocks_stack_propagation(consumer):
                    break


def _trailing_stack_statement(block: BasicBlock) -> Statement | None:
    index = len(block.statements) - 1
    if index >= 0 and block.statements[index].kind in {"jump", "branch", "return", "jump_dynamic"}:
        index -= 1
    while (
        index >= 0
        and block.statements[index].kind == "store"
        and re.match(r"^phi_\d+ = ", block.statements[index].text)
    ):
        index -= 1
    if index >= 0 and block.statements[index].kind in {"stack", "stack_preserved"}:
        return block.statements[index]
    return None


def _blocks_stack_propagation(statement: Statement) -> bool:
    return statement.kind in {"stack", "stack_preserved", "call", "opaque", "return", "jump_dynamic"}


def _first_stack_consumer(block: BasicBlock) -> Statement | None:
    for statement in block.statements:
        text = statement.condition if statement.condition else statement.text
        if _stack_read_count(text):
            return statement
        if _blocks_stack_propagation(statement):
            return None
    return None


def reattach_orphan_stack_prefixes(blocks: list[BasicBlock]) -> None:
    """Route jumps through synthetic stack setup emitted just before a label."""
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].add(source_index)

    changed = False
    for target_index in range(1, len(blocks)):
        prefix_index = target_index - 1
        prefix = blocks[prefix_index]
        target = blocks[target_index]
        prefix_statements = [
            statement for statement in prefix.statements if statement.kind not in {"state", "trace"}
        ]
        if (
            not prefix.synthetic
            or target.synthetic
            or predecessors[prefix_index]
            or prefix.successors != [target.label]
            or not prefix_statements
            or any(statement.kind not in {"stack", "stack_preserved"} for statement in prefix_statements)
            or _first_stack_consumer(target) is None
        ):
            continue
        incoming = predecessors[target_index].difference({prefix_index})
        if not incoming:
            continue
        for source_index in incoming:
            source = blocks[source_index]
            source.successors = [
                prefix.label if successor == target.label else successor
                for successor in source.successors
            ]
            for statement in source.statements:
                if statement.kind not in {"branch", "jump"} or statement.target != target.label:
                    continue
                statement.target = prefix.label
                if statement.kind == "branch" and statement.condition is not None:
                    statement.text = f"if ({statement.condition}) goto {prefix.label};"
                else:
                    statement.text = f"goto {prefix.label};"
        changed = True
    if changed:
        build_successors(blocks)


DIRECT_LOCAL_HELPER_STORE_RE = re.compile(
    r"^store_i32\(\(*\s*&(?P<name>local_[0-9A-Fa-f]+)\s*\)*,\s*(?P<value>.+)\);$"
)
LOCAL_SELF_OFFSET_STORE_RE = re.compile(
    r"^store_i32\(\(*\s*(?P<name>local_[0-9A-Fa-f]+)\s*\)*,\s*"
    r"\(*\s*&(?P=name)\s*\)*\s*(?P<operator>[+-])\s*(?P<value>.+)\);$"
)
DIRECT_LOCAL_ASSIGNMENT_RE = re.compile(
    r"^(?P<name>local_[0-9A-Fa-f]+)\s*=\s*(?P<value>.+);$"
)


def fold_direct_local_helper_stores(blocks: list[BasicBlock]) -> None:
    for block in blocks:
        for statement in block.statements:
            match = DIRECT_LOCAL_HELPER_STORE_RE.fullmatch(statement.text.strip())
            if match is not None:
                statement.text = f"{match.group('name')} = {match.group('value')};"
                continue

            # A taken branch can bypass the physical push/load prefix that
            # normally feeds cmp_store.  Stack propagation then exposes the
            # same scalar local once as a value and once as its address.  The
            # VM operation is still a scalar self-update, not an array store.
            self_offset = LOCAL_SELF_OFFSET_STORE_RE.fullmatch(statement.text.strip())
            if self_offset is not None:
                name = self_offset.group("name")
                statement.text = (
                    f"{name} = {name} {self_offset.group('operator')} "
                    f"{self_offset.group('value')};"
                )


RANGE_STACK_PUSH_RE = re.compile(
    r"^vm_push\(vm, (?P<name>local_[0-9A-Fa-f]+) "
    r"(?P<operator>>=|>) (?P<limit>-?(?:0[xX][0-9A-Fa-f]+|\d+))\);$"
)
RANGE_STACK_UPPER_PUSH_RE = re.compile(
    r"^vm_push\(vm, (?P<name>local_[0-9A-Fa-f]+) "
    r"(?P<operator><=|<) (?P<limit>-?(?:0[xX][0-9A-Fa-f]+|\d+))\);$"
)


def fold_range_stack_guards(blocks: list[BasicBlock]) -> None:
    """Replace a split lower/upper range stack protocol with one condition."""
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    for first_index, first in enumerate(blocks):
        first_pushes = [
            statement
            for statement in first.statements
            if statement.kind == "stack" and RANGE_STACK_PUSH_RE.fullmatch(statement.text.strip())
        ]
        if len(first_pushes) != 1 or len(first.successors) != 1:
            continue
        first_push = first_pushes[0]
        lower = RANGE_STACK_PUSH_RE.fullmatch(first_push.text.strip())
        second_index = index_by_label.get(first.successors[0])
        if lower is None or second_index is None or second_index == first_index:
            continue

        second = blocks[second_index]
        second_pushes = [
            statement
            for statement in second.statements
            if statement.kind == "stack"
            and RANGE_STACK_UPPER_PUSH_RE.fullmatch(statement.text.strip())
        ]
        if len(second_pushes) != 1 or len(second.successors) != 1:
            continue
        second_push = second_pushes[0]
        upper = RANGE_STACK_UPPER_PUSH_RE.fullmatch(second_push.text.strip())
        if upper is None or upper.group("name") != lower.group("name"):
            continue

        consumer_index = index_by_label.get(second.successors[0])
        if consumer_index is None:
            continue
        consumer = next(
            (
                statement
                for statement in blocks[consumer_index].statements
                if statement.kind == "branch"
                and statement.condition is not None
                and "vm_pop(vm) && vm_pop(vm)" in statement.condition
            ),
            None,
        )
        if consumer is None or consumer.condition is None:
            continue

        range_condition = (
            f"{lower.group('name')} {lower.group('operator')} {lower.group('limit')} && "
            f"{upper.group('name')} {upper.group('operator')} {upper.group('limit')}"
        )
        consumer.condition = consumer.condition.replace(
            "vm_pop(vm) && vm_pop(vm)",
            range_condition,
            1,
        )
        consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
        consumer.source_lines = merge_lines(
            first_push.source_lines,
            second_push.source_lines,
            consumer.source_lines,
        )
        first.statements = [statement for statement in first.statements if statement is not first_push]
        second.statements = [statement for statement in second.statements if statement is not second_push]


def fold_conditional_assignment_dispatches(blocks: list[BasicBlock]) -> None:
    """Recover a branch ladder whose cases assign one local and share a return."""
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].add(source_index)

    consumed: set[int] = set()
    for start_index in range(len(blocks)):
        if start_index in consumed:
            continue
        cases: list[tuple[int, Statement, int, Statement, Statement, str]] = []
        local_name: str | None = None
        join_label: str | None = None
        cursor = start_index
        while cursor < len(blocks) - 1:
            test_block = blocks[cursor]
            branch = next(
                (statement for statement in reversed(test_block.statements) if statement.kind in {"branch", "jump", "return", "jump_dynamic"}),
                None,
            )
            if branch is None or branch.kind != "branch" or not branch.condition or not branch.target:
                break
            case_index = index_by_label.get(branch.target)
            if case_index is None or case_index in {cursor, cursor + 1}:
                break
            case_block = blocks[case_index]
            meaningful = [
                statement
                for statement in case_block.statements
                if statement.kind not in {"state", "trace", "jump"}
            ]
            case_jump = next(
                (statement for statement in reversed(case_block.statements) if statement.kind == "jump"),
                None,
            )
            if len(meaningful) != 1 or case_jump is None or not case_jump.target:
                break
            assignment = DIRECT_LOCAL_ASSIGNMENT_RE.fullmatch(meaningful[0].text.strip())
            if assignment is None:
                break
            if local_name is None:
                local_name = assignment.group("name")
                join_label = case_jump.target
            elif assignment.group("name") != local_name or case_jump.target != join_label:
                break
            next_label = blocks[cursor + 1].label
            if next_label not in test_block.successors:
                break
            cases.append(
                (
                    cursor,
                    branch,
                    case_index,
                    meaningful[0],
                    case_jump,
                    assignment.group("value").strip(),
                )
            )
            cursor += 1

        if len(cases) < 2 or local_name is None or join_label is None or cursor >= len(blocks):
            continue
        default_block = blocks[cursor]
        default_meaningful = [
            statement
            for statement in default_block.statements
            if statement.kind not in {"state", "trace"}
        ]
        default_value = local_name
        if (
            len(default_meaningful) == 1
            and default_meaningful[0].kind == "jump"
            and default_meaningful[0].target == join_label
        ):
            default_control = default_meaningful[0]
        elif (
            len(default_meaningful) == 2
            and default_meaningful[1].kind == "jump"
            and default_meaningful[1].target == join_label
        ):
            default_assignment = DIRECT_LOCAL_ASSIGNMENT_RE.fullmatch(default_meaningful[0].text.strip())
            if default_assignment is None or default_assignment.group("name") != local_name:
                continue
            default_value = default_assignment.group("value").strip()
            default_control = default_meaningful[1]
        else:
            continue
        join_index = index_by_label.get(join_label)
        if join_index is None:
            continue
        join_returns_local = any(
            statement.kind == "return" and statement.text.strip() == f"return {local_name};"
            for statement in blocks[join_index].statements
        )
        if not join_returns_local:
            continue
        case_test_indices = {case[0] for case in cases}
        case_target_indices = {case[2] for case in cases}
        if len(case_target_indices) != len(cases):
            continue
        if any(
            predecessor not in case_test_indices
            and not (
                blocks[predecessor].synthetic
                and not [
                    statement
                    for statement in blocks[predecessor].statements
                    if statement.kind not in {"state", "trace"}
                ]
            )
            for case_index in case_target_indices
            for predecessor in predecessors[case_index]
        ):
            continue

        selection_arguments = [local_name]
        for _, branch, _, _, _, value in cases:
            selection_arguments.extend((branch.condition, value))
        selection_arguments.append(default_value)
        source_lines = merge_lines(
            *(statement.source_lines for case in cases for statement in (case[1], case[3], case[4])),
            default_control.source_lines,
        )
        first_block = blocks[start_index]
        first_branch = cases[0][1]
        first_block.statements = [statement for statement in first_block.statements if statement is not first_branch]
        first_block.statements.extend(
            [
                Statement(
                    "conditional_assign",
                    f"select_assign({', '.join(selection_arguments)});",
                    source_lines,
                ),
                Statement("jump", f"goto {join_label};", source_lines, target=join_label),
            ]
        )
        first_block.successors = [join_label]
        for test_index, _, _, _, _, _ in cases[1:]:
            blocks[test_index].statements = []
        default_block.statements = []
        for case_index in case_target_indices:
            blocks[case_index].statements = []
        consumed.update(case_test_indices | case_target_indices | {cursor})
    build_successors(blocks)


def fold_relative_assignment_ladders(blocks: list[BasicBlock]) -> None:
    """Collapse conditional branches into a linear, IP-skipping assignment tail.

    Some VM compilers encode a lookup as a ladder of tests followed by labels
    that each assign a value and adjust the VM base to the common return block.
    The CFG only sees the physical fallthrough, so emitting it literally
    overwrites every selected value and produces a chain of misleading gotos.
    When all tail blocks are single-local assignments, recover the lookup as a
    lazy conditional assignment expression.
    """
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].add(source_index)

    def meaningful(block: BasicBlock) -> list[Statement]:
        return [
            statement
            for statement in block.statements
            if statement.kind not in {"state", "trace"}
        ]

    def assignment(block: BasicBlock) -> tuple[Statement, re.Match[str]] | None:
        statements = meaningful(block)
        if len(statements) != 1:
            return None
        statement = statements[0]
        match = DIRECT_LOCAL_ASSIGNMENT_RE.fullmatch(statement.text.strip())
        return (statement, match) if match is not None else None

    def local_initial_value(local_name: str, before: int) -> str | None:
        for block in reversed(blocks[:before]):
            for statement in reversed(block.statements):
                match = DIRECT_LOCAL_ASSIGNMENT_RE.fullmatch(statement.text.strip())
                if match is not None and match.group("name") == local_name:
                    value = match.group("value").strip()
                    return local_name if value == "vm_pop(vm)" else value
        return None

    consumed: set[int] = set()
    for first_index in range(len(blocks)):
        if first_index in consumed:
            continue

        cases: list[tuple[int, Statement, int, str | None]] = []
        cursor = first_index
        while cursor < len(blocks) - 1:
            test_statements = meaningful(blocks[cursor])
            if len(test_statements) != 1 or test_statements[0].kind != "branch":
                break
            branch = test_statements[0]
            if not branch.condition or not branch.target:
                break
            next_index = cursor + 1
            if blocks[next_index].label not in blocks[cursor].successors:
                break
            target_index = index_by_label.get(branch.target)
            if target_index is None or target_index <= cursor or target_index == next_index:
                break
            target_statements = meaningful(blocks[target_index])
            if any(statement.kind in {"branch", "jump", "jump_dynamic"} for statement in target_statements):
                break
            target_assignment = assignment(blocks[target_index])
            value = target_assignment[1].group("value").strip() if target_assignment else None
            if target_assignment is None and target_statements and not (
                len(target_statements) == 1 and target_statements[0].kind == "return"
            ):
                break
            cases.append((cursor, branch, target_index, value))
            cursor = next_index

        if len(cases) < 2 or cursor >= len(blocks):
            continue
        default_index = cursor
        default_assignment = assignment(blocks[default_index])
        if default_assignment is None:
            continue
        local_name = default_assignment[1].group("name")

        invalid_target = False
        for _, _, target_index, value in cases:
            if value is None:
                continue
            target_assignment = assignment(blocks[target_index])
            if target_assignment is None or target_assignment[1].group("name") != local_name:
                invalid_target = True
                break
        if invalid_target:
            continue
        if any(value is None for _, _, target_index, value in cases):
            initial_value = local_initial_value(local_name, first_index)
            if initial_value is None:
                continue
        else:
            initial_value = None

        # Follow the physical tail until the common return of this ladder.
        tail_end = default_index
        join_index: int | None = None
        while tail_end < len(blocks):
            tail_statements = meaningful(blocks[tail_end])
            if any(
                statement.kind == "return"
                and statement.text.strip() == f"return {local_name};"
                for statement in tail_statements
            ):
                join_index = tail_end
                break
            if len(blocks[tail_end].successors) != 1:
                break
            successor_index = index_by_label.get(blocks[tail_end].successors[0])
            if successor_index != tail_end + 1:
                break
            tail_end += 1
        if join_index is None or join_index <= default_index:
            continue

        tail_indices = set(range(default_index, join_index))
        test_indices = {test_index for test_index, _, _, _ in cases}
        target_indices = {target_index for _, _, target_index, _ in cases}
        if not target_indices <= tail_indices | {join_index}:
            continue
        if any(
            predecessor not in tail_indices | test_indices
            for index in tail_indices
            for predecessor in predecessors[index]
        ):
            continue

        values = [
            value if value is not None else initial_value
            for _, _, _, value in cases
        ]
        if any(value is None for value in values):
            continue
        selection_arguments = [local_name]
        for (_, branch, _, _), value in zip(cases, values):
            selection_arguments.extend((branch.condition or "false", value or local_name))
        selection_arguments.append(default_assignment[1].group("value").strip())
        case_lines: list[tuple[int, ...]] = []
        for _, branch, target_index, _ in cases:
            case_lines.append(branch.source_lines)
            target_assignment = assignment(blocks[target_index])
            if target_assignment is not None:
                case_lines.append(target_assignment[0].source_lines)
        source_lines = merge_lines(*case_lines, default_assignment[0].source_lines)

        first_block = blocks[first_index]
        first_branch = cases[0][1]
        first_block.statements = [
            statement for statement in first_block.statements if statement is not first_branch
        ]
        first_block.statements.append(
            Statement(
                "conditional_assign",
                f"select_assign({', '.join(selection_arguments)});",
                source_lines,
            )
        )
        for index in range(first_index + 1, join_index):
            if index != first_index:
                blocks[index].statements = []
        consumed.update(range(first_index, join_index))
        build_successors(blocks)


def propagate_single_edge_stacks(blocks: list[BasicBlock]) -> None:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].append(source_index)

    changed = True
    while changed:
        changed = False
        for source_index, source in enumerate(blocks):
            if len(source.successors) != 1:
                continue
            target_index = index_by_label.get(source.successors[0])
            if target_index is None:
                continue
            previous_index = source_index
            seen_targets: set[int] = set()
            while predecessors[target_index] == [previous_index]:
                if target_index in seen_targets:
                    target_index = -1
                    break
                seen_targets.add(target_index)
                candidate = blocks[target_index]
                if _first_stack_consumer(candidate) is not None:
                    break
                if any(_blocks_stack_propagation(statement) for statement in candidate.statements):
                    target_index = -1
                    break
                if len(candidate.successors) != 1:
                    target_index = -1
                    break
                next_index = index_by_label.get(candidate.successors[0])
                if next_index is None:
                    target_index = -1
                    break
                previous_index, target_index = target_index, next_index
            if target_index < 0 or predecessors[target_index] != [previous_index]:
                continue
            target = blocks[target_index]
            control_index = len(source.statements)
            if control_index and source.statements[-1].kind in {"jump", "branch", "return", "jump_dynamic"}:
                control_index -= 1
            stack_start = control_index
            while (
                stack_start > 0
                and source.statements[stack_start - 1].kind in {"stack", "stack_preserved"}
            ):
                stack_start -= 1
            stack_statements = source.statements[stack_start:control_index]
            if not stack_statements:
                continue

            values: list[tuple[str, Statement]] = []
            for statement in stack_statements:
                match = re.fullmatch(r"vm_push\(vm, (.*)\);", statement.text)
                if match is None:
                    values = []
                    break
                values.append((match.group(1), statement))
            if not values:
                continue

            available = list(reversed(values))
            if not available:
                continue

            replacements: list[tuple[Statement, str, Statement]] = []
            remaining = list(available)
            for statement in target.statements:
                text = statement.condition if statement.condition else statement.text
                pop_count = _stack_read_count(text)
                if pop_count == 0:
                    if _blocks_stack_propagation(statement):
                        break
                    continue
                for _ in range(pop_count):
                    if not remaining:
                        break
                    value_text, push_statement = remaining.pop(0)
                    replacements.append((statement, value_text, push_statement))
                if not remaining or statement.kind in {"call", "opaque", "branch", "jump", "return"}:
                    break
            if not replacements:
                continue

            consumed_push_ids = {id(push_statement) for _, _, push_statement in replacements}
            source.statements = [statement for statement in source.statements if id(statement) not in consumed_push_ids]
            for statement, value_text, push_statement in replacements:
                if statement.condition and _stack_read_count(statement.condition):
                    statement.condition = _replace_next_stack_read(statement.condition, value_text)
                    statement.text = f"if ({statement.condition}) goto {statement.target};"
                else:
                    statement.text = _replace_next_stack_read(statement.text, value_text)
                statement.source_lines = merge_lines(push_statement.source_lines, statement.source_lines)
            changed = True


def _inherited_stack_provider(
    blocks: list[BasicBlock],
    predecessors: dict[int, list[int]],
    source_index: int,
    seen: set[int] | None = None,
) -> tuple[int, Statement, str] | None:
    visited = set(seen or ())
    if source_index in visited:
        return None
    visited.add(source_index)

    statement = _trailing_stack_statement(blocks[source_index])
    if statement is not None:
        match = re.fullmatch(r"vm_push\(vm, (.*)\);", statement.text)
        if match is not None:
            return source_index, statement, match.group(1)

    if any(
        _blocks_stack_propagation(statement)
        or _stack_read_count(statement.condition if statement.condition else statement.text)
        for statement in blocks[source_index].statements
    ):
        return None
    incoming = predecessors[source_index]
    if len(incoming) != 1:
        return None
    return _inherited_stack_provider(blocks, predecessors, incoming[0], visited)


def _forward_stack_providers(
    blocks: list[BasicBlock],
    predecessors: dict[int, list[int]],
    source_index: int,
    seen: set[int] | None = None,
) -> list[tuple[int, Statement, str]] | None:
    direct = _inherited_stack_provider(blocks, predecessors, source_index, seen)
    if direct is not None:
        return [direct]

    visited = set(seen or ())
    if source_index in visited:
        return None
    visited.add(source_index)
    block = blocks[source_index]
    if any(
        _blocks_stack_propagation(statement)
        or _stack_read_count(statement.condition if statement.condition else statement.text)
        for statement in block.statements
    ):
        return None

    incoming = predecessors[source_index]
    if len(incoming) == 1:
        return _forward_stack_providers(
            blocks,
            predecessors,
            incoming[0],
            visited,
        )
    if len(incoming) < 2 or any(index >= source_index for index in incoming):
        return None

    providers: list[tuple[int, Statement, str]] = []
    provider_ids: set[int] = set()
    for incoming_index in incoming:
        inherited = _forward_stack_providers(blocks, predecessors, incoming_index, visited)
        if not inherited:
            return None
        for provider in inherited:
            if id(provider[1]) in provider_ids:
                continue
            providers.append(provider)
            provider_ids.add(id(provider[1]))
    return providers or None


def _stack_forwarding_is_covered(
    blocks: list[BasicBlock],
    index_by_label: dict[str, int],
    source_index: int,
    valid_targets: set[int],
    seen: set[int] | None = None,
) -> bool:
    if source_index in valid_targets:
        return True
    visited = set(seen or ())
    if source_index in visited:
        return False
    visited.add(source_index)
    block = blocks[source_index]
    if any(
        _blocks_stack_propagation(statement)
        or _stack_read_count(statement.condition if statement.condition else statement.text)
        for statement in block.statements
    ):
        return False
    successors = [index_by_label.get(label) for label in block.successors]
    if not successors or any(index is None for index in successors):
        return False
    return all(
        _stack_forwarding_is_covered(
            blocks,
            index_by_label,
            index,
            valid_targets,
            visited,
        )
        for index in successors
        if index is not None
    )


def propagate_phi_stacks(blocks: list[BasicBlock]) -> None:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].append(source_index)

    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1
    while True:
        candidates: dict[int, tuple[Statement, list[tuple[int, Statement, str]]]] = {}
        for target_index, source_indices in predecessors.items():
            if not source_indices:
                continue
            consumer = _first_stack_consumer(blocks[target_index])
            if consumer is None:
                continue
            providers: list[tuple[int, Statement, str]] = []
            provider_ids: set[int] = set()
            for source_index in source_indices:
                inherited = _forward_stack_providers(blocks, predecessors, source_index)
                if inherited is None:
                    # A loop preheader may seed a value that is intentionally
                    # reused by the structured loop.  A bare back edge does
                    # not invalidate that loop-invariant seed.
                    if source_index >= target_index:
                        continue
                    providers = []
                    break
                for provider_index, statement, value in inherited:
                    if id(statement) not in provider_ids:
                        providers.append((provider_index, statement, value))
                        provider_ids.add(id(statement))
            if providers:
                candidates[target_index] = (consumer, providers)

        valid_targets = set(candidates)
        while valid_targets:
            reusable_sources = {
                source_index
                for target_index in valid_targets
                for source_index, _, _ in candidates[target_index][1]
            }
            eligible_sources = {
                source_index
                for source_index, block in enumerate(blocks)
                if block.successors
                and (
                    source_index in reusable_sources
                    or all(index_by_label.get(successor) in valid_targets for successor in block.successors)
                )
            }
            narrowed = {
                target_index
                for target_index in valid_targets
                if all(source_index in eligible_sources for source_index, _, _ in candidates[target_index][1])
            }
            if narrowed == valid_targets:
                break
            valid_targets = narrowed
        if not valid_targets:
            break

        assignments: dict[int, list[tuple[str, str, tuple[int, ...], bool]]] = {}
        for target_index in sorted(valid_targets):
            consumer, providers = candidates[target_index]
            phi_name = f"phi_{phi_index}"
            phi_index += 1
            provider_lines: tuple[int, ...] = ()
            for source_index, statement, value in providers:
                preserve_stack = any(
                    successor_index is None
                    or not _stack_forwarding_is_covered(
                        blocks,
                        index_by_label,
                        successor_index,
                        valid_targets,
                    )
                    for successor in blocks[source_index].successors
                    for successor_index in [index_by_label.get(successor)]
                )
                assignments.setdefault(id(statement), []).append(
                    (phi_name, value, statement.source_lines, preserve_stack)
                )
                provider_lines = merge_lines(provider_lines, statement.source_lines)
            if consumer.condition and _stack_read_count(consumer.condition):
                consumer.condition = _replace_next_stack_read(consumer.condition, phi_name)
                consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
            else:
                consumer.text = _replace_next_stack_read(consumer.text, phi_name)
            consumer.source_lines = merge_lines(provider_lines, consumer.source_lines)

        for block in blocks:
            rewritten: list[Statement] = []
            for statement in block.statements:
                items = assignments.get(id(statement))
                if not items:
                    rewritten.append(statement)
                    continue
                first_name, first_value, first_lines, _ = items[0]
                rewritten.append(Statement("store", f"{first_name} = {first_value};", first_lines))
                for phi_name, _, lines, _ in items[1:]:
                    rewritten.append(Statement("store", f"{phi_name} = {first_name};", lines))
                if any(preserve for _, _, _, preserve in items):
                    rewritten.append(
                        Statement(
                            "stack_preserved",
                            f"vm_push(vm, {first_name});",
                            statement.source_lines,
                        )
                    )
            block.statements = rewritten


def propagate_partial_edge_stacks(blocks: list[BasicBlock]) -> None:
    """Move a merge stack read onto missing, non-critical incoming edges."""
    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1

    while True:
        index_by_label = {block.label: index for index, block in enumerate(blocks)}
        predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
        for source_index, block in enumerate(blocks):
            for successor in block.successors:
                target_index = index_by_label.get(successor)
                if target_index is not None:
                    predecessors[target_index].append(source_index)

        candidate: tuple[
            int,
            Statement,
            list[tuple[int, Statement, str]],
            list[int],
        ] | None = None
        for target_index, source_indices in predecessors.items():
            if len(source_indices) < 2 or any(index >= target_index for index in source_indices):
                continue
            consumer = _first_stack_consumer(blocks[target_index])
            if consumer is None:
                continue

            providers: list[tuple[int, Statement, str]] = []
            missing: list[int] = []
            valid = True
            for source_index in source_indices:
                inherited = _forward_stack_providers(blocks, predecessors, source_index)
                if inherited is None:
                    missing.append(source_index)
                    continue
                if len(inherited) != 1:
                    valid = False
                    break
                provider_index, statement, value = inherited[0]
                if (
                    provider_index >= target_index
                    or blocks[provider_index].successors != [blocks[target_index].label]
                ):
                    valid = False
                    break
                providers.append((provider_index, statement, value))
            if not valid or not providers or not missing:
                continue
            if any(blocks[index].successors != [blocks[target_index].label] for index in missing):
                continue
            candidate = target_index, consumer, providers, missing
            break

        if candidate is None:
            return

        target_index, consumer, providers, missing = candidate
        phi_name = f"phi_{phi_index}"
        phi_index += 1
        provider_ids = {id(statement) for _, statement, _ in providers}
        provider_values = {id(statement): value for _, statement, value in providers}
        for block in blocks:
            rewritten: list[Statement] = []
            for statement in block.statements:
                if id(statement) not in provider_ids:
                    rewritten.append(statement)
                    continue
                rewritten.append(
                    Statement(
                        "store",
                        f"{phi_name} = {provider_values[id(statement)]};",
                        statement.source_lines,
                    )
                )
            block.statements = rewritten

        for source_index in missing:
            source = blocks[source_index]
            insert_at = len(source.statements)
            if insert_at and source.statements[-1].kind in {
                "jump",
                "branch",
                "return",
                "jump_dynamic",
            }:
                insert_at -= 1
            source.statements.insert(
                insert_at,
                Statement("store", f"{phi_name} = vm_pop(vm);", consumer.source_lines),
            )

        if consumer.condition is not None:
            consumer.condition = _replace_next_stack_read(consumer.condition, phi_name)
            if consumer.kind == "branch":
                consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
        else:
            consumer.text = _replace_next_stack_read(consumer.text, phi_name)


def fold_partial_edge_stack_runs(blocks: list[BasicBlock]) -> None:
    """Fold a complete multi-value stack run at a one-sided merge.

    A common VM idiom pushes several arguments on one edge and falls through
    to a shared helper that consumes them, while the other edge already has
    those arguments in its incoming frame.  The scalar version above handles
    one value; this pass applies the same transformation to a contiguous run
    without attempting to merge unrelated providers or loops.
    """

    def trailing_run(block: BasicBlock) -> list[tuple[Statement, str]]:
        index = len(block.statements) - 1
        if index >= 0 and block.statements[index].kind in {
            "jump",
            "branch",
            "return",
            "jump_dynamic",
        }:
            index -= 1
        result: list[tuple[Statement, str]] = []
        while index >= 0 and block.statements[index].kind == "stack":
            statement = block.statements[index]
            match = re.fullmatch(r"vm_push\(vm, (.*)\);", statement.text)
            if match is None:
                return []
            result.append((statement, match.group(1)))
            index -= 1
        result.reverse()
        return result

    def replace_reads(text: str, names: list[str]) -> str:
        for name in names:
            text = _replace_first_pop(text, name)
        return text

    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1

    while True:
        index_by_label = {block.label: index for index, block in enumerate(blocks)}
        predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
        for source_index, block in enumerate(blocks):
            for successor in block.successors:
                target_index = index_by_label.get(successor)
                if target_index is not None:
                    predecessors[target_index].append(source_index)

        changed = False
        for target_index, source_indices in predecessors.items():
            if len(source_indices) < 2 or any(index >= target_index for index in source_indices):
                continue
            consumer = _first_stack_consumer(blocks[target_index])
            if consumer is None:
                continue
            consumer_source = consumer.condition or consumer.text
            read_count = _stack_read_count(consumer_source)
            if read_count < 2:
                continue

            provider_index: int | None = None
            provider_run: list[tuple[Statement, str]] = []
            missing: list[int] = []
            valid = True
            for source_index in source_indices:
                source = blocks[source_index]
                run = trailing_run(source)
                if run:
                    if provider_index is not None or len(run) != read_count:
                        valid = False
                        break
                    if source.successors != [blocks[target_index].label]:
                        valid = False
                        break
                    provider_index = source_index
                    provider_run = run
                    continue

                if source.successors != [blocks[target_index].label]:
                    valid = False
                    break
                if any(
                    _blocks_stack_propagation(statement)
                    or _stack_read_count(statement.condition or statement.text)
                    for statement in source.statements
                ):
                    valid = False
                    break
                missing.append(source_index)

            if not valid or provider_index is None or not missing:
                continue

            names = [f"phi_{phi_index + offset}" for offset in range(read_count)]
            phi_index += read_count
            provider_ids = {id(statement) for statement, _ in provider_run}
            rewritten_provider = [
                Statement(
                    "store",
                    f"{name} = {value};",
                    statement.source_lines,
                )
                for name, (statement, value) in zip(names, provider_run)
            ]
            provider = blocks[provider_index]
            provider.statements = [
                statement
                for statement in provider.statements
                if id(statement) not in provider_ids
            ]
            insert_at = len(provider.statements)
            if insert_at and provider.statements[-1].kind in {
                "jump",
                "branch",
                "return",
                "jump_dynamic",
            }:
                insert_at -= 1
            provider.statements[insert_at:insert_at] = rewritten_provider

            # A VM pop reads the newest value first.  The rendered consumer
            # lists arguments oldest-to-newest, so missing edges assign in the
            # reverse order to reconstruct the same scalar argument list.
            for source_index in missing:
                source = blocks[source_index]
                insert_at = len(source.statements)
                if insert_at and source.statements[-1].kind in {
                    "jump",
                    "branch",
                    "return",
                    "jump_dynamic",
                }:
                    insert_at -= 1
                source.statements[insert_at:insert_at] = [
                    Statement("store", f"{name} = vm_pop(vm);", consumer.source_lines)
                    for name in reversed(names)
                ]

            if consumer.condition is not None:
                consumer.condition = replace_reads(consumer.condition, names)
                if consumer.kind == "branch":
                    consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
            else:
                consumer.text = replace_reads(consumer.text, names)
            consumer.source_lines = merge_lines(
                *(statement.source_lines for statement, _ in provider_run),
                consumer.source_lines,
            )
            changed = True
            break

        if not changed:
            return


def fold_edge_stack_argument_selects(blocks: list[BasicBlock]) -> None:
    """Turn one-sided stack arguments at a merge into a scalar phi value.

    A branch can enter a common opcode with one path supplying a value on the
    VM stack while the other path relies on the caller argument slot.  When
    the merge reads exactly one ``vm_arg`` and no path has an unresolved pop,
    assigning a scalar on each edge is equivalent and removes the push/pop
    protocol from the high-level output.
    """
    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1

    while True:
        index_by_label = {block.label: index for index, block in enumerate(blocks)}
        predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
        for source_index, block in enumerate(blocks):
            for successor in block.successors:
                target_index = index_by_label.get(successor)
                if target_index is not None:
                    predecessors[target_index].append(source_index)

        changed = False
        for target_index, source_indices in predecessors.items():
            if len(source_indices) < 2:
                continue
            target = blocks[target_index]
            consumer = _first_stack_consumer(target)
            if consumer is None:
                continue
            consumer_source = consumer.condition or consumer.text
            read = _next_stack_read(consumer_source)
            if read is None or not read.startswith("vm_arg(vm,"):
                continue
            if _stack_read_count(consumer_source) != 1:
                continue

            provider: tuple[int, Statement, str] | None = None
            valid = True
            for source_index in source_indices:
                source = blocks[source_index]
                trailing = _trailing_stack_statement(source)
                if trailing is not None:
                    match = re.fullmatch(r"vm_push\(vm, (.*)\);", trailing.text)
                    if match is None or trailing.kind != "stack":
                        valid = False
                        break
                    if provider is not None:
                        valid = False
                        break
                    provider = (source_index, trailing, match.group(1))
                    continue

                if any(
                    _blocks_stack_propagation(statement)
                    or _stack_read_count(statement.condition or statement.text)
                    for statement in source.statements
                ):
                    valid = False
                    break
            if not valid or provider is None:
                continue

            provider_index, push_statement, value = provider
            if provider_index >= target_index:
                continue
            phi_name = f"edge_phi_{phi_index}"
            phi_index += 1
            push_statement.kind = "store"
            push_statement.text = f"{phi_name} = {value};"

            fallback_indices = set(source_indices).difference({provider_index})
            for source_index in source_indices:
                if source_index == provider_index:
                    continue
                incoming = predecessors[source_index]
                if len(incoming) == 1 and incoming[0] in fallback_indices:
                    continue
                source = blocks[source_index]
                insert_at = len(source.statements)
                if insert_at and source.statements[-1].kind in {
                    "jump",
                    "branch",
                    "return",
                    "jump_dynamic",
                }:
                    insert_at -= 1
                source.statements.insert(
                    insert_at,
                    Statement("store", f"{phi_name} = {read};", consumer.source_lines),
                )

            if consumer.condition is not None:
                consumer.condition = consumer.condition.replace(read, phi_name, 1)
                if consumer.kind == "branch":
                    consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
            else:
                consumer.text = consumer.text.replace(read, phi_name, 1)
            consumer.source_lines = merge_lines(push_statement.source_lines, consumer.source_lines)
            changed = True
            break

        if not changed:
            return


def fold_stack_diamond_selects(blocks: list[BasicBlock]) -> None:
    """Replace a conditional push/missing-stack diamond with a lazy select."""
    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1
    while True:
        index_by_label = {block.label: index for index, block in enumerate(blocks)}
        predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
        for source_index, block in enumerate(blocks):
            for successor in block.successors:
                target_index = index_by_label.get(successor)
                if target_index is not None:
                    predecessors[target_index].append(source_index)

        changed = False
        for target_index, source_indices in predecessors.items():
            if len(source_indices) != 2:
                continue
            consumer = _first_stack_consumer(blocks[target_index])
            if consumer is None:
                continue
            for header_index, provider_index in (
                (source_indices[0], source_indices[1]),
                (source_indices[1], source_indices[0]),
            ):
                if provider_index != header_index + 1:
                    continue
                header = blocks[header_index]
                provider = blocks[provider_index]
                if provider.successors != [blocks[target_index].label]:
                    continue
                control = next(
                    (statement for statement in reversed(header.statements) if statement.kind == "branch"),
                    None,
                )
                if (
                    control is None
                    or control.target != blocks[target_index].label
                    or not control.condition
                    or set(header.successors) != {provider.label, blocks[target_index].label}
                ):
                    continue
                push_statement = _trailing_stack_statement(provider)
                if push_statement is None:
                    continue
                push = re.fullmatch(r"vm_push\(vm, (.*)\);", push_statement.text)
                if push is None:
                    continue
                meaningful = [
                    statement
                    for statement in provider.statements
                    if statement is not push_statement
                    and statement.kind not in {"state", "trace", "jump"}
                ]
                if meaningful:
                    condition_name = f"phi_{phi_index}"
                    phi_index += 1
                    control_index = header.statements.index(control)
                    header.statements.insert(
                        control_index,
                        Statement(
                            "store",
                            f"{condition_name} = {control.condition};",
                            control.source_lines,
                        ),
                    )
                    control.condition = condition_name
                    control.text = f"if ({condition_name}) goto {control.target};"
                    consumer_source = consumer.condition or consumer.text
                    inherited = _next_stack_read(consumer_source)
                    if inherited is None:
                        continue
                    selected = f"{condition_name} ? {inherited} : {push.group(1)}"
                    if consumer.condition is not None:
                        consumer.condition = _replace_next_stack_read(consumer.condition, selected)
                        if consumer.kind == "branch":
                            consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
                    else:
                        consumer.text = _replace_next_stack_read(consumer.text, selected)
                    consumer.source_lines = merge_lines(
                        control.source_lines,
                        push_statement.source_lines,
                        consumer.source_lines,
                    )
                    provider.statements = [
                        statement for statement in provider.statements if statement is not push_statement
                    ]
                    changed = True
                    break

                consumer_source = consumer.condition or consumer.text
                inherited = _next_stack_read(consumer_source)
                if inherited is None:
                    continue
                selected = f"{control.condition} ? {inherited} : {push.group(1)}"
                if consumer.condition is not None:
                    consumer.condition = _replace_next_stack_read(consumer.condition, selected)
                    if consumer.kind == "branch":
                        consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
                else:
                    consumer.text = _replace_next_stack_read(consumer.text, selected)
                consumer.source_lines = merge_lines(
                    control.source_lines,
                    push_statement.source_lines,
                    consumer.source_lines,
                )
                header.statements = [statement for statement in header.statements if statement is not control]
                header.successors = [provider.label]
                provider.statements = [
                    statement for statement in provider.statements if statement is not push_statement
                ]
                changed = True
                break
            if changed:
                break
        if not changed:
            return


STACK_LOCAL_STORE_RE = re.compile(
    r"^store_(?P<type>i8|i16|i32|i64|typed)\(vm_pop\(vm\), vm_pop\(vm\)(?P<tail>.*)\);$"
)
STACK_LOCAL_ADDRESS_RE = re.compile(r"^vm_push\(vm, &(?P<name>local_[0-9A-Fa-f]+)\);$")
STACK_LOCAL_VALUE_RE = re.compile(r"^vm_push\(vm, (?P<name>local_[0-9A-Fa-f]+)\);$")


def _stack_local_pair(block: BasicBlock) -> tuple[str, Statement, Statement] | None:
    for index in range(len(block.statements) - 1):
        address_statement = block.statements[index]
        value_statement = block.statements[index + 1]
        address_match = STACK_LOCAL_ADDRESS_RE.fullmatch(address_statement.text)
        value_match = STACK_LOCAL_VALUE_RE.fullmatch(value_statement.text)
        if address_match and value_match and address_match.group("name") == value_match.group("name"):
            return address_match.group("name"), address_statement, value_statement
    return None


def fold_stack_local_chains(blocks: list[BasicBlock]) -> None:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].append(source_index)

    stores: dict[int, tuple[Statement, re.Match[str]]] = {}
    pairs: dict[int, tuple[str, Statement, Statement]] = {}
    variables: dict[int, str] = {}
    for index, block in enumerate(blocks):
        for statement in block.statements:
            match = STACK_LOCAL_STORE_RE.fullmatch(statement.text)
            if match is not None:
                stores[index] = (statement, match)
                break
        pair = _stack_local_pair(block)
        if pair is not None:
            pairs[index] = pair
            if index in stores:
                variables[index] = pair[0]

    changed = True
    while changed:
        changed = False
        for target_index in stores:
            if target_index in variables:
                continue
            incoming = {
                pairs[source_index][0]
                for source_index in predecessors[target_index]
                if source_index in pairs
            }
            if len(incoming) == 1:
                variables[target_index] = incoming.pop()
                changed = True

    for index, variable in variables.items():
        statement, match = stores[index]
        statement.text = f"{variable} = {variable}{match.group('tail')};"

    for source_index, (variable, address_statement, value_statement) in pairs.items():
        source = blocks[source_index]
        if len(source.successors) != 1:
            continue
        target_index = index_by_label.get(source.successors[0])
        if target_index is None or variables.get(target_index) != variable:
            continue
        target_statement = stores[target_index][0]
        target_statement.source_lines = merge_lines(
            address_statement.source_lines,
            value_statement.source_lines,
            target_statement.source_lines,
        )
        removed = {id(address_statement), id(value_statement)}
        source.statements = [statement for statement in source.statements if id(statement) not in removed]


EXPLICIT_STACK_PUSH_RE = re.compile(r"^vm_push\(vm, (?P<value>.*)\);$")


def _simple_stack_value(text: str) -> bool:
    return re.fullmatch(
        r"(?:&?(?:local_[0-9A-Fa-f]+|global_[0-9A-Fa-f]+|phi_\d+|result_L\d+(?:_\d+)?)"
        r"|-?(?:0[xX][0-9A-Fa-f]+|\d+)|true|false|NULL|\"(?:\\.|[^\"])*\")",
        text.strip(),
    ) is not None


def fold_block_local_stack_values(blocks: list[BasicBlock]) -> None:
    """Turn explicit same-block VM stack traffic into scalar expressions."""
    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1

    for block in blocks:
        pending: list[tuple[int, Statement, str]] = []
        removed: set[int] = set()
        for statement_index, statement in enumerate(block.statements):
            pushed = EXPLICIT_STACK_PUSH_RE.fullmatch(statement.text)
            if pushed is not None and statement.kind in {"stack", "stack_preserved"}:
                pending.append((statement_index, statement, pushed.group("value")))
                continue

            source = statement.condition if statement.condition is not None else statement.text
            pop_count = _stack_read_count(source)
            replacements: list[tuple[str, Statement, bool]] = []
            for _ in range(min(pop_count, len(pending))):
                push_index, push_statement, value = pending.pop()
                between = block.statements[push_index + 1:statement_index]
                direct = _simple_stack_value(value) and all(
                    item.kind in {"stack", "stack_preserved", "state", "trace"}
                    for item in between
                )
                if direct:
                    removed.add(id(push_statement))
                    replacements.append((value, push_statement, True))
                    continue
                name = f"phi_{phi_index}"
                phi_index += 1
                push_statement.kind = "store"
                push_statement.text = f"{name} = {value};"
                replacements.append((name, push_statement, False))

            for value, push_statement, direct in replacements:
                if statement.condition is not None and _stack_read_count(statement.condition):
                    statement.condition = _replace_next_stack_read(statement.condition, value)
                    if statement.kind == "branch":
                        statement.text = f"if ({statement.condition}) goto {statement.target};"
                else:
                    statement.text = _replace_next_stack_read(statement.text, value)
                if direct:
                    statement.source_lines = merge_lines(
                        push_statement.source_lines,
                        statement.source_lines,
                    )

            if _blocks_stack_propagation(statement):
                pending.clear()

        if removed:
            block.statements = [
                statement for statement in block.statements if id(statement) not in removed
            ]


def _trailing_stack_statements(block: BasicBlock) -> list[Statement]:
    end = len(block.statements)
    if end and block.statements[-1].kind in {"jump", "branch", "return", "jump_dynamic"}:
        end -= 1
    stack_end = end
    while (
        stack_end
        and block.statements[stack_end - 1].kind == "store"
        and re.match(r"^phi_\d+ = ", block.statements[stack_end - 1].text)
    ):
        stack_end -= 1
    start = stack_end
    while start and block.statements[start - 1].kind == "stack":
        start -= 1
    return block.statements[start:stack_end]


def fold_stack_returns(blocks: list[BasicBlock]) -> None:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].append(source_index)

    existing_phi_indices = [
        int(match.group(1))
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"\bphi_(\d+)\b", statement.text)
    ]
    phi_index = max(existing_phi_indices, default=0) + 1

    for target_index, target in enumerate(blocks):
        return_statement = next(
            (statement for statement in target.statements if statement.kind == "return"),
            None,
        )
        if return_statement is None or return_statement.text.strip() != "return;":
            continue
        source_indices = predecessors[target_index]
        if not source_indices:
            continue

        providers: list[tuple[int, list[Statement], list[str]]] = []
        missing_provider = False
        unsafe_provider = False
        for source_index in source_indices:
            source = blocks[source_index]
            statements = _trailing_stack_statements(source)
            if not statements:
                missing_provider = True
                continue
            if any(successor != target.label for successor in source.successors):
                unsafe_provider = True
                break
            values: list[str] = []
            for statement in statements:
                match = re.fullmatch(r"vm_push\(vm, (.*)\);", statement.text)
                if match is None:
                    values = []
                    break
                values.append(match.group(1))
            if not values:
                unsafe_provider = True
                break
            providers.append((source_index, statements, values))
        if unsafe_provider or not providers:
            continue

        widths = {len(values) for _, _, values in providers}
        if len(widths) != 1:
            continue
        width = widths.pop()

        if missing_provider:
            if width != 1:
                continue
            expressions = {values[0] for _, _, values in providers}
            if len(expressions) != 1:
                continue
            expression = expressions.pop()
            if not re.fullmatch(
                r"(?:local_[0-9A-Fa-f]+|global_[0-9A-Fa-f]+|-?(?:0[xX][0-9A-Fa-f]+|\d+)|true|false)",
                expression,
            ):
                continue
            provider_lines: tuple[int, ...] = ()
            for source_index, statements, _ in providers:
                provider_lines = merge_lines(provider_lines, *(statement.source_lines for statement in statements))
                removed = {id(statement) for statement in statements}
                blocks[source_index].statements = [
                    statement for statement in blocks[source_index].statements if id(statement) not in removed
                ]
            return_statement.text = f"return {expression};"
            return_statement.source_lines = merge_lines(provider_lines, return_statement.source_lines)
            continue

        phi_names = [f"phi_{phi_index + offset}" for offset in range(width)]
        phi_index += width
        provider_lines: tuple[int, ...] = ()
        for source_index, statements, values in providers:
            replacements: list[Statement] = []
            for name, value, statement in zip(phi_names, values, statements):
                replacements.append(Statement("store", f"{name} = {value};", statement.source_lines))
                provider_lines = merge_lines(provider_lines, statement.source_lines)
            replacement_by_id = {id(statement): replacement for statement, replacement in zip(statements, replacements)}
            blocks[source_index].statements = [
                replacement_by_id.get(id(statement), statement)
                for statement in blocks[source_index].statements
            ]
        if width == 1:
            return_statement.text = f"return {phi_names[0]};"
        else:
            return_statement.text = f"return values({', '.join(phi_names)});"
        return_statement.source_lines = merge_lines(provider_lines, return_statement.source_lines)


def fold_edge_stack_returns(blocks: list[BasicBlock]) -> None:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].append(source_index)

    for target_index, target in enumerate(blocks):
        return_statement = next(
            (
                statement
                for statement in target.statements
                if statement.kind == "return" and statement.text.strip() == "return;"
            ),
            None,
        )
        if return_statement is None:
            continue

        candidates: list[tuple[int, list[Statement], list[str]]] = []
        for source_index in predecessors[target_index]:
            source = blocks[source_index]
            if source.successors != [target.label]:
                continue
            stack_statements = _trailing_stack_statements(source)
            if not stack_statements:
                continue
            values: list[str] = []
            for statement in stack_statements:
                match = re.fullmatch(r"vm_push\(vm, (.*)\);", statement.text)
                if match is None:
                    values = []
                    break
                values.append(match.group(1))
            if values:
                candidates.append((source_index, stack_statements, values))

        # If every path provides the same-width stack, fold_stack_returns
        # handles the common return more compactly.
        if not candidates or len(candidates) == len(predecessors[target_index]):
            continue

        for source_index, stack_statements, values in candidates:
            source = blocks[source_index]
            removed = {id(statement) for statement in stack_statements}
            source.statements = [
                statement for statement in source.statements if id(statement) not in removed
            ]
            control_lines: tuple[int, ...] = ()
            if (
                source.statements
                and source.statements[-1].kind in {"jump", "branch"}
                and source.statements[-1].target == target.label
            ):
                control_lines = source.statements.pop().source_lines
            text = f"return {values[0]};" if len(values) == 1 else f"return values({', '.join(values)});"
            lines = merge_lines(
                *(statement.source_lines for statement in stack_statements),
                control_lines,
                return_statement.source_lines,
            )
            source.statements.append(Statement("return", text, lines))
            source.successors.clear()


LOCAL_ASSIGNMENT_RE = re.compile(r"^(?P<name>local_[0-9A-Fa-f]+)\s*=")
SCRIPT_LOAD_RE = re.compile(
    r"^(?P<name>local_[0-9A-Fa-f]+)\s*=\s*sys1_load_program\((?P<args>.*)\);$"
)
SCRIPT_MODULE_RE = re.compile(r'"(?P<name>[^"\\/]+)\._bp"')
LOCAL_CALL_START_RE = re.compile(r"\b(?P<name>local_[0-9A-Fa-f]+)\(")


def _script_module_from_assignment(text: str) -> tuple[str, str] | None:
    match = SCRIPT_LOAD_RE.fullmatch(text.strip())
    if match is None:
        return None
    modules = list(SCRIPT_MODULE_RE.finditer(match.group("args")))
    if not modules:
        return None
    return match.group("name"), modules[-1].group("name").lower()


def _matching_parenthesis(text: str, open_index: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _rewrite_script_calls(text: str, known_scripts: dict[str, str]) -> str:
    offset = 0
    while True:
        match = LOCAL_CALL_START_RE.search(text, offset)
        if match is None:
            return text
        module_name = known_scripts.get(match.group("name"))
        commands = SCRIPT_COMMAND_NAMES.get(module_name or "")
        open_index = match.end() - 1
        close_index = _matching_parenthesis(text, open_index)
        if commands is None or close_index is None:
            offset = match.end()
            continue
        arguments = split_arguments(text[open_index + 1:close_index])
        selector = parse_int(arguments[-1]) if arguments else None
        command_name = commands.get(selector) if selector is not None else None
        if command_name is None:
            offset = close_index + 1
            continue
        replacement = f"{command_name}({', '.join(arguments[:-1])})"
        text = text[:match.start()] + replacement + text[close_index + 1:]
        offset = match.start() + len(replacement)


def recover_script_dispatch_calls(blocks: list[BasicBlock]) -> None:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, list[int]] = {index: [] for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].append(source_index)

    def transfer(incoming: dict[str, str], block: BasicBlock) -> dict[str, str]:
        current = dict(incoming)
        for statement in block.statements:
            loaded = _script_module_from_assignment(statement.text)
            assignment = LOCAL_ASSIGNMENT_RE.match(statement.text.strip())
            if loaded is not None:
                current[loaded[0]] = loaded[1]
            elif assignment is not None:
                current.pop(assignment.group("name"), None)
        return current

    incoming_maps: list[dict[str, str]] = [{} for _ in blocks]
    outgoing_maps: list[dict[str, str]] = [{} for _ in blocks]
    changed = True
    while changed:
        changed = False
        for index, block in enumerate(blocks):
            sources = predecessors[index]
            if not sources:
                incoming: dict[str, str] = {}
            else:
                common_names = set(outgoing_maps[sources[0]])
                for source in sources[1:]:
                    common_names.intersection_update(outgoing_maps[source])
                incoming = {
                    name: outgoing_maps[sources[0]][name]
                    for name in common_names
                    if all(outgoing_maps[source][name] == outgoing_maps[sources[0]][name] for source in sources)
                }
            outgoing = transfer(incoming, block)
            if incoming != incoming_maps[index] or outgoing != outgoing_maps[index]:
                incoming_maps[index] = incoming
                outgoing_maps[index] = outgoing
                changed = True

    for index, block in enumerate(blocks):
        known_scripts = dict(incoming_maps[index])
        for statement in block.statements:
            loaded = _script_module_from_assignment(statement.text)
            assignment = LOCAL_ASSIGNMENT_RE.match(statement.text.strip())
            statement.text = _rewrite_script_calls(statement.text, known_scripts)
            if statement.condition is not None:
                statement.condition = _rewrite_script_calls(statement.condition, known_scripts)
                if statement.kind == "branch":
                    statement.text = f"if ({statement.condition}) goto {statement.target};"
            if loaded is not None:
                known_scripts[loaded[0]] = loaded[1]
            elif assignment is not None:
                known_scripts.pop(assignment.group("name"), None)


LOCAL_CONSTANT_RE = re.compile(
    r"^(?P<name>local_[0-9A-Fa-f]+)\s*=\s*(?P<value>true|false|-?(?:0[xX][0-9A-Fa-f]+|\d+));$"
)

LOCAL_COPY_RE = re.compile(
    r"^(?P<name>local_[0-9A-Fa-f]+)\s*=\s*"
    r"(?P<value>local_[0-9A-Fa-f]+|true|false|-?(?:0[xX][0-9A-Fa-f]+|\d+));$"
)
LOCAL_NAME_RE = re.compile(r"\blocal_[0-9A-Fa-f]+\b")


def _statement_source(statement: Statement) -> str:
    return statement.condition if statement.condition is not None else statement.text


def _assigned_local(statement: Statement) -> str | None:
    match = LOCAL_ASSIGNMENT_RE.match(statement.text.strip())
    return match.group("name") if match is not None else None


def _local_liveness(blocks: list[BasicBlock]) -> list[set[str]]:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    uses: list[set[str]] = []
    definitions: list[set[str]] = []
    for block in blocks:
        block_uses: set[str] = set()
        block_definitions: set[str] = set()
        for statement in block.statements:
            assigned = _assigned_local(statement)
            text = _statement_source(statement)
            names = set(LOCAL_NAME_RE.findall(text))
            if assigned is not None:
                names.discard(assigned)
            block_uses.update(names.difference(block_definitions))
            if assigned is not None:
                block_definitions.add(assigned)
        uses.append(block_uses)
        definitions.append(block_definitions)

    live_in = [set() for _ in blocks]
    live_out = [set() for _ in blocks]
    changed = True
    while changed:
        changed = False
        for index in range(len(blocks) - 1, -1, -1):
            outgoing: set[str] = set()
            for label in blocks[index].successors:
                successor = index_by_label.get(label)
                if successor is not None:
                    outgoing.update(live_in[successor])
            incoming = uses[index] | outgoing.difference(definitions[index])
            if outgoing != live_out[index] or incoming != live_in[index]:
                live_out[index] = outgoing
                live_in[index] = incoming
                changed = True
    return live_out


def fold_redundant_local_copies(blocks: list[BasicBlock]) -> None:
    """内联不会逃逸的块内标量副本。"""
    address_taken = {
        match.group(0)
        for block in blocks
        for statement in block.statements
        for match in re.finditer(r"(?<=&)local_[0-9A-Fa-f]+\b", _statement_source(statement))
    }
    memory_bases = {
        match.group("name")
        for block in blocks
        for statement in block.statements
        for match in re.finditer(
            r"\b(?:load_(?:i8|i16|i32|i64)|store_(?:i8|i16|i32|i64)|memcpy|memclr|memset)"
            r"\(\s*\(*\s*(?P<name>local_[0-9A-Fa-f]+)\b",
            _statement_source(statement),
        )
    }
    memory_bases.update(
        match.group("name")
        for block in blocks
        for statement in block.statements
        for match in re.finditer(
            r"\bload_typed\([^,]+,\s*\(*\s*(?P<name>local_[0-9A-Fa-f]+)\b",
            _statement_source(statement),
        )
    )

    changed = True
    while changed:
        changed = False
        live_out = _local_liveness(blocks)
        for block_index, block in enumerate(blocks):
            assignment_index = 0
            while assignment_index < len(block.statements):
                assignment = block.statements[assignment_index]
                match = LOCAL_COPY_RE.fullmatch(assignment.text.strip())
                if match is None:
                    assignment_index += 1
                    continue
                target = match.group("name")
                value = match.group("value")
                if target == value or target in address_taken or target in memory_bases:
                    assignment_index += 1
                    continue
                if value.startswith("local_") and value in address_taken:
                    assignment_index += 1
                    continue

                end = len(block.statements)
                for index in range(assignment_index + 1, len(block.statements)):
                    if _assigned_local(block.statements[index]) == target:
                        end = index
                        break
                if end == len(block.statements) and target in live_out[block_index]:
                    assignment_index += 1
                    continue

                consumers = [
                    statement
                    for statement in block.statements[assignment_index + 1:end]
                    if target in LOCAL_NAME_RE.findall(_statement_source(statement))
                ]
                if not consumers:
                    assignment_index += 1
                    continue
                if value.startswith("local_") and any(
                    _assigned_local(statement) == value
                    for statement in block.statements[assignment_index + 1:end]
                ):
                    assignment_index += 1
                    continue

                first_consumer = consumers[0]

                def replace_target(match: re.Match[str]) -> str:
                    return value if match.group(0) == target else match.group(0)

                for consumer in consumers:
                    if consumer.condition is not None:
                        consumer.condition = LOCAL_NAME_RE.sub(
                            replace_target, consumer.condition
                        )
                        if consumer.kind == "branch":
                            consumer.text = f"if ({consumer.condition}) goto {consumer.target};"
                    else:
                        consumer.text = LOCAL_NAME_RE.sub(replace_target, consumer.text)
                first_consumer.source_lines = merge_lines(
                    assignment.source_lines,
                    first_consumer.source_lines,
                )
                del block.statements[assignment_index]
                changed = True


def fold_constant_branches(blocks: list[BasicBlock]) -> None:
    for block in blocks:
        constants: dict[str, str] = {}
        rewritten: list[Statement] = []
        for statement in block.statements:
            assignment = LOCAL_ASSIGNMENT_RE.match(statement.text.strip())
            constant = LOCAL_CONSTANT_RE.fullmatch(statement.text.strip())
            if statement.kind == "branch" and statement.condition:
                condition = statement.condition
                for name, value in constants.items():
                    condition = re.sub(rf"\b{re.escape(name)}\b", value, condition)
                outcome = constant_condition(condition)
                if outcome is False:
                    rewritten.append(
                        Statement(
                            "state",
                            f"/* 到 {statement.target} 的分支恒不成立 */",
                            statement.source_lines,
                        )
                    )
                    continue
                if outcome is True:
                    statement.kind = "jump"
                    statement.condition = None
                    statement.text = f"goto {statement.target};"
                rewritten.append(statement)
                continue
            rewritten.append(statement)
            if assignment is not None:
                if constant is None:
                    constants.pop(assignment.group("name"), None)
                else:
                    constants[constant.group("name")] = constant.group("value")
        block.statements = rewritten
    build_successors(blocks)


def fold_two_stage_terminal_guards(blocks: list[BasicBlock]) -> None:
    """Collapse two conditional entries into one terminal failure region."""
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].add(source_index)

    for source_index in range(len(blocks) - 3):
        guard_index = source_index + 1
        failure_index = guard_index + 1
        source = blocks[source_index]
        guard = blocks[guard_index]
        failure = blocks[failure_index]
        first = next(
            (statement for statement in reversed(source.statements) if statement.kind in {"branch", "jump", "return", "jump_dynamic"}),
            None,
        )
        second = next(
            (statement for statement in reversed(guard.statements) if statement.kind in {"branch", "jump", "return", "jump_dynamic"}),
            None,
        )
        if (
            first is None
            or second is None
            or first.kind != "branch"
            or second.kind != "branch"
            or not first.condition
            or not second.condition
            or first.target != failure.label
        ):
            continue
        success_index = index_by_label.get(second.target or "")
        if success_index is None or success_index <= failure_index:
            continue
        success = blocks[success_index]
        if (
            set(source.successors) != {guard.label, failure.label}
            or set(guard.successors) != {failure.label, success.label}
            or predecessors[guard_index] != {source_index}
            or predecessors[failure_index] != {source_index, guard_index}
        ):
            continue
        guard_meaningful = [
            statement
            for statement in guard.statements
            if statement.kind not in {"state", "trace"} and statement is not second
        ]
        failure_control = next(
            (
                statement
                for statement in reversed(failure.statements)
                if statement.kind in {"branch", "jump", "return", "jump_dynamic"}
            ),
            None,
        )
        if guard_meaningful or failure_control is None or failure_control.kind != "return":
            continue

        first.condition = f"!({first.condition}) && ({second.condition})"
        first.target = success.label
        first.text = f"if ({first.condition}) goto {success.label};"
        first.source_lines = merge_lines(first.source_lines, second.source_lines)
        guard.statements = [statement for statement in guard.statements if statement is not second]
        guard.successors = [failure.label]

    build_successors(blocks)


def fold_repeated_branch_targets(blocks: list[BasicBlock]) -> None:
    """Combine adjacent short-circuit branches that jump to the same target."""
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
    for source_index, block in enumerate(blocks):
        for successor in block.successors:
            target_index = index_by_label.get(successor)
            if target_index is not None:
                predecessors[target_index].add(source_index)

    for source_index in range(len(blocks) - 2):
        guard_index = source_index + 1
        source = blocks[source_index]
        guard = blocks[guard_index]
        first = next(
            (
                statement
                for statement in reversed(source.statements)
                if statement.kind in {"branch", "jump", "return", "jump_dynamic"}
            ),
            None,
        )
        second = next(
            (
                statement
                for statement in reversed(guard.statements)
                if statement.kind in {"branch", "jump", "return", "jump_dynamic"}
            ),
            None,
        )
        if (
            first is None
            or second is None
            or first.kind != "branch"
            or second.kind != "branch"
            or not first.condition
            or not second.condition
            or first.target != second.target
            or predecessors[guard_index] != {source_index}
            or guard.label not in source.successors
        ):
            continue
        if any(
            statement is not second and statement.kind not in {"state", "trace"}
            for statement in guard.statements
        ):
            continue
        fallthrough = blocks[guard_index + 1].label
        if set(guard.successors) != {first.target, fallthrough}:
            continue

        first.condition = f"({first.condition}) || ({second.condition})"
        first.text = f"if ({first.condition}) goto {first.target};"
        first.source_lines = merge_lines(first.source_lines, second.source_lines)
        guard.statements = [statement for statement in guard.statements if statement is not second]
        guard.successors = [fallthrough]

    build_successors(blocks)


def thread_empty_forwarders(blocks: list[BasicBlock]) -> None:
    """Redirect controls through blocks that contain only hidden VM state."""
    forwarders = {
        block.label: block.successors[0]
        for block in blocks
        if len(block.successors) == 1
        and block.successors[0] != block.label
        and not [
            statement
            for statement in block.statements
            if statement.kind not in {"state", "trace"}
        ]
    }

    def resolve(label: str) -> str:
        current = label
        seen: set[str] = set()
        while current in forwarders and current not in seen:
            seen.add(current)
            current = forwarders[current]
        return label if current in seen else current

    for block in blocks:
        rewritten_successors: list[str] = []
        for successor in block.successors:
            target = resolve(successor)
            if target not in rewritten_successors:
                rewritten_successors.append(target)
        block.successors = rewritten_successors
        for statement in block.statements:
            if statement.kind not in {"branch", "jump"} or statement.target is None:
                continue
            target = resolve(statement.target)
            if target == statement.target:
                continue
            statement.target = target
            if statement.kind == "branch" and statement.condition is not None:
                statement.text = f"if ({statement.condition}) goto {target};"
            else:
                statement.text = f"goto {target};"


def entry_parameter_statement_ids(
    blocks: list[BasicBlock],
    function_labels: set[str],
) -> set[int]:
    index_by_label = {block.label: index for index, block in enumerate(blocks)}
    roots = set(function_labels)
    if blocks:
        roots.add(blocks[0].label)
    result: set[int] = set()
    for label in roots:
        root = index_by_label.get(label)
        if root is None:
            continue
        scanning = True
        saw_parameter = False
        for block in blocks[root:]:
            if not scanning:
                break
            for statement in block.statements:
                if statement.kind == "state":
                    continue
                text = statement.text.strip()
                if not saw_parameter and ENTRY_PARAMETER_PRELUDE_RE.match(text):
                    continue
                if ENTRY_LOCAL_PARAM_RE.fullmatch(text):
                    result.add(id(statement))
                    saw_parameter = True
                    continue
                scanning = False
                break
    return result


def eliminate_residual_stack_values(
    blocks: list[BasicBlock],
    function_labels: set[str],
) -> list[str]:
    """Lower dead explicit stack values to ordinary discarded expressions.

    All live edge values have already been folded into expressions or phi
    locals by this point. If a VM pop remains, keep the protocol intact and
    report it; otherwise no residual push has a possible consumer.
    """
    parameter_statements = entry_parameter_statement_ids(blocks, function_labels)
    unresolved = [
        block.label
        for block in blocks
        if any(
            id(statement) not in parameter_statements
            and (
                "vm_pop(vm)" in statement.text
                or "vm_pop(vm)" in (statement.condition or "")
            )
            for statement in block.statements
        )
    ]
    if unresolved:
        return [
            "unresolved stack reads in " + ", ".join(dict.fromkeys(unresolved))
        ]

    for block in blocks:
        for statement in block.statements:
            pushed = EXPLICIT_STACK_PUSH_RE.fullmatch(statement.text.strip())
            if pushed is None:
                continue
            statement.kind = "effect"
            statement.text = f"(void)({pushed.group('value')});"
            statement.node_kind = StatementKind.UNKNOWN
            statement.effects = Effect.PURE
            statement.__post_init__()
    return []


MATERIALIZED_CALL_RESULT_RE = re.compile(
    r"^(?P<name>result_L\d+(?:_\d+)?)\s*=\s*(?P<value>.*);$"
)
MATERIALIZED_CALL_RESULT_NAME_RE = re.compile(r"\bresult_L\d+(?:_\d+)?\b")


def fold_dead_materialized_call_results(blocks: list[BasicBlock]) -> None:
    """内联仅被丢弃的调用结果。"""
    statements = [statement for block in blocks for statement in block.statements]
    block_by_statement = {
        id(statement): block
        for block in blocks
        for statement in block.statements
    }
    references_by_name: dict[str, list[Statement]] = {}
    for statement in statements:
        referenced_names = set(
            MATERIALIZED_CALL_RESULT_NAME_RE.findall(statement.text)
        )
        if statement.condition is not None:
            referenced_names.update(
                MATERIALIZED_CALL_RESULT_NAME_RE.findall(statement.condition)
            )
        for name in referenced_names:
            references_by_name.setdefault(name, []).append(statement)

    for definition in statements:
        assignment = MATERIALIZED_CALL_RESULT_RE.fullmatch(definition.text.strip())
        if assignment is None:
            continue
        name = assignment.group("name")
        references = references_by_name.get(name, [])
        discards = [
            statement
            for statement in references
            if statement is not definition
            and statement.text.strip() == f"(void)({name});"
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


def lift_module(
    assembly: AssemblyModule,
    dynamic_call_hints_by_line: dict[int, tuple[int, int]] | None = None,
    *,
    recover_named_script_calls: bool = True,
) -> Module:
    with engine_scope(assembly.engine):
        return _lift_module(assembly, dynamic_call_hints_by_line,
                            recover_named_script_calls=recover_named_script_calls)


def _lift_module(
    assembly: AssemblyModule,
    dynamic_call_hints_by_line: dict[int, tuple[int, int]] | None = None,
    *,
    recover_named_script_calls: bool = True,
) -> Module:
    call_targets = direct_call_target_labels(assembly)
    base_jump_targets = direct_base_jump_target_labels(assembly)
    call_result_counts_by_line = immediate_call_result_counts(assembly)
    address_targets = dict(base_jump_targets)
    address_targets.update(call_targets)
    raw_blocks = partition_blocks(assembly, address_targets)
    targeted_labels = {
        arg.strip()
        for _, _, instructions, _ in raw_blocks
        for instruction in instructions
        if instruction.opcode in BLOCK_TERMINATORS
        for arg in instruction.args
        if re.fullmatch(r"L[0-9A-Fa-f]+", arg.strip())
    }
    targeted_labels.update(
        instruction.args[0].strip()
        for _, _, instructions, _ in raw_blocks
        for instruction in instructions
        if instruction.opcode == "push_offset"
        and instruction.args
        and re.fullmatch(r"L[0-9A-Fa-f]+", instruction.args[0].strip())
    )
    targeted_labels.update(call_targets.values())
    targeted_labels.update(base_jump_targets.values())

    def lift_blocks(
        direct_argument_counts: dict[str, int] | None = None,
        direct_result_counts: dict[str, int] | None = None,
    ) -> tuple[list[BasicBlock], LiftStatistics]:
        stats = LiftStatistics()
        blocks: list[BasicBlock] = []
        carried_stack: list[Expression] = []
        for index, (label, source_line, instructions, synthetic) in enumerate(raw_blocks):
            block = BasicBlock(
                label=label,
                source_line=source_line,
                synthetic=synthetic,
                terminator_opcode=instructions[-1].opcode if instructions else None,
            )
            lifter = BlockLifter(
                block,
                stats,
                carried_stack,
                call_targets,
                getattr(call_targets, "by_line", None),
                base_jump_targets,
                getattr(base_jump_targets, "by_line", None),
                call_result_counts_by_line,
                direct_argument_counts,
                direct_result_counts,
                dynamic_call_hints_by_line,
            )
            for instruction in instructions:
                lifter.lift(instruction)
            last_opcode = instructions[-1].opcode if instructions else None
            next_label = raw_blocks[index + 1][0] if index + 1 < len(raw_blocks) else None
            can_carry = (
                next_label is not None
                and last_opcode not in BLOCK_TERMINATORS
                and next_label not in targeted_labels
            )
            if can_carry:
                carried_stack = list(lifter.stack)
            else:
                lifter.sync_stack()
                carried_stack = []
            blocks.append(block)
        return blocks, stats

    function_labels = set(call_targets.values())
    argument_counts: dict[str, int] = {}
    signature_result_counts: dict[str, int] = {}
    blocks: list[BasicBlock] = []
    stats = LiftStatistics()
    converged = False
    for _ in range(16):
        blocks, stats = lift_blocks(argument_counts, signature_result_counts)
        build_successors(blocks)
        reattach_orphan_stack_prefixes(blocks)
        function_labels.update(
            target
            for block in blocks
            for statement in block.statements
            for target in [_direct_call_target(statement.text)]
            if target is not None
        )
        next_argument_counts = recover_entry_stack_parameters(blocks, function_labels)
        next_result_counts = infer_direct_call_result_counts(blocks, function_labels)
        if (
            next_argument_counts == argument_counts
            and next_result_counts == signature_result_counts
        ):
            converged = True
            break
        argument_counts = next_argument_counts
        signature_result_counts = next_result_counts

    if not converged:
        blocks, stats = lift_blocks(argument_counts, signature_result_counts)
        build_successors(blocks)
        reattach_orphan_stack_prefixes(blocks)
        argument_counts = recover_entry_stack_parameters(blocks, function_labels)

    result_counts = infer_direct_call_result_counts(blocks, function_labels)
    fold_direct_multi_store_results(blocks, result_counts)
    fold_multi_call_results(blocks, result_counts)
    fold_store_multi_call_results(blocks)
    fold_deferred_call_results(blocks)
    fold_call_results(blocks, result_counts)
    fold_delayed_call_results(blocks, result_counts)
    propagate_single_edge_stacks(blocks)
    propagate_phi_stacks(blocks)
    propagate_partial_edge_stacks(blocks)
    fold_partial_edge_stack_runs(blocks)
    fold_edge_stack_argument_selects(blocks)
    fold_stack_diamond_selects(blocks)
    fold_stack_local_chains(blocks)
    fold_stack_returns(blocks)
    fold_edge_stack_returns(blocks)
    # Phi and return folding can turn a path-dependent provider into a normal
    # single-edge value.  A second pass removes those newly exposed stack ops.
    propagate_single_edge_stacks(blocks)
    fold_stack_local_chains(blocks)
    fold_stack_returns(blocks)
    fold_edge_stack_returns(blocks)
    result_counts = infer_direct_call_result_counts(blocks, function_labels)
    fold_direct_multi_store_results(blocks, result_counts)
    fold_multi_call_results(blocks, result_counts)
    fold_call_results(blocks, result_counts)
    fold_delayed_call_results(blocks, result_counts)
    # Calls with path-dependent return widths cannot be typed statically, but
    # their first immediate consumer can still use the scalar result directly.
    fold_call_results(blocks)
    fold_delayed_call_results(blocks)
    # Result folding exposes additional cross-edge consumers.  Re-run the
    # complete stack dataflow sequence so those values can become scalars too.
    propagate_single_edge_stacks(blocks)
    propagate_phi_stacks(blocks)
    propagate_partial_edge_stacks(blocks)
    fold_partial_edge_stack_runs(blocks)
    fold_edge_stack_argument_selects(blocks)
    fold_stack_diamond_selects(blocks)
    propagate_single_edge_stacks(blocks)
    fold_block_local_stack_values(blocks)
    if recover_named_script_calls:
        recover_script_dispatch_calls(blocks)
    fold_constant_branches(blocks)
    fold_redundant_local_copies(blocks)
    fold_constant_branches(blocks)
    thread_empty_forwarders(blocks)
    fold_repeated_branch_targets(blocks)
    fold_two_stage_terminal_guards(blocks)
    # Late CFG folding removes edges and exposes stack providers that were
    # ambiguous during the first data-flow sequence.
    propagate_single_edge_stacks(blocks)
    propagate_phi_stacks(blocks)
    propagate_partial_edge_stacks(blocks)
    fold_partial_edge_stack_runs(blocks)
    fold_edge_stack_argument_selects(blocks)
    fold_stack_diamond_selects(blocks)
    propagate_single_edge_stacks(blocks)
    fold_block_local_stack_values(blocks)
    fold_direct_local_helper_stores(blocks)
    fold_range_stack_guards(blocks)
    # The range fold removes the unresolved pops that previously blocked a
    # one-sided caller-argument override at the following merge.
    fold_edge_stack_argument_selects(blocks)
    fold_conditional_assignment_dispatches(blocks)
    fold_relative_assignment_ladders(blocks)
    argument_counts = recover_entry_stack_parameters(blocks, function_labels)
    normalize_direct_call_arguments(blocks, argument_counts)
    eliminate_residual_stack_values(blocks, function_labels)
    fold_dead_materialized_call_results(blocks)
    analyze_stack_ssa(blocks)
    # CFG joins are retained on the IR blocks for inspection.  The SSA pass
    # may observe function-entry reads or dead VM bookkeeping that the C
    # emitter intentionally lowers away; only unresolved reads that survive
    # lowering are user-visible diagnostics.
    diagnostics = [f"line {item.line_no}: {item.message}: {item.text.strip()}" for item in assembly.diagnostics]
    cfg = ControlFlowGraph.build(blocks)
    return Module(
        source_path=assembly.path,
        blocks=blocks,
        diagnostics=diagnostics,
        stats=stats,
        cfg=cfg,
        stack_diagnostics=[],
    )
