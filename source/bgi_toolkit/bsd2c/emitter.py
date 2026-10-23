from __future__ import annotations

from dataclasses import dataclass, field
import re

from .builtin_functions import builtin_resolver_scope, call_opcode, load_builtin_resolver
from .integer_semantics import trunc_div, trunc_mod

from .ir import (
    AddressOf,
    ArgRef,
    AssignStmt,
    BasicBlock,
    BasePointer,
    BinaryExpr,
    BranchStmt,
    CallExpr,
    CommentStmt,
    DerefExpr,
    Expr,
    ExprStmt,
    Function,
    InternalLabelCallStmt,
    IntLiteral,
    JumpStmt,
    LabelRef,
    LabelStmt,
    LocalVar,
    Module,
    NamedVar,
    ParamRef,
    RawStmt,
    RegisteredScriptJumpStmt,
    RegisterExceptionStmt,
    ReturnStmt,
    SetBaseStmt,
    StackAllocStmt,
    StringLiteral,
    UnaryExpr,
    UnknownExpr,
    UnregisterExceptionStmt,
)


@dataclass(slots=True)
class CfgInfo:
    entry_label: str
    block_by_label: dict[str, BasicBlock]
    index_by_label: dict[str, int]
    predecessors: dict[str, set[str]]
    dominators: dict[str, set[str]]
    postdominators: dict[str, set[str]]
    immediate_postdominators: dict[str, str | None]
    back_edges: set[tuple[str, str]]
    natural_loops: dict[str, set[str]]


class RegionNode:
    pass


@dataclass(slots=True)
class SequenceRegion(RegionNode):
    children: list[RegionNode] = field(default_factory=list)


@dataclass(slots=True)
class BlockRegion(RegionNode):
    index: int
    show_label: bool = True
    suppress_terminal_jump_to: str | None = None
    loop_header_label: str | None = None
    loop_exit_label: str | None = None
    switch_exit_label: str | None = None


@dataclass(slots=True)
class BlockBodyRegion(RegionNode):
    index: int
    show_label: bool = True


@dataclass(slots=True)
class GotoRegion(RegionNode):
    target: str


@dataclass(slots=True)
class IfRegion(RegionNode):
    header_index: int
    condition: Expr
    then_region: RegionNode
    else_region: RegionNode | None = None
    show_label: bool = True


@dataclass(slots=True)
class DoWhileRegion(RegionNode):
    condition: Expr
    body_region: RegionNode
    exit_label: str | None = None
    header_index: int | None = None


@dataclass(slots=True)
class WhileRegion(RegionNode):
    header_index: int
    condition: Expr
    body_region: RegionNode
    exit_label: str | None = None
    show_label: bool = True


@dataclass(slots=True)
class InfiniteLoopRegion(RegionNode):
    header_index: int
    body_region: RegionNode
    show_label: bool = True


@dataclass(slots=True)
class SwitchCaseRegion:
    value: int | str
    body: RegionNode


@dataclass(slots=True)
class SwitchRegion(RegionNode):
    header_index: int
    subject: Expr
    cases: list[SwitchCaseRegion]
    default_region: RegionNode | None = None
    exit_label: str | None = None
    show_label: bool = True


@dataclass(slots=True)
class SelectColorLoopRegion(RegionNode):
    header_index: int
    cases: list[tuple[Expr, int]]
    exit_index: int
    show_label: bool = True


@dataclass(slots=True)
class InlineExitRegion(RegionNode):
    block_index: int


@dataclass(slots=True)
class PositiveCaseChainRegion(RegionNode):
    header_index: int
    cases: list[tuple[Expr, int]]
    suppress_terminal_jump_to: str | None = None
    show_label: bool = True


@dataclass(slots=True)
class EmptyRegion(RegionNode):
    pass


def escape_string(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r'\"')


def canonical_callee_name(name: str) -> str:
    return load_builtin_resolver().resolve(name) or (name[2:] if name.startswith("f_") else name)


def is_synthetic_label(label: str) -> bool:
    return re.fullmatch(r".+(?:\$bb|__bb)\d+", label) is not None


NEGATED_COMPARE_OPS = {
    "==": "!=",
    "!=": "==",
    "<": ">=",
    "<=": ">",
    ">": "<=",
    ">=": "<",
}

SWAPPED_COMPARE_OPS = {
    "==": "==",
    "!=": "!=",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}

# _SelectEx is a legacy script helper, rather than an engine display name.
SELECT_SETUP_CALLEES = {"_SelectEx", "f_170"}

BOOLEAN_RESULT_BINARY_OPS = {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}
COMPARE_BINARY_OPS = {"==", "!=", "<", "<=", ">", ">="}
ORDERING_COMPARE_OPS = {"<", "<=", ">", ">="}

BINARY_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "<": 7,
    "<=": 7,
    ">": 7,
    ">=": 7,
    "<<": 8,
    ">>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
}

PREFIX_PRECEDENCE = 11
ATOM_PRECEDENCE = 12

BITWISE_BINARY_OPS = {"&", "|", "^"}
BITWISE_CHILD_CLARITY_OPS = {"+", "-", "<<", ">>", "&"}


def evaluate_int_constant(expr: Expr) -> int | None:
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, UnaryExpr):
        value = evaluate_int_constant(expr.value)
        if value is None:
            return None
        if expr.op == "+":
            return value
        if expr.op == "-":
            return -value
        if expr.op == "~":
            return ~value
        if expr.op == "!":
            return int(not value)
        return None
    if not isinstance(expr, BinaryExpr):
        return None

    left = evaluate_int_constant(expr.left)
    right = evaluate_int_constant(expr.right)
    if left is None or right is None:
        return None

    if expr.op == "+":
        return left + right
    if expr.op == "-":
        return left - right
    if expr.op == "*":
        return left * right
    if expr.op == "/" and right != 0:
        return trunc_div(left, right)
    if expr.op == "%" and right != 0:
        return trunc_mod(left, right)
    if expr.op == "<<":
        return left << right
    if expr.op == ">>":
        return left >> right
    if expr.op == "&":
        return left & right
    if expr.op == "|":
        return left | right
    if expr.op == "^":
        return left ^ right
    if expr.op == "==":
        return int(left == right)
    if expr.op == "!=":
        return int(left != right)
    if expr.op == "<":
        return int(left < right)
    if expr.op == "<=":
        return int(left <= right)
    if expr.op == ">":
        return int(left > right)
    if expr.op == ">=":
        return int(left >= right)
    if expr.op == "&&":
        return int(bool(left) and bool(right))
    if expr.op == "||":
        return int(bool(left) or bool(right))
    return None


def pointer_element_size(type_name: str) -> int | None:
    normalized = type_name.replace(" ", "")
    if not normalized.endswith("*"):
        return None
    base_type = normalized[:-1]
    return {
        "bool": 1,
        "char": 1,
        "long": 4,
    }.get(base_type)


def make_c_cast(type_name: str, value: Expr) -> UnknownExpr:
    rendered_value = render_expr_with_precedence(value, PREFIX_PRECEDENCE)
    return UnknownExpr(f"({type_name})({rendered_value})")


def simplify_sign_extension(expr: BinaryExpr) -> Expr | None:
    if expr.op != ">>":
        return None

    right_shift = evaluate_int_constant(expr.right)
    if right_shift is None:
        return None

    left_expr = expr.left
    if not isinstance(left_expr, BinaryExpr) or left_expr.op != "<<":
        return None

    left_shift = evaluate_int_constant(left_expr.right)
    if left_shift is None:
        return None

    value = left_expr.left
    if left_shift == 16 and right_shift == 16:
        return make_c_cast("short", value)
    if left_shift == 24 and right_shift == 24:
        return make_c_cast("signed char", value)
    if left_shift == 16 and right_shift == 24:
        return make_c_cast("signed char", simplify_expr(BinaryExpr(">>", value, IntLiteral(8))))
    return None


def simplify_sign_bit_test(left: Expr, right: Expr) -> Expr | None:
    constant = evaluate_int_constant(right)
    if constant != 1:
        return None
    if not isinstance(left, BinaryExpr) or left.op != ">>":
        return None

    shift = evaluate_int_constant(left.right)
    if shift != 31:
        return None
    return simplify_expr(BinaryExpr("<", left.left, IntLiteral(0)))


def match_named_array_element(address: Expr) -> str | None:
    def array_base_name(expr: Expr) -> str | None:
        if isinstance(expr, AddressOf) and isinstance(expr.target, NamedVar):
            match = re.fullmatch(r"(.+)\[0\]", expr.target.name)
            if match is not None:
                return match.group(1)
        return None

    base_name = array_base_name(address)
    if base_name is not None:
        return f"{base_name}[0]"

    if isinstance(address, BinaryExpr) and address.op == "+":
        left_base = array_base_name(address.left)
        right_base = array_base_name(address.right)
        if left_base is not None:
            delta = evaluate_int_constant(address.right)
            if delta is not None and delta % 4 == 0:
                return f"{left_base}[{delta // 4}]"
        if right_base is not None:
            delta = evaluate_int_constant(address.left)
            if delta is not None and delta % 4 == 0:
                return f"{right_base}[{delta // 4}]"
    return None


def extract_local_array_index_expr(expr: Expr, stride: int = 4) -> Expr | None:
    constant = evaluate_int_constant(expr)
    if constant is not None:
        if constant % stride == 0:
            return IntLiteral(constant // stride)
        return None

    if isinstance(expr, BinaryExpr) and expr.op == "*":
        for factor_expr, value_expr in ((expr.left, expr.right), (expr.right, expr.left)):
            factor = evaluate_int_constant(factor_expr)
            if factor is None or factor % stride != 0:
                continue
            quotient = factor // stride
            if quotient == 1:
                return value_expr
            return simplify_expr(BinaryExpr("*", value_expr, IntLiteral(quotient)))

    if isinstance(expr, BinaryExpr) and expr.op == "<<":
        shift = evaluate_int_constant(expr.right)
        stride_shift = stride.bit_length() - 1
        if shift is not None and 1 << stride_shift == stride and shift >= stride_shift:
            remaining_shift = shift - stride_shift
            if remaining_shift == 0:
                return expr.left
            return simplify_expr(BinaryExpr("<<", expr.left, IntLiteral(remaining_shift)))

    if isinstance(expr, BinaryExpr) and expr.op in {"+", "-"}:
        left = extract_local_array_index_expr(expr.left, stride)
        right = extract_local_array_index_expr(expr.right, stride)
        if left is not None and right is not None:
            return simplify_expr(BinaryExpr(expr.op, left, right))
    return None


def render_local_array_element(address: Expr) -> str | None:
    matched = match_local_array_index(address)
    if matched is None:
        return None
    offset, index_expr = matched
    if index_expr is None:
        return None
    rendered_index = render_expr_with_precedence(index_expr)
    return f"v{offset}[{rendered_index}]"


def simplify_expr(expr: Expr) -> Expr:
    if isinstance(expr, AddressOf):
        target = simplify_expr(expr.target)
        if isinstance(target, DerefExpr):
            return simplify_expr(target.address)
        return AddressOf(target)
    if isinstance(expr, DerefExpr):
        address = simplify_expr(expr.address)
        array_element = match_named_array_element(address)
        if array_element is not None:
            return NamedVar(array_element)
        if isinstance(address, AddressOf):
            return simplify_expr(address.target)
        return DerefExpr(address, expr.size)
    if isinstance(expr, UnaryExpr):
        value = simplify_expr(expr.value)
        if expr.op == "!":
            if isinstance(value, UnaryExpr) and value.op == "!":
                return simplify_expr(BinaryExpr("!=", value.value, IntLiteral(0)))
            if isinstance(value, BinaryExpr) and value.op in NEGATED_COMPARE_OPS:
                return simplify_expr(BinaryExpr(NEGATED_COMPARE_OPS[value.op], value.left, value.right))
        constant = evaluate_int_constant(UnaryExpr(expr.op, value))
        if constant is not None:
            return IntLiteral(constant)
        return UnaryExpr(expr.op, value)
    if isinstance(expr, BinaryExpr):
        left = simplify_expr(expr.left)
        right = simplify_expr(expr.right)
        constant = evaluate_int_constant(BinaryExpr(expr.op, left, right))
        if constant is not None:
            return IntLiteral(constant)

        sign_extension = simplify_sign_extension(BinaryExpr(expr.op, left, right))
        if sign_extension is not None:
            return sign_extension

        if expr.op == "&":
            sign_bit_test = simplify_sign_bit_test(left, right)
            if sign_bit_test is not None:
                return sign_bit_test
            sign_bit_test = simplify_sign_bit_test(right, left)
            if sign_bit_test is not None:
                return sign_bit_test

        if expr.op in {"==", "!=", "<", "<=", ">", ">="}:
            left_const = evaluate_int_constant(left)
            right_const = evaluate_int_constant(right)
            if left_const is not None and right_const is None:
                swap_op = {
                    "==": "==",
                    "!=": "!=",
                    "<": ">",
                    "<=": ">=",
                    ">": "<",
                    ">=": "<=",
                }[expr.op]
                return simplify_expr(BinaryExpr(swap_op, right, left))

        if expr.op == "+":
            if evaluate_int_constant(left) == 0:
                return right
            if evaluate_int_constant(right) == 0:
                return left
        if expr.op == "-":
            if evaluate_int_constant(right) == 0:
                return left
        if expr.op == "*":
            if evaluate_int_constant(left) == 0 or evaluate_int_constant(right) == 0:
                return IntLiteral(0)
            if evaluate_int_constant(left) == 1:
                return right
            if evaluate_int_constant(right) == 1:
                return left
            if evaluate_int_constant(left) == -1:
                return simplify_expr(UnaryExpr("-", right))
            if evaluate_int_constant(right) == -1:
                return simplify_expr(UnaryExpr("-", left))
        if expr.op == "/" and evaluate_int_constant(right) == 1:
            return left
        if expr.op in {"<<", ">>"} and evaluate_int_constant(right) == 0:
            return left
        if expr.op == "&&":
            if evaluate_int_constant(left) == 1:
                return simplify_expr(BinaryExpr("!=", right, IntLiteral(0)))
            if evaluate_int_constant(right) == 1:
                return simplify_expr(BinaryExpr("!=", left, IntLiteral(0)))
        if expr.op == "||":
            if evaluate_int_constant(left) == 0:
                return simplify_expr(BinaryExpr("!=", right, IntLiteral(0)))
            if evaluate_int_constant(right) == 0:
                return simplify_expr(BinaryExpr("!=", left, IntLiteral(0)))

        return BinaryExpr(expr.op, left, right)
    if isinstance(expr, CallExpr):
        return CallExpr(expr.callee, tuple(simplify_expr(arg) for arg in expr.args), expr.builtin_opcode)
    return expr


def expr_key(expr: Expr) -> str:
    return render_expr(expr)


def expr_precedence(expr: Expr) -> int:
    if isinstance(expr, BinaryExpr):
        return BINARY_PRECEDENCE.get(expr.op, 0)
    if isinstance(expr, (AddressOf, DerefExpr, UnaryExpr)):
        return PREFIX_PRECEDENCE
    return ATOM_PRECEDENCE


def wrap_rendered_expr(text: str, current_precedence: int, parent_precedence: int, wrap_on_equal: bool = False) -> str:
    if current_precedence < parent_precedence:
        return f"({text})"
    if wrap_on_equal and current_precedence == parent_precedence:
        return f"({text})"
    return text


def should_wrap_child_for_clarity(parent_expr: Expr, child_expr: Expr) -> bool:
    return (
        isinstance(parent_expr, BinaryExpr)
        and parent_expr.op in BITWISE_BINARY_OPS
        and isinstance(child_expr, BinaryExpr)
        and child_expr.op in BITWISE_CHILD_CLARITY_OPS
    )


LEFT_CHAIN_CLARITY_OPS = {"==", "!=", "<", "<=", ">", ">=", "-", "/", "%"}


def should_wrap_left_equal_precedence_child(parent_expr: Expr, child_expr: Expr) -> bool:
    if not isinstance(parent_expr, BinaryExpr) or not isinstance(child_expr, BinaryExpr):
        return False
    if expr_precedence(parent_expr) != expr_precedence(child_expr):
        return False
    return parent_expr.op in LEFT_CHAIN_CLARITY_OPS or child_expr.op in LEFT_CHAIN_CLARITY_OPS


def expr_yields_boolean(expr: Expr) -> bool:
    simplified = simplify_expr(expr)
    if isinstance(simplified, UnaryExpr) and simplified.op == "!":
        return True
    return isinstance(simplified, BinaryExpr) and simplified.op in BOOLEAN_RESULT_BINARY_OPS


def should_numericize_boolean_child(parent_expr: Expr, child_expr: Expr) -> bool:
    return isinstance(parent_expr, BinaryExpr) and parent_expr.op in COMPARE_BINARY_OPS and expr_yields_boolean(child_expr)


def render_boolean_numeric_expr(
    expr: Expr,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
) -> str:
    rendered = render_expr_with_precedence(
        expr,
        array_locals=array_locals,
        pointer_param_sizes=pointer_param_sizes,
    )
    return f"(({rendered}) ? 1 : 0)"


def select_compare_chain_middle(expr: BinaryExpr) -> Expr | None:
    left_constant = evaluate_int_constant(expr.left)
    right_constant = evaluate_int_constant(expr.right)
    if left_constant is not None and right_constant is None:
        return expr.right
    if right_constant is not None and left_constant is None:
        return expr.left
    return None


def rewrite_script_compare_chain(expr: BinaryExpr) -> Expr | None:
    if expr.op not in ORDERING_COMPARE_OPS:
        return None

    if isinstance(expr.left, BinaryExpr) and expr.left.op in ORDERING_COMPARE_OPS and not expr_yields_boolean(expr.right):
        middle = select_compare_chain_middle(expr.left)
        if middle is not None:
            return simplify_expr(BinaryExpr("&&", expr.left, BinaryExpr(expr.op, middle, expr.right)))

    if isinstance(expr.right, BinaryExpr) and expr.right.op in ORDERING_COMPARE_OPS and not expr_yields_boolean(expr.left):
        middle = select_compare_chain_middle(expr.right)
        if middle is not None:
            return simplify_expr(BinaryExpr("&&", BinaryExpr(expr.op, expr.left, middle), expr.right))

    return None


def render_prefixed_expr(
    prefix: str,
    value: Expr,
    parent_precedence: int,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
) -> str:
    rendered_value = render_expr_with_precedence(
        value,
        PREFIX_PRECEDENCE,
        array_locals=array_locals,
        pointer_param_sizes=pointer_param_sizes,
    )
    if isinstance(value, UnaryExpr):
        rendered_value = f"({rendered_value})"
    return wrap_rendered_expr(f"{prefix}{rendered_value}", PREFIX_PRECEDENCE, parent_precedence)


def render_pointer_param_element(
    address: Expr,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
) -> str | None:
    if not pointer_param_sizes:
        return None

    if not isinstance(address, BinaryExpr) or address.op != "+":
        return None

    candidates = ((address.left, address.right), (address.right, address.left))
    for base_expr, delta_expr in candidates:
        if not isinstance(base_expr, ParamRef):
            continue
        stride = pointer_param_sizes.get(base_expr.name)
        if stride is None:
            continue
        index_expr = extract_local_array_index_expr(simplify_expr(delta_expr), stride)
        if index_expr is None:
            continue
        rendered_index = render_expr_with_precedence(
            index_expr,
            array_locals=array_locals,
            pointer_param_sizes=pointer_param_sizes,
        )
        return f"{base_expr.name}[{rendered_index}]"
    return None


def render_expr_with_precedence(
    expr: Expr,
    parent_precedence: int = 0,
    is_right_child: bool = False,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
) -> str:
    expr = simplify_expr(expr)
    if isinstance(expr, IntLiteral):
        return str(expr.value)
    if isinstance(expr, StringLiteral):
        return f'"{escape_string(expr.value)}"'
    if isinstance(expr, LocalVar):
        if array_locals and expr.offset in array_locals:
            return f"v{expr.offset}[0]"
        return f"v{expr.offset}"
    if isinstance(expr, NamedVar):
        return expr.name
    if isinstance(expr, ArgRef):
        return f"Arg({expr.size}, {expr.index})"
    if isinstance(expr, ParamRef):
        return expr.name
    if isinstance(expr, LabelRef):
        return expr.name
    if isinstance(expr, BasePointer):
        return "BASE"
    if isinstance(expr, AddressOf):
        if isinstance(expr.target, LocalVar) and array_locals and expr.target.offset in array_locals:
            return wrap_rendered_expr(f"v{expr.target.offset}", ATOM_PRECEDENCE, parent_precedence)
        return render_prefixed_expr("&", expr.target, parent_precedence, array_locals, pointer_param_sizes)
    if isinstance(expr, DerefExpr):
        local_array_element = render_local_array_element(expr.address)
        if local_array_element is not None:
            return wrap_rendered_expr(local_array_element, ATOM_PRECEDENCE, parent_precedence)
        pointer_array_element = render_pointer_param_element(expr.address, array_locals, pointer_param_sizes)
        if pointer_array_element is not None:
            return wrap_rendered_expr(pointer_array_element, ATOM_PRECEDENCE, parent_precedence)
        return render_prefixed_expr("*", expr.address, parent_precedence, array_locals, pointer_param_sizes)
    if isinstance(expr, UnaryExpr):
        return render_prefixed_expr(expr.op, expr.value, parent_precedence, array_locals, pointer_param_sizes)
    if isinstance(expr, BinaryExpr):
        compare_chain = rewrite_script_compare_chain(expr)
        if compare_chain is not None:
            return render_expr_with_precedence(
                compare_chain,
                parent_precedence,
                is_right_child=is_right_child,
                array_locals=array_locals,
                pointer_param_sizes=pointer_param_sizes,
            )
        precedence = expr_precedence(expr)
        left_numericized = should_numericize_boolean_child(expr, expr.left)
        right_numericized = should_numericize_boolean_child(expr, expr.right)
        if left_numericized:
            left = render_boolean_numeric_expr(expr.left, array_locals, pointer_param_sizes)
        else:
            left = render_expr_with_precedence(
                expr.left,
                precedence,
                array_locals=array_locals,
                pointer_param_sizes=pointer_param_sizes,
            )
        if right_numericized:
            right = render_boolean_numeric_expr(expr.right, array_locals, pointer_param_sizes)
        else:
            right = render_expr_with_precedence(
                expr.right,
                precedence,
                is_right_child=True,
                array_locals=array_locals,
                pointer_param_sizes=pointer_param_sizes,
            )
        if not left_numericized and should_wrap_left_equal_precedence_child(expr, expr.left):
            left = f"({left})"
        if not left_numericized and should_wrap_child_for_clarity(expr, expr.left):
            left = f"({left})"
        if not right_numericized and should_wrap_child_for_clarity(expr, expr.right):
            right = f"({right})"
        return wrap_rendered_expr(f"{left} {expr.op} {right}", precedence, parent_precedence, is_right_child)
    if isinstance(expr, CallExpr):
        return f"{expr.callee}({', '.join(render_expr_with_precedence(arg, array_locals=array_locals, pointer_param_sizes=pointer_param_sizes) for arg in expr.args)})"
    if isinstance(expr, UnknownExpr):
        return expr.text
    return "UNKNOWN"


def extract_switch_case(expr: Expr) -> tuple[Expr, int] | None:
    simplified = simplify_expr(expr)
    if not isinstance(simplified, BinaryExpr) or simplified.op != "==":
        return None
    left_constant = evaluate_int_constant(simplified.left)
    right_constant = evaluate_int_constant(simplified.right)
    if left_constant is not None and right_constant is None:
        return simplified.right, left_constant
    if right_constant is not None and left_constant is None:
        return simplified.left, right_constant
    return None


def combine_conditions_with_or(conditions: list[Expr]) -> Expr:
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = BinaryExpr("||", combined, condition)
    return combined


def combine_conditions_with_and(conditions: list[Expr]) -> Expr:
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = BinaryExpr("&&", combined, condition)
    return combined


def match_select_setup_sources(statements: list[object], index: int) -> tuple[int, list[StringLiteral], bool, StringLiteral | None] | None:
    """Match a real selection buffer and retain literal origins for text round trips."""
    def match_select_tail(cursor: int, options: list[StringLiteral], include_initialize: bool) -> tuple[int, list[StringLiteral], bool, StringLiteral | None] | None:
        if not options or cursor + 1 >= len(statements):
            return None

        select_stmt = statements[cursor]
        set_base_stmt = statements[cursor + 1]
        if not isinstance(select_stmt, ExprStmt) or not isinstance(select_stmt.value, CallExpr):
            return None
        select_call = select_stmt.value
        if (call_opcode(select_call) or select_call.callee) not in SELECT_SETUP_CALLEES or len(select_call.args) != 3:
            return None

        option_count = evaluate_int_constant(select_call.args[0])
        if option_count != len(options):
            return None

        outline: StringLiteral | None = None
        outline_value = select_call.args[2]
        if isinstance(outline_value, StringLiteral):
            outline = outline_value if outline_value.value else None
        elif evaluate_int_constant(outline_value) != 0:
            return None

        select_buffer = select_call.args[1]
        if not isinstance(select_buffer, AddressOf) or not isinstance(select_buffer.target, LocalVar) or select_buffer.target.offset != 0:
            return None

        if not isinstance(set_base_stmt, SetBaseStmt):
            return None
        if not isinstance(set_base_stmt.value, BinaryExpr) or set_base_stmt.value.op != "-":
            return None
        if not isinstance(set_base_stmt.value.left, BasePointer):
            return None
        if evaluate_int_constant(set_base_stmt.value.right) != option_count * 4:
            return None

        return cursor + 2, options, include_initialize, outline

    init_stmt = statements[index]
    if isinstance(init_stmt, ExprStmt) and isinstance(init_stmt.value, CallExpr):
        if call_opcode(init_stmt.value) == "f_164" and not init_stmt.value.args:
            cursor = index + 1
            options: list[StringLiteral] = []
            while cursor + 1 < len(statements):
                alloc_stmt = statements[cursor]
                assign_stmt = statements[cursor + 1]
                if not isinstance(alloc_stmt, StackAllocStmt) or alloc_stmt.size != 4:
                    break
                if not isinstance(assign_stmt, AssignStmt):
                    break
                if not isinstance(assign_stmt.target, LocalVar) or assign_stmt.target.offset != 4:
                    break
                if not isinstance(assign_stmt.value, StringLiteral):
                    break
                options.append(assign_stmt.value)
                cursor += 2
            return match_select_tail(cursor, options, True)

    cursor = index
    options = []
    while cursor + 1 < len(statements):
        alloc_stmt = statements[cursor]
        assign_stmt = statements[cursor + 1]
        if not isinstance(alloc_stmt, StackAllocStmt) or alloc_stmt.size != 4:
            break
        if not isinstance(assign_stmt, AssignStmt):
            break
        if not isinstance(assign_stmt.target, LocalVar) or assign_stmt.target.offset != 4:
            break
        if not isinstance(assign_stmt.value, StringLiteral):
            break
        options.append(assign_stmt.value)
        cursor += 2
    return match_select_tail(cursor, options, False)


def match_select_setup(statements: list[object], index: int) -> tuple[int, list[str], bool, str | None] | None:
    match = match_select_setup_sources(statements, index)
    if match is None:
        return None
    end, options, initialize, outline = match
    return end, [option.value for option in options], initialize, outline.value if outline else None


def render_select_setup(options: list[str], indent: str, include_initialize: bool, outline: str | None) -> list[str]:
    rendered_options = ", ".join(
        f"/*{index}:*/ {render_expr(StringLiteral(option))}" for index, option in enumerate(options)
    )
    lines: list[str] = []
    resolver = load_builtin_resolver()
    if include_initialize:
        initialize = resolver.display_name("f_164", like="")
        lines.append(f"{indent}{initialize}();")
    select = resolver.display_name("f_160", like="")
    if outline is None:
        lines.append(f"{indent}{select}({rendered_options});")
    else:
        rendered_outline = render_expr(StringLiteral(outline))
        lines.append(f"{indent}{select}({rendered_options}, /*outline:*/ {rendered_outline});")
    return lines


def iter_stmt_expressions(stmt) -> list[Expr]:
    if isinstance(stmt, AssignStmt):
        return [stmt.target, stmt.value]
    if isinstance(stmt, ExprStmt):
        return [stmt.value]
    if isinstance(stmt, RegisteredScriptJumpStmt):
        return [stmt.script]
    if isinstance(stmt, SetBaseStmt):
        return [stmt.value]
    if isinstance(stmt, BranchStmt):
        return [stmt.condition]
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return [stmt.value]
    return []


def walk_expr(expr: Expr, visitor) -> None:
    visitor(expr)
    if isinstance(expr, AddressOf):
        walk_expr(expr.target, visitor)
        return
    if isinstance(expr, DerefExpr):
        walk_expr(expr.address, visitor)
        return
    if isinstance(expr, UnaryExpr):
        walk_expr(expr.value, visitor)
        return
    if isinstance(expr, BinaryExpr):
        walk_expr(expr.left, visitor)
        walk_expr(expr.right, visitor)
        return
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            walk_expr(arg, visitor)


def match_local_array_index(address: Expr) -> tuple[int, Expr | None] | None:
    if isinstance(address, AddressOf) and isinstance(address.target, LocalVar):
        return address.target.offset, IntLiteral(0)

    terms: list[Expr] = []

    def collect_terms(expr: Expr) -> None:
        if isinstance(expr, BinaryExpr) and expr.op == "+":
            collect_terms(expr.left)
            collect_terms(expr.right)
        else:
            terms.append(expr)

    collect_terms(address)
    bases = [term for term in terms if isinstance(term, AddressOf) and isinstance(term.target, LocalVar)]
    if len(bases) != 1:
        return None

    base = bases[0]
    delta_terms = [term for term in terms if term is not base]
    if not delta_terms:
        return base.target.offset, IntLiteral(0)
    delta_expr = delta_terms[0]
    for term in delta_terms[1:]:
        delta_expr = BinaryExpr("+", delta_expr, term)
    index_expr = extract_local_array_index_expr(simplify_expr(delta_expr))
    return base.target.offset, index_expr


def iter_rendered_statements(function: Function):
    for block in function.blocks:
        index = 0
        while index < len(block.body):
            select_setup = match_select_setup(block.body, index)
            if select_setup is not None:
                next_index, _, _, _ = select_setup
                index = next_index
                continue
            yield block.body[index]
            index += 1
        if block.terminator is not None:
            yield block.terminator


def collect_local_index_upper_bounds(statements: list[object]) -> dict[str, int]:
    bounds: dict[str, int] = {}
    relations: list[tuple[str, str, int]] = []

    def bound_key(expr: Expr) -> str | None:
        if isinstance(expr, (LocalVar, ParamRef, ArgRef, NamedVar)):
            return expr_key(expr)
        return None

    def update_bound(key: str, upper_bound: int) -> None:
        if upper_bound > 0:
            bounds[key] = max(bounds.get(key, 0), upper_bound)

    def note_bound(condition: Expr) -> None:
        condition = simplify_expr(condition)
        if not isinstance(condition, BinaryExpr):
            return
        if condition.op in {"&&", "||"}:
            note_bound(condition.left)
            note_bound(condition.right)
            return

        candidates = (
            (condition.left, condition.right, condition.op),
            (condition.right, condition.left, SWAPPED_COMPARE_OPS.get(condition.op)),
        )
        for index_expr, limit_expr, operator in candidates:
            key = bound_key(index_expr)
            if key is None:
                continue
            limit = evaluate_int_constant(limit_expr)
            if limit is not None:
                if operator == "<":
                    update_bound(key, limit)
                elif operator in {"<=", ">"}:
                    update_bound(key, limit + 1)
                continue

            limit_key = bound_key(limit_expr)
            if limit_key is not None and operator in {"<", "<="}:
                relations.append((key, limit_key, -1 if operator == "<" else 0))

    for stmt in statements:
        if isinstance(stmt, BranchStmt):
            note_bound(stmt.condition)

    changed = True
    while changed:
        changed = False
        for target_key, limit_key, adjustment in relations:
            limit_bound = bounds.get(limit_key)
            if limit_bound is None:
                continue
            inferred = limit_bound + adjustment
            if inferred > bounds.get(target_key, 0):
                bounds[target_key] = inferred
                changed = True
    return bounds


def infer_local_array_size(index_expr: Expr | None, index_bounds: dict[str, int]) -> int:
    if index_expr is None:
        return 1
    constant = evaluate_int_constant(index_expr)
    if constant is not None:
        return max(1, constant + 1)
    key = expr_key(index_expr)
    if key in index_bounds:
        return max(1, index_bounds[key])
    if isinstance(index_expr, BinaryExpr) and index_expr.op in {"+", "-"}:
        left_size = infer_local_array_size(index_expr.left, index_bounds)
        right_constant = evaluate_int_constant(index_expr.right)
        if right_constant is not None:
            adjustment = right_constant if index_expr.op == "+" else -right_constant
            return max(1, left_size + adjustment)
    if isinstance(index_expr, BinaryExpr) and index_expr.op == "*":
        for factor_expr, value_expr in ((index_expr.left, index_expr.right), (index_expr.right, index_expr.left)):
            factor = evaluate_int_constant(factor_expr)
            if factor is not None and factor > 0:
                value_size = infer_local_array_size(value_expr, index_bounds)
                return max(1, (value_size - 1) * factor + 1)
    if isinstance(index_expr, BinaryExpr) and index_expr.op == "<<":
        shift = evaluate_int_constant(index_expr.right)
        if shift is not None and shift >= 0:
            value_size = infer_local_array_size(index_expr.left, index_bounds)
            return max(1, (value_size - 1) * (1 << shift) + 1)
    return 1


def collect_function_local_layout(function: Function) -> tuple[dict[int, str], dict[int, int]]:
    local_types: dict[int, str] = {}
    array_sizes: dict[int, int] = {}
    statements = list(iter_rendered_statements(function))
    index_bounds = collect_local_index_upper_bounds(statements)

    def note_local(offset: int, type_name: str | None = None) -> None:
        if offset not in local_types:
            local_types[offset] = "long"
        if type_name == "char*":
            local_types[offset] = "char*"

    def visit(expr: Expr) -> None:
        if isinstance(expr, LocalVar):
            note_local(expr.offset)
            return
        if isinstance(expr, AddressOf) and isinstance(expr.target, LocalVar):
            note_local(expr.target.offset)
            return
        if isinstance(expr, DerefExpr):
            local_array_access = match_local_array_index(expr.address)
            if local_array_access is not None:
                offset, index_expr = local_array_access
                note_local(offset)
                array_sizes[offset] = max(
                    array_sizes.get(offset, 1),
                    infer_local_array_size(index_expr, index_bounds),
                )

    for stmt in statements:
        if isinstance(stmt, AssignStmt) and isinstance(stmt.target, LocalVar) and isinstance(stmt.value, StringLiteral):
            note_local(stmt.target.offset, "char*")
        for expr in iter_stmt_expressions(stmt):
            walk_expr(expr, visit)

    return local_types, array_sizes


def collect_function_local_array_sizes(function: Function) -> dict[int, int]:
    return collect_function_local_layout(function)[1]


def collect_function_local_declarations(function: Function) -> tuple[list[str], set[int]]:
    local_types, array_sizes = collect_function_local_layout(function)

    declarations: list[str] = []
    for offset in sorted(local_types):
        type_name = local_types[offset]
        if offset in array_sizes:
            declarations.append(f"    {type_name} v{offset}[{array_sizes[offset]}];")
        else:
            declarations.append(f"    {type_name} v{offset};")
    return declarations, set(array_sizes)


def match_call_stmt(stmt, *callee_names: str) -> CallExpr | None:
    if not isinstance(stmt, ExprStmt):
        return None
    if not isinstance(stmt.value, CallExpr):
        return None
    normalized_targets = {canonical_callee_name(name) for name in callee_names}
    if (call_opcode(stmt.value) or stmt.value.callee.removeprefix("f_")) not in normalized_targets:
        return None
    return stmt.value


def contains_call(statements: list[object], *callee_names: str) -> bool:
    return any(match_call_stmt(stmt, *callee_names) is not None for stmt in statements)


def is_comment_only(statements: list[object]) -> bool:
    return all(isinstance(stmt, CommentStmt) for stmt in statements)


def collect_non_cfg_label_refs(function: Function) -> set[str]:
    refs: set[str] = set()

    def note_expr_labels(expr: Expr) -> None:
        def visitor(node: Expr) -> None:
            if isinstance(node, LabelRef):
                refs.add(node.name)

        walk_expr(expr, visitor)

    for block in function.blocks:
        for stmt in block.body:
            if isinstance(stmt, RegisterExceptionStmt):
                refs.add(stmt.target)
                continue
            if isinstance(stmt, InternalLabelCallStmt):
                refs.add(stmt.target)
                continue
            if isinstance(stmt, RawStmt):
                for match in re.finditer(r"\b(?:call\s+:|goto\s+)([A-Za-z_]\w*)", stmt.text):
                    refs.add(match.group(1))
                continue
            for expr in iter_stmt_expressions(stmt):
                note_expr_labels(expr)

    return refs


def collect_explicit_label_refs(function: Function) -> set[str]:
    refs = collect_non_cfg_label_refs(function)

    for index, block in enumerate(function.blocks):
        terminator = block.terminator
        next_label = function.blocks[index + 1].label if index + 1 < len(function.blocks) else None
        if isinstance(terminator, BranchStmt):
            refs.add(terminator.target)
        elif isinstance(terminator, JumpStmt) and terminator.target != next_label:
            refs.add(terminator.target)

    return refs


EMITTED_GOTO_LABEL_RE = re.compile(r"^\s*(?:if\s*\(.*\)\s+)?goto\s+([A-Za-z_]\w*)\s*;\s*$")
EMITTED_LABEL_RE = re.compile(r"^\s*([A-Za-z_]\w*):\s*$")


def extract_registered_label_name(expr: Expr) -> str | None:
    if not isinstance(expr, CallExpr):
        return None
    if call_opcode(expr) != "f_0f7" or len(expr.args) != 1:
        return None
    argument = expr.args[0]
    if not isinstance(argument, StringLiteral):
        return None
    return argument.value


def build_registered_label_target_map(function: Function) -> dict[str, str]:
    targets: dict[str, str] = {}
    blocked_names: set[str] = set()
    index_by_label = {block.label: index for index, block in enumerate(function.blocks)}

    for block_index, block in enumerate(function.blocks):
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            continue
        target_index = index_by_label.get(terminator.target)
        if target_index is None or target_index >= block_index:
            continue
        registered_name = extract_registered_label_name(terminator.condition)
        if registered_name is None or registered_name in blocked_names:
            continue
        existing_target = targets.get(registered_name)
        if existing_target is None:
            targets[registered_name] = terminator.target
            continue
        if existing_target != terminator.target:
            targets.pop(registered_name, None)
            blocked_names.add(registered_name)

    return targets


def compute_predecessor_labels(blocks: list[BasicBlock]) -> dict[str, set[str]]:
    predecessors = {block.label: set() for block in blocks}
    for block in blocks:
        for successor in block.successor_labels:
            if successor in predecessors:
                predecessors[successor].add(block.label)
    return predecessors


def compute_dominators(blocks: list[BasicBlock], predecessors: dict[str, set[str]]) -> dict[str, set[str]]:
    if not blocks:
        return {}

    labels = {block.label for block in blocks}
    entry = blocks[0].label
    dominators = {label: set(labels) for label in labels}
    dominators[entry] = {entry}

    changed = True
    while changed:
        changed = False
        for block in blocks[1:]:
            preds = predecessors.get(block.label, set())
            if preds:
                new_dominators = {block.label}.union(set.intersection(*(dominators[pred] for pred in preds)))
            else:
                new_dominators = {block.label}
            if new_dominators != dominators[block.label]:
                dominators[block.label] = new_dominators
                changed = True

    return dominators


def compute_postdominators(blocks: list[BasicBlock]) -> dict[str, set[str]]:
    if not blocks:
        return {}

    labels = {block.label for block in blocks}
    successors = {
        block.label: [successor for successor in block.successor_labels if successor in labels]
        for block in blocks
    }
    exits = {block.label for block in blocks if not successors[block.label]}
    postdominators = {label: set(labels) for label in labels}
    for exit_label in exits:
        postdominators[exit_label] = {exit_label}

    changed = True
    while changed:
        changed = False
        for block in reversed(blocks):
            succs = successors[block.label]
            if succs:
                new_postdominators = {block.label}.union(set.intersection(*(postdominators[succ] for succ in succs)))
            else:
                new_postdominators = {block.label}
            if new_postdominators != postdominators[block.label]:
                postdominators[block.label] = new_postdominators
                changed = True

    return postdominators


def compute_immediate_postdominators(postdominators: dict[str, set[str]]) -> dict[str, str | None]:
    immediate_postdominators: dict[str, str | None] = {}
    for label, strict_postdominators in postdominators.items():
        candidates = strict_postdominators - {label}
        immediate = next(
            (
                candidate
                for candidate in candidates
                if all(candidate == other or candidate not in postdominators[other] for other in candidates)
            ),
            None,
        )
        immediate_postdominators[label] = immediate
    return immediate_postdominators


def compute_back_edges(blocks: list[BasicBlock], dominators: dict[str, set[str]]) -> set[tuple[str, str]]:
    back_edges: set[tuple[str, str]] = set()
    labels = {block.label for block in blocks}
    for block in blocks:
        for successor in block.successor_labels:
            if successor in labels and successor in dominators.get(block.label, set()):
                back_edges.add((block.label, successor))
    return back_edges


def collect_natural_loop(
    latch_label: str,
    header_label: str,
    predecessors: dict[str, set[str]],
) -> set[str]:
    loop_nodes = {header_label, latch_label}
    pending = [latch_label]

    while pending:
        label = pending.pop()
        for predecessor in predecessors.get(label, set()):
            if predecessor not in loop_nodes:
                loop_nodes.add(predecessor)
                pending.append(predecessor)

    return loop_nodes


def build_cfg_info(function: Function) -> CfgInfo:
    block_by_label = {block.label: block for block in function.blocks}
    index_by_label = {block.label: index for index, block in enumerate(function.blocks)}
    predecessors = compute_predecessor_labels(function.blocks)
    dominators = compute_dominators(function.blocks, predecessors)
    postdominators = compute_postdominators(function.blocks)
    immediate_postdominators = compute_immediate_postdominators(postdominators)
    back_edges = compute_back_edges(function.blocks, dominators)
    natural_loops: dict[str, set[str]] = {}
    for latch_label, header_label in back_edges:
        natural_loops.setdefault(header_label, set()).update(
            collect_natural_loop(latch_label, header_label, predecessors)
        )

    return CfgInfo(
        entry_label=function.blocks[0].label if function.blocks else function.name,
        block_by_label=block_by_label,
        index_by_label=index_by_label,
        predecessors=predecessors,
        dominators=dominators,
        postdominators=postdominators,
        immediate_postdominators=immediate_postdominators,
        back_edges=back_edges,
        natural_loops=natural_loops,
    )


def collect_pointer_param_sizes(function: Function) -> dict[str, int]:
    if not function.params:
        return {}

    param_types = function.param_types or ["long"] * len(function.params)
    pointer_sizes: dict[str, int] = {}
    for index, name in enumerate(function.params):
        type_name = param_types[index] if index < len(param_types) else "long"
        stride = pointer_element_size(type_name)
        if stride is not None:
            pointer_sizes[name] = stride
    return pointer_sizes


def render_expr(
    expr: Expr,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
) -> str:
    return render_expr_with_precedence(expr, array_locals=array_locals, pointer_param_sizes=pointer_param_sizes)


def render_stmt(
    stmt,
    indent: str,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
    registered_label_targets: dict[str, str] | None = None,
) -> list[str]:
    if isinstance(stmt, CommentStmt):
        return [f"{indent}// {stmt.text}"]
    if isinstance(stmt, LabelStmt):
        return [f"{stmt.name}:"]
    if isinstance(stmt, StackAllocStmt):
        return [f"{indent}StackAlloc({stmt.size});"]
    if isinstance(stmt, SetBaseStmt):
        return [f"{indent}SetBase({render_expr(stmt.value, array_locals, pointer_param_sizes)});"]
    if isinstance(stmt, AssignStmt):
        return [
            f"{indent}{render_expr(stmt.target, array_locals, pointer_param_sizes)} = {render_expr(stmt.value, array_locals, pointer_param_sizes)};"
        ]
    if isinstance(stmt, ExprStmt):
        return [f"{indent}{render_expr(stmt.value, array_locals, pointer_param_sizes)};"]
    if isinstance(stmt, JumpStmt):
        return [f"{indent}goto {stmt.target};"]
    if isinstance(stmt, BranchStmt):
        if stmt.jump_if_false:
            return [
                f"{indent}if ({render_expr(UnaryExpr('!', stmt.condition), array_locals, pointer_param_sizes)}) goto {stmt.target};"
            ]
        return [f"{indent}if ({render_expr(stmt.condition, array_locals, pointer_param_sizes)}) goto {stmt.target};"]
    if isinstance(stmt, RegisterExceptionStmt):
        return [f"{indent}reg_exception_handler({stmt.target});"]
    if isinstance(stmt, UnregisterExceptionStmt):
        return [f"{indent}unreg_exception_handler();"]
    if isinstance(stmt, ReturnStmt):
        if stmt.value is None:
            return [f"{indent}return;"]
        return [f"{indent}return {render_expr(stmt.value, array_locals, pointer_param_sizes)};"]
    if isinstance(stmt, InternalLabelCallStmt):
        resolved_target = registered_label_targets.get(stmt.registered_name, stmt.target) if registered_label_targets else stmt.target
        return [f"{indent}call :{stmt.registered_name}; // target {resolved_target}"]
    if isinstance(stmt, RegisteredScriptJumpStmt):
        registered_name = render_expr(StringLiteral(stmt.registered_name), array_locals, pointer_param_sizes)
        script = render_expr(stmt.script, array_locals, pointer_param_sizes)
        register = stmt.register_callee or load_builtin_resolver().display_name("f_0f6")
        return [f"{indent}{register}({registered_name});", f"{indent}{stmt.callee}({script});"]
    if isinstance(stmt, RawStmt):
        text = stmt.text.rstrip()
        return [f"{indent}{text}" if text.endswith(";") else f"{indent}{text};"]
    return [f"{indent}// <unknown stmt>"]


def render_terminator(
    block: BasicBlock,
    function: Function,
    indent: str,
    array_locals: set[int] | None = None,
    pointer_param_sizes: dict[str, int] | None = None,
    registered_label_targets: dict[str, str] | None = None,
) -> list[str]:
    terminator = block.terminator
    if terminator is None:
        return []
    if isinstance(terminator, ReturnStmt) and terminator.value is None and function.return_type != "void":
        return [f"{indent}return 0;"]
    return render_stmt(terminator, indent, array_locals, pointer_param_sizes, registered_label_targets)


class StructuredEmitter:
    def __init__(
        self,
        function: Function,
        array_locals: set[int] | None = None,
        pointer_param_sizes: dict[str, int] | None = None,
        registered_label_targets: dict[str, str] | None = None,
    ):
        self.function = function
        self.blocks = function.blocks
        self.array_locals = array_locals or set()
        self.pointer_param_sizes = pointer_param_sizes or {}
        self.registered_label_targets = registered_label_targets or {}
        self.cfg = build_cfg_info(function)
        self.index_by_label = dict(self.cfg.index_by_label)
        self.block_by_label = dict(self.cfg.block_by_label)
        self.predecessors = dict(self.cfg.predecessors)
        self.dominators = dict(self.cfg.dominators)
        self.postdominators = dict(self.cfg.postdominators)
        self.immediate_postdominators = dict(self.cfg.immediate_postdominators)
        self.back_edges = set(self.cfg.back_edges)
        self.natural_loops = dict(self.cfg.natural_loops)
        self.predecessor_counts: dict[str, int] = {
            label: len(predecessors) for label, predecessors in self.predecessors.items()
        }
        self.suppressed_labels: set[str] = set()
        self.non_cfg_label_refs = collect_non_cfg_label_refs(function)
        self.explicit_label_refs = collect_explicit_label_refs(function)
        self.required_label_refs: set[str] = set()

    def should_render_label(self, label: str, show_label: bool) -> bool:
        required_label = label in self.required_label_refs
        return (
            label != self.function.name
            and label in self.explicit_label_refs
            and (
                required_label
                or (
                    not is_synthetic_label(label)
                    and show_label
                    and label not in self.suppressed_labels
                )
            )
        )

    def render(self) -> list[str]:
        root = self.build_region_tree(0, len(self.blocks), suppress_first_label=False)
        lines = self.render_region_node(root, "    ")
        required_label_refs = self.non_cfg_label_refs.union(self.collect_emitted_goto_label_refs(lines))
        missing_required_labels = required_label_refs.difference(self.collect_rendered_labels(lines))
        self.required_label_refs = missing_required_labels
        if missing_required_labels:
            lines = self.render_region_node(root, "    ")
        return lines

    def collect_emitted_goto_label_refs(self, lines: list[str]) -> set[str]:
        refs: set[str] = set()
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            match = EMITTED_GOTO_LABEL_RE.match(line)
            if match is not None:
                refs.add(match.group(1))
        return refs

    def collect_rendered_labels(self, lines: list[str]) -> set[str]:
        labels: set[str] = set()
        for line in lines:
            match = EMITTED_LABEL_RE.match(line)
            if match is not None:
                labels.add(match.group(1))
        return labels

    def build_region_tree(
        self,
        start: int,
        end: int,
        suppress_first_label: bool = False,
        suppress_terminal_jump_to: str | None = None,
        loop_header_label: str | None = None,
        loop_exit_label: str | None = None,
        switch_exit_label: str | None = None,
    ) -> RegionNode:
        children: list[RegionNode] = []
        index = start

        while index < end:
            show_label = not suppress_first_label or index != start

            return_guard = self.match_return_guard(index, end)
            if return_guard is not None:
                fail_index, success_index = return_guard
                block = self.blocks[index]
                children.append(BlockBodyRegion(index, show_label=show_label))
                children.append(
                    IfRegion(
                        header_index=index,
                        condition=block.terminator.condition,
                        then_region=BlockRegion(fail_index, show_label=False),
                    )
                )
                self.maybe_suppress_internal_label(self.blocks[success_index].label, {block.label}, 1)
                index = success_index
                suppress_first_label = False
                continue

            guard_exit_chain = self.match_guard_exit_chain(index, end)
            if guard_exit_chain is not None:
                condition, body_start, exit_index, inline_exit = guard_exit_chain
                children.append(BlockBodyRegion(index, show_label=show_label))
                then_region: RegionNode
                if inline_exit:
                    self.suppressed_labels.add(self.blocks[exit_index].label)
                    then_region = InlineExitRegion(exit_index)
                else:
                    then_region = GotoRegion(self.blocks[exit_index].label)
                children.append(IfRegion(header_index=index, condition=condition, then_region=then_region))
                if self.predecessor_counts.get(self.blocks[body_start].label, 0) <= 1:
                    self.suppressed_labels.add(self.blocks[body_start].label)
                index = body_start
                suppress_first_label = False
                continue

            if self.match_infinite_self_loop(index):
                children.append(
                    InfiniteLoopRegion(
                        header_index=index,
                        body_region=BlockBodyRegion(index, show_label=False),
                        show_label=show_label,
                    )
                )
                index += 1
                suppress_first_label = False
                continue

            do_while_match = self.match_do_while(index, end)
            if do_while_match is not None:
                condition_index, condition, exit_index = do_while_match
                exit_label = self.blocks[exit_index].label
                children.append(
                    DoWhileRegion(
                        condition=condition,
                        header_index=index,
                        body_region=SequenceRegion([
                            self.build_region_tree(
                                index,
                                condition_index,
                                suppress_first_label=True,
                                loop_header_label=self.blocks[index].label,
                                loop_exit_label=exit_label,
                            ),
                            # The test's block may also display text or update
                            # the value being tested. Execute its body on every
                            # iteration before evaluating the while condition.
                            BlockBodyRegion(condition_index, show_label=False),
                        ]),
                        exit_label=exit_label,
                    )
                )
                self.maybe_suppress_internal_label(exit_label, self.slice_labels(index, exit_index), 1)
                index = exit_index
                suppress_first_label = False
                continue

            select_color_loop = self.match_select_color_loop(index, end)
            if select_color_loop is not None:
                cases, exit_index = select_color_loop
                children.append(
                    SelectColorLoopRegion(
                        header_index=index,
                        cases=cases,
                        exit_index=exit_index,
                        show_label=show_label,
                    )
                )
                self.maybe_suppress_internal_label(self.blocks[exit_index].label, self.slice_labels(index, exit_index), 1)
                index = exit_index
                suppress_first_label = False
                continue

            switch_match = self.match_switch(
                index,
                end,
                region_exit_labels=tuple(
                    label for label in (suppress_terminal_jump_to, switch_exit_label) if label is not None
                ),
            )
            if switch_match is not None:
                subject_expr, cases, default_region, end_index, join_label = switch_match
                children.append(BlockBodyRegion(index, show_label=show_label))
                case_regions = [
                    SwitchCaseRegion(
                        value=case_value,
                        body=self.build_region_tree(
                            case_start,
                            case_end,
                            suppress_first_label=True,
                            switch_exit_label=join_label,
                        ),
                    )
                    for case_value, case_start, case_end in cases
                ]
                default_node: RegionNode | None = None
                if default_region is not None:
                    default_start, default_end = default_region
                    default_node = self.build_region_tree(
                        default_start,
                        default_end,
                        suppress_first_label=True,
                        switch_exit_label=join_label,
                    )
                children.append(
                    SwitchRegion(
                        header_index=index,
                        subject=subject_expr,
                        cases=case_regions,
                        default_region=default_node,
                        exit_label=join_label,
                    )
                )
                if join_label is not None:
                    self.maybe_suppress_internal_label(
                        join_label,
                        self.slice_labels(index, end_index),
                        len(cases) + (1 if default_region is not None else 0),
                    )
                index = end_index
                suppress_first_label = False
                continue

            guard_match = self.match_guard_goto(index, end)
            if guard_match is not None:
                target_index, _ = guard_match
                block = self.blocks[index]
                children.append(BlockBodyRegion(index, show_label=show_label))
                children.append(
                    IfRegion(
                        header_index=index,
                        condition=block.terminator.condition,
                        then_region=self.build_region_tree(
                            index + 1,
                            target_index,
                            suppress_first_label=True,
                            loop_header_label=loop_header_label,
                            loop_exit_label=loop_exit_label,
                            switch_exit_label=switch_exit_label,
                        ),
                    )
                )
                self.maybe_suppress_internal_label(self.blocks[target_index].label, self.slice_labels(index, target_index), 2)
                index = target_index
                suppress_first_label = False
                continue

            positive_case_chain = self.match_positive_case_chain(index, end)
            if positive_case_chain is not None:
                cases, exit_index, skip_index, common_join_label = positive_case_chain
                children.append(
                    PositiveCaseChainRegion(
                        header_index=index,
                        cases=cases,
                        suppress_terminal_jump_to=common_join_label,
                        show_label=show_label,
                    )
                )
                if common_join_label is not None:
                    self.maybe_suppress_internal_label(
                        common_join_label,
                        self.slice_labels(index, exit_index),
                        len(cases) + 1,
                    )
                if skip_index is not None:
                    self.maybe_suppress_label(self.blocks[exit_index].label, 1)
                    index = skip_index
                else:
                    index = exit_index
                suppress_first_label = False
                continue

            while_match = self.match_while(index, end)
            if while_match is not None:
                block = self.blocks[index]
                children.append(BlockBodyRegion(index, show_label=show_label))
                children.append(
                    WhileRegion(
                        header_index=index,
                        condition=block.terminator.condition,
                        body_region=self.build_region_tree(
                            index + 1,
                            while_match,
                            suppress_first_label=True,
                            suppress_terminal_jump_to=block.label,
                            loop_header_label=block.label,
                            loop_exit_label=self.blocks[while_match].label,
                        ),
                        exit_label=self.blocks[while_match].label,
                    )
                )
                self.maybe_suppress_internal_label(
                    self.blocks[while_match].label,
                    self.slice_labels(index, while_match),
                    1,
                )
                index = while_match
                suppress_first_label = False
                continue

            conjunctive_if_chain = self.match_conjunctive_if_chain(index, end)
            if conjunctive_if_chain is not None:
                condition, body_start, join_index, condition_count = conjunctive_if_chain
                block = self.blocks[index]
                join_label = self.blocks[join_index].label
                children.append(BlockBodyRegion(index, show_label=show_label))
                children.append(
                    IfRegion(
                        header_index=index,
                        condition=condition,
                        then_region=self.build_region_tree(
                            body_start,
                            join_index,
                            suppress_first_label=True,
                            suppress_terminal_jump_to=join_label,
                            loop_header_label=loop_header_label,
                            loop_exit_label=loop_exit_label,
                            switch_exit_label=switch_exit_label,
                        ),
                    )
                )
                self.maybe_suppress_internal_label(
                    join_label,
                    self.slice_labels(index, join_index),
                    condition_count + 1,
                )
                index = join_index
                suppress_first_label = False
                continue

            if_else_match = self.match_if_else(index, end)
            if if_else_match is not None:
                else_index, end_index = if_else_match
                block = self.blocks[index]
                end_label = self.blocks[end_index].label
                children.append(BlockBodyRegion(index, show_label=show_label))
                children.append(
                    IfRegion(
                        header_index=index,
                        condition=block.terminator.condition,
                        then_region=self.build_region_tree(
                            index + 1,
                            else_index,
                            suppress_first_label=True,
                            suppress_terminal_jump_to=end_label,
                            loop_header_label=loop_header_label,
                            loop_exit_label=loop_exit_label,
                            switch_exit_label=switch_exit_label,
                        ),
                        else_region=self.build_region_tree(
                            else_index,
                            end_index,
                            suppress_first_label=True,
                            suppress_terminal_jump_to=end_label,
                            loop_header_label=loop_header_label,
                            loop_exit_label=loop_exit_label,
                            switch_exit_label=switch_exit_label,
                        ),
                    )
                )
                self.maybe_suppress_internal_label(end_label, self.slice_labels(index, end_index), 2)
                index = end_index
                suppress_first_label = False
                continue

            if_match = self.match_if(index, end)
            if if_match is not None:
                block = self.blocks[index]
                children.append(BlockBodyRegion(index, show_label=show_label))
                children.append(
                    IfRegion(
                        header_index=index,
                        condition=block.terminator.condition,
                        then_region=self.build_region_tree(
                            index + 1,
                            if_match,
                            suppress_first_label=True,
                            suppress_terminal_jump_to=block.terminator.target,
                            loop_header_label=loop_header_label,
                            loop_exit_label=loop_exit_label,
                            switch_exit_label=switch_exit_label,
                        ),
                    )
                )
                self.maybe_suppress_internal_label(block.terminator.target, self.slice_labels(index, if_match), 2)
                index = if_match
                suppress_first_label = False
                continue

            children.append(
                BlockRegion(
                    index=index,
                    show_label=show_label,
                    suppress_terminal_jump_to=suppress_terminal_jump_to,
                    loop_header_label=loop_header_label,
                    loop_exit_label=loop_exit_label,
                    switch_exit_label=switch_exit_label,
                )
            )
            index += 1
            suppress_first_label = False

        if not children:
            return EmptyRegion()
        if len(children) == 1:
            return children[0]
        return SequenceRegion(children)

    def can_flatten_else_region(self, region: RegionNode) -> bool:
        region = self.normalize_statement_region(region)
        return isinstance(region, (IfRegion, SwitchRegion, PositiveCaseChainRegion))

    def is_render_empty_region(self, region: RegionNode) -> bool:
        if isinstance(region, EmptyRegion):
            return True
        if isinstance(region, SequenceRegion):
            return all(self.is_render_empty_region(child) for child in region.children)
        if isinstance(region, BlockBodyRegion):
            block = self.blocks[region.index]
            return not block.body and not self.should_render_label(block.label, region.show_label)
        return False

    def normalize_statement_region(self, region: RegionNode) -> RegionNode:
        if isinstance(region, SequenceRegion):
            meaningful_children = [child for child in region.children if not self.is_render_empty_region(child)]
            if not meaningful_children:
                return EmptyRegion()
            if len(meaningful_children) == 1:
                return self.normalize_statement_region(meaningful_children[0])
            return SequenceRegion(meaningful_children)
        return region

    def render_region_node(self, region: RegionNode, indent: str) -> list[str]:
        if isinstance(region, EmptyRegion):
            return []
        if isinstance(region, SequenceRegion):
            lines: list[str] = []
            for child in region.children:
                lines.extend(self.render_region_node(child, indent))
            return lines
        if isinstance(region, BlockRegion):
            return self.render_block_fallback(
                region.index,
                indent,
                show_label=region.show_label,
                suppress_terminal_jump_to=region.suppress_terminal_jump_to,
                loop_header_label=region.loop_header_label,
                loop_exit_label=region.loop_exit_label,
                switch_exit_label=region.switch_exit_label,
            )
        if isinstance(region, BlockBodyRegion):
            return self.render_block_body(self.blocks[region.index], indent, show_label=region.show_label)
        if isinstance(region, GotoRegion):
            return [f"{indent}goto {region.target};"]
        if isinstance(region, InlineExitRegion):
            return self.render_inline_exit_block(region.block_index, indent)
        if isinstance(region, IfRegion):
            lines = [f"{indent}if ({render_expr(region.condition, self.array_locals, self.pointer_param_sizes)}) {{"]
            lines.extend(self.render_region_node(region.then_region, indent + "    "))
            if region.else_region is None:
                lines.append(f"{indent}}}")
                return lines

            normalized_else_region = self.normalize_statement_region(region.else_region)
            if self.can_flatten_else_region(normalized_else_region):
                else_lines = self.render_region_node(normalized_else_region, indent)
                if else_lines:
                    first = else_lines[0].lstrip()
                    else_lines[0] = f"{indent}}} else {first}"
                    lines.extend(else_lines)
                    return lines

            lines.append(f"{indent}}} else {{")
            lines.extend(self.render_region_node(normalized_else_region, indent + "    "))
            lines.append(f"{indent}}}")
            return lines
        if isinstance(region, DoWhileRegion):
            lines = [f"{indent}do {{"]
            lines.extend(self.render_region_node(region.body_region, indent + "    "))
            lines.append(f"{indent}}} while ({render_expr(region.condition, self.array_locals, self.pointer_param_sizes)});")
            return lines
        if isinstance(region, WhileRegion):
            lines = [f"{indent}while ({render_expr(region.condition, self.array_locals, self.pointer_param_sizes)}) {{"]
            lines.extend(self.render_region_node(region.body_region, indent + "    "))
            lines.append(f"{indent}}}")
            return lines
        if isinstance(region, InfiniteLoopRegion):
            block = self.blocks[region.header_index]
            lines: list[str] = []
            if self.should_render_label(block.label, region.show_label):
                lines.append(f"{block.label}:")
            lines.append(f"{indent}while (1) {{")
            lines.extend(self.render_region_node(region.body_region, indent + "    "))
            lines.append(f"{indent}}}")
            return lines
        if isinstance(region, SwitchRegion):
            lines = [f"{indent}switch ({render_expr(region.subject, self.array_locals, self.pointer_param_sizes)}) {{"]
            for case in region.cases:
                lines.append(f"{indent}case {case.value}:")
                lines.extend(self.render_region_node(case.body, indent + "    "))
            if region.default_region is not None:
                lines.append(f"{indent}default:")
                lines.extend(self.render_region_node(region.default_region, indent + "    "))
            lines.append(f"{indent}}}")
            return lines
        if isinstance(region, SelectColorLoopRegion):
            return self.render_select_color_loop(
                region.header_index,
                region.cases,
                indent,
                show_label=region.show_label,
            )
        if isinstance(region, PositiveCaseChainRegion):
            return self.render_positive_case_chain(
                region.header_index,
                region.cases,
                indent,
                show_label=region.show_label,
                suppress_terminal_jump_to=region.suppress_terminal_jump_to,
            )
        return [f"{indent}// <unknown region>"]

    def render_region(
        self,
        start: int,
        end: int,
        indent: str,
        suppress_first_label: bool = False,
        suppress_terminal_jump_to: str | None = None,
        loop_header_label: str | None = None,
        loop_exit_label: str | None = None,
        switch_exit_label: str | None = None,
    ) -> list[str]:
        lines: list[str] = []
        index = start

        while index < end:
            return_guard = self.match_return_guard(index, end)
            if return_guard is not None:
                fail_index, success_index = return_guard
                block = self.blocks[index]
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}if ({render_expr(block.terminator.condition, self.array_locals, self.pointer_param_sizes)}) {{")
                lines.extend(self.render_block_fallback(fail_index, indent + "    ", show_label=False))
                lines.append(f"{indent}}}")
                self.maybe_suppress_label(self.blocks[success_index].label, 1)
                index = success_index
                suppress_first_label = False
                continue

            guard_exit_chain = self.match_guard_exit_chain(index, end)
            if guard_exit_chain is not None:
                condition, body_start, exit_index, inline_exit = guard_exit_chain
                block = self.blocks[index]
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}if ({render_expr(condition, self.array_locals, self.pointer_param_sizes)}) {{")
                if inline_exit:
                    self.suppressed_labels.add(self.blocks[exit_index].label)
                    lines.extend(self.render_inline_exit_block(exit_index, indent + "    "))
                else:
                    lines.append(f"{indent}    goto {self.blocks[exit_index].label};")
                lines.append(f"{indent}}}")
                if self.predecessor_counts.get(self.blocks[body_start].label, 0) <= 1:
                    self.suppressed_labels.add(self.blocks[body_start].label)
                index = body_start
                suppress_first_label = False
                continue

            do_while_match = self.match_do_while(index, end)
            if do_while_match is not None:
                condition_index, condition, exit_index = do_while_match
                exit_label = self.blocks[exit_index].label
                lines.append(f"{indent}do {{")
                lines.extend(
                    self.render_region(
                        index,
                        condition_index,
                        indent + "    ",
                        suppress_first_label=True,
                        loop_header_label=self.blocks[index].label,
                        loop_exit_label=exit_label,
                    )
                )
                lines.extend(self.render_block_body(self.blocks[condition_index], indent + "    ", show_label=False))
                lines.append(f"{indent}}} while ({render_expr(condition, self.array_locals, self.pointer_param_sizes)});")
                self.maybe_suppress_label(exit_label, 1)
                index = exit_index
                suppress_first_label = False
                continue

            select_color_loop = self.match_select_color_loop(index, end)
            if select_color_loop is not None:
                cases, exit_index = select_color_loop
                lines.extend(
                    self.render_select_color_loop(
                        index,
                        cases,
                        indent,
                        show_label=not suppress_first_label or index != start,
                    )
                )
                self.maybe_suppress_label(self.blocks[exit_index].label, 1)
                index = exit_index
                suppress_first_label = False
                continue

            switch_match = self.match_switch(
                index,
                end,
                region_exit_labels=tuple(
                    label for label in (suppress_terminal_jump_to, switch_exit_label) if label is not None
                ),
            )
            if switch_match is not None:
                subject_expr, cases, default_region, end_index, join_label = switch_match
                block = self.blocks[index]
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}switch ({render_expr(subject_expr, self.array_locals, self.pointer_param_sizes)}) {{")
                for case_value, case_start, case_end in cases:
                    lines.append(f"{indent}case {case_value}:")
                    lines.extend(
                        self.render_region(
                            case_start,
                            case_end,
                            indent + "    ",
                            suppress_first_label=True,
                            switch_exit_label=join_label,
                        )
                    )
                if default_region is not None:
                    default_start, default_end = default_region
                    lines.append(f"{indent}default:")
                    lines.extend(
                        self.render_region(
                            default_start,
                            default_end,
                            indent + "    ",
                            suppress_first_label=True,
                            switch_exit_label=join_label,
                        )
                    )
                lines.append(f"{indent}}}")
                if join_label is not None:
                    self.maybe_suppress_label(join_label, len(cases) + (1 if default_region is not None else 0))
                index = end_index
                suppress_first_label = False
                continue

            guard_match = self.match_guard_goto(index, end)
            if guard_match is not None:
                target_index, _ = guard_match
                block = self.blocks[index]
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}if ({render_expr(block.terminator.condition, self.array_locals, self.pointer_param_sizes)}) {{")
                lines.extend(
                    self.render_region(
                        index + 1,
                        target_index,
                        indent + "    ",
                        suppress_first_label=True,
                        loop_header_label=loop_header_label,
                        loop_exit_label=loop_exit_label,
                        switch_exit_label=switch_exit_label,
                    )
                )
                lines.append(f"{indent}}}")
                self.maybe_suppress_label(self.blocks[target_index].label, 2)
                index = target_index
                suppress_first_label = False
                continue

            positive_case_chain = self.match_positive_case_chain(index, end)
            if positive_case_chain is not None:
                cases, exit_index, skip_index, common_join_label = positive_case_chain
                lines.extend(
                    self.render_positive_case_chain(
                        index,
                        cases,
                        indent,
                        show_label=not suppress_first_label or index != start,
                        suppress_terminal_jump_to=common_join_label,
                    )
                )
                if common_join_label is not None:
                    self.maybe_suppress_label(common_join_label, len(cases) + 1)
                if skip_index is not None:
                    self.maybe_suppress_label(self.blocks[exit_index].label, 1)
                    index = skip_index
                else:
                    index = exit_index
                suppress_first_label = False
                continue

            while_match = self.match_while(index, end)
            if while_match is not None:
                block = self.blocks[index]
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}while ({render_expr(block.terminator.condition, self.array_locals, self.pointer_param_sizes)}) {{")
                lines.extend(
                    self.render_region(
                        index + 1,
                        while_match,
                        indent + "    ",
                        suppress_first_label=True,
                        suppress_terminal_jump_to=block.label,
                        loop_header_label=block.label,
                        loop_exit_label=self.blocks[while_match].label,
                    )
                )
                lines.append(f"{indent}}}")
                self.maybe_suppress_label(self.blocks[while_match].label, 1)
                index = while_match
                suppress_first_label = False
                continue

            if_else_match = self.match_if_else(index, end)
            if if_else_match is not None:
                else_index, end_index = if_else_match
                block = self.blocks[index]
                end_label = self.blocks[end_index].label
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}if ({render_expr(block.terminator.condition, self.array_locals, self.pointer_param_sizes)}) {{")
                lines.extend(
                    self.render_region(
                        index + 1,
                        else_index,
                        indent + "    ",
                        suppress_first_label=True,
                        suppress_terminal_jump_to=end_label,
                        loop_header_label=loop_header_label,
                        loop_exit_label=loop_exit_label,
                        switch_exit_label=switch_exit_label,
                    )
                )
                nested_switch = self.match_switch(
                    else_index,
                    end_index,
                    region_exit_labels=tuple(label for label in (end_label, switch_exit_label) if label is not None),
                )
                nested_if = self.match_if(else_index, end_index)
                nested_if_else = self.match_if_else(else_index, end_index)
                flatten_nested_else = False
                if nested_switch is not None and nested_switch[3] == end_index:
                    flatten_nested_else = True
                if nested_if is not None and nested_if == end_index:
                    flatten_nested_else = True
                if nested_if_else is not None and nested_if_else[1] == end_index:
                    flatten_nested_else = True
                if flatten_nested_else:
                    nested_lines = self.render_region(
                        else_index,
                        end_index,
                        indent,
                        suppress_first_label=True,
                        suppress_terminal_jump_to=end_label,
                        loop_header_label=loop_header_label,
                        loop_exit_label=loop_exit_label,
                        switch_exit_label=switch_exit_label,
                    )
                    if nested_lines:
                        first = nested_lines[0].lstrip()
                        nested_lines[0] = f"{indent}}} else {first}"
                        lines.extend(nested_lines)
                else:
                    lines.append(f"{indent}}} else {{")
                    lines.extend(
                        self.render_region(
                            else_index,
                            end_index,
                            indent + "    ",
                            suppress_first_label=True,
                            suppress_terminal_jump_to=end_label,
                            loop_header_label=loop_header_label,
                            loop_exit_label=loop_exit_label,
                            switch_exit_label=switch_exit_label,
                        )
                    )
                    lines.append(f"{indent}}}")
                self.maybe_suppress_label(end_label, 2)
                index = end_index
                suppress_first_label = False
                continue

            if_match = self.match_if(index, end)
            if if_match is not None:
                block = self.blocks[index]
                lines.extend(self.render_block_body(block, indent, show_label=not suppress_first_label or index != start))
                lines.append(f"{indent}if ({render_expr(block.terminator.condition, self.array_locals, self.pointer_param_sizes)}) {{")
                lines.extend(
                    self.render_region(
                        index + 1,
                        if_match,
                        indent + "    ",
                        suppress_first_label=True,
                        suppress_terminal_jump_to=block.terminator.target,
                        loop_header_label=loop_header_label,
                        loop_exit_label=loop_exit_label,
                        switch_exit_label=switch_exit_label,
                    )
                )
                lines.append(f"{indent}}}")
                self.maybe_suppress_label(block.terminator.target, 2)
                index = if_match
                suppress_first_label = False
                continue

            lines.extend(
                self.render_block_fallback(
                    index,
                    indent,
                    show_label=not suppress_first_label or index != start,
                    suppress_terminal_jump_to=suppress_terminal_jump_to,
                    loop_header_label=loop_header_label,
                    loop_exit_label=loop_exit_label,
                    switch_exit_label=switch_exit_label,
                )
            )
            index += 1
            suppress_first_label = False

        return lines

    def maybe_suppress_label(self, label: str, max_predecessors: int | None = None) -> None:
        if label == self.function.name or is_synthetic_label(label):
            return
        if max_predecessors is not None and self.predecessor_counts.get(label, 0) > max_predecessors:
            return
        self.suppressed_labels.add(label)

    def maybe_suppress_internal_label(
        self,
        label: str,
        allowed_predecessors: set[str],
        max_predecessors: int | None = None,
    ) -> None:
        if label == self.function.name or is_synthetic_label(label):
            return
        if label in self.non_cfg_label_refs:
            return
        if self.predecessors.get(label, set()).issubset(allowed_predecessors):
            self.suppressed_labels.add(label)
            return
        self.maybe_suppress_label(label, max_predecessors)

    def match_return_guard(self, index: int, end: int) -> tuple[int, int] | None:
        if index + 2 >= end:
            return None

        block = self.blocks[index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None

        success_label = terminator.target
        success_index = self.index_by_label.get(success_label)
        if success_index != index + 2 or success_index >= end:
            return None

        true_label, false_label = self.branch_true_false_labels(index, terminator)
        if false_label != success_label or true_label != self.blocks[index + 1].label:
            return None

        if not self.slice_has_only_allowed_exits(index + 1, success_index, {success_label}):
            return None

        fail_block = self.blocks[index + 1]
        if fail_block.successor_labels:
            return None
        if not isinstance(fail_block.terminator, ReturnStmt):
            return None
        return index + 1, success_index

    def is_guard_exit_pair(self, index: int, end: int, exit_label: str | None = None) -> tuple[int, str, Expr] | None:
        if index + 1 >= end:
            return None

        block = self.blocks[index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None

        exit_block = self.blocks[index + 1]
        if any(not isinstance(stmt, CommentStmt) for stmt in exit_block.body):
            return None
        if not isinstance(exit_block.terminator, JumpStmt):
            return None

        success_index = self.index_by_label.get(terminator.target)
        if success_index != index + 2 or success_index >= end:
            return None

        current_exit_label = exit_block.terminator.target
        if exit_label is not None and current_exit_label != exit_label:
            return None

        return success_index, current_exit_label, terminator.condition

    def is_inlineable_exit_block(self, index: int) -> bool:
        if index < 0 or index >= len(self.blocks):
            return False

        block = self.blocks[index]
        if block.successor_labels:
            return False
        if not isinstance(block.terminator, ReturnStmt):
            return False
        return all(isinstance(stmt, (CommentStmt, UnregisterExceptionStmt, SetBaseStmt)) for stmt in block.body)

    def render_inline_exit_block(self, index: int, indent: str) -> list[str]:
        block = self.blocks[index]
        lines: list[str] = []
        for stmt in block.body:
            lines.extend(render_stmt(stmt, indent, self.array_locals, self.pointer_param_sizes, self.registered_label_targets))
        lines.extend(render_terminator(block, self.function, indent, self.array_locals, self.pointer_param_sizes, self.registered_label_targets))
        return lines

    def match_guard_exit_chain(self, index: int, end: int) -> tuple[Expr, int, int, bool] | None:
        first_pair = self.is_guard_exit_pair(index, end)
        if first_pair is None:
            return None

        conditions: list[Expr] = []
        current_index = index
        exit_label: str | None = None
        body_start = -1

        while True:
            pair = self.is_guard_exit_pair(current_index, end, exit_label)
            if pair is None:
                break

            next_index, exit_label, condition = pair
            if current_index != index and any(not isinstance(stmt, CommentStmt) for stmt in self.blocks[current_index].body):
                return None
            if self.predecessor_counts.get(self.blocks[next_index].label, 0) > 1:
                break

            conditions.append(condition)
            body_start = next_index

            next_pair = self.is_guard_exit_pair(next_index, end, exit_label)
            if next_pair is None:
                break
            current_index = next_index

        if not conditions or exit_label is None:
            return None

        exit_index = self.index_by_label.get(exit_label)
        if exit_index is None or body_start < 0 or body_start >= exit_index or exit_index >= end:
            return None

        return combine_conditions_with_or(conditions), body_start, exit_index, self.is_inlineable_exit_block(exit_index)

    def match_do_while(self, index: int, end: int) -> tuple[int, Expr, int] | None:
        header_label = self.blocks[index].label
        loop_nodes = self.natural_loops.get(header_label)
        if not loop_nodes or len(loop_nodes) <= 1:
            return None

        loop_indices = {self.index_by_label[label] for label in loop_nodes if label in self.index_by_label}
        if index not in loop_indices:
            return None

        exit_index = max(loop_indices) + 1
        if exit_index >= end:
            return None

        expected_loop_indices = set(range(index, exit_index))
        if loop_indices != expected_loop_indices:
            return None

        back_index = exit_index - 1
        if back_index <= index:
            return None
        back_block = self.blocks[back_index]
        if any(not isinstance(stmt, CommentStmt) for stmt in back_block.body):
            return None
        if not isinstance(back_block.terminator, JumpStmt) or back_block.terminator.target != header_label:
            return None

        if not any(
            header == header_label and self.index_by_label.get(latch) == back_index
            for latch, header in self.back_edges
        ):
            return None

        condition_index = back_index - 1
        if condition_index < index:
            return None
        condition_block = self.blocks[condition_index]
        terminator = condition_block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None

        exit_label = self.immediate_postdominators.get(condition_block.label)
        if exit_label is None or terminator.target != exit_label:
            return None
        if self.index_by_label.get(exit_label) != exit_index:
            return None

        true_label, false_label = self.branch_true_false_labels(condition_index, terminator)
        if false_label != exit_label or true_label != back_block.label:
            return None

        if not self.slice_has_only_allowed_exits(index, exit_index, {header_label, exit_label}):
            return None

        if not self.slice_is_dominated_by(index + 1, exit_index, header_label):
            return None

        return condition_index, terminator.condition, exit_index

    def match_infinite_self_loop(self, index: int) -> bool:
        block = self.blocks[index]
        return isinstance(block.terminator, JumpStmt) and block.terminator.target == block.label

    def match_guard_goto(self, index: int, end: int) -> tuple[int, str] | None:
        block = self.blocks[index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None

        target_index = self.index_by_label.get(terminator.target)
        if target_index is None or target_index <= index + 1 or target_index >= end:
            return None

        then_block = self.blocks[index + 1]
        if any(not isinstance(stmt, CommentStmt) for stmt in then_block.body):
            return None
        if not isinstance(then_block.terminator, JumpStmt):
            return None
        return target_index, then_block.terminator.target

    def match_select_color_loop(self, index: int, end: int) -> tuple[list[tuple[Expr, int]], int] | None:
        if index + 1 >= end:
            return None

        header = self.blocks[index]
        if not contains_call(header.body, "SelectColor", "SelectColorEx", "f_17c"):
            return None

        cases: list[tuple[Expr, int]] = []
        current_index = index
        header_label = header.label

        while True:
            condition_block = self.blocks[current_index]
            terminator = condition_block.terminator
            if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
                return None

            preview_index = current_index + 1
            if preview_index >= end:
                return None
            preview_block = self.blocks[preview_index]
            if not isinstance(preview_block.terminator, JumpStmt) or preview_block.terminator.target != header_label:
                return None
            if self.predecessor_counts.get(preview_block.label, 0) > 1:
                return None

            cases.append((terminator.condition, preview_index))

            false_index = self.index_by_label.get(terminator.target)
            if false_index is None or false_index <= preview_index or false_index >= end:
                return None

            next_block = self.blocks[false_index]
            next_terminator = next_block.terminator
            if isinstance(next_terminator, BranchStmt) and next_terminator.jump_if_false and all(
                isinstance(stmt, CommentStmt) for stmt in next_block.body
            ):
                current_index = false_index
                continue

            return cases, false_index

    def match_positive_case_chain(self, index: int, end: int) -> tuple[list[tuple[Expr, int]], int, int | None, str | None] | None:
        if index + 1 >= end:
            return None

        cases: list[tuple[Expr, int]] = []
        subject_expr: Expr | None = None
        subject_expr_key: str | None = None
        current_index = index
        case_targets: set[str] = set()

        while True:
            if current_index >= end:
                return None

            block = self.blocks[current_index]
            if current_index != index and not is_comment_only(block.body):
                return None

            terminator = block.terminator
            if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
                return None

            case_match = extract_switch_case(terminator.condition)
            if case_match is None:
                return None

            current_subject, _ = case_match
            current_key = expr_key(current_subject)
            if subject_expr is None:
                subject_expr = current_subject
                subject_expr_key = current_key
            elif current_key != subject_expr_key:
                return None

            case_index = current_index + 1
            if case_index >= end:
                return None

            case_block = self.blocks[case_index]
            if self.predecessor_counts.get(case_block.label, 0) > 1:
                return None
            if not isinstance(case_block.terminator, JumpStmt):
                return None
            case_targets.add(case_block.terminator.target)

            cases.append((terminator.condition, case_index))

            false_index = self.index_by_label.get(terminator.target)
            if false_index is None or false_index <= case_index or false_index > end:
                return None

            if false_index == end:
                if len(cases) < 2:
                    return None
                return cases, false_index, None, None

            false_block = self.blocks[false_index]
            false_terminator = false_block.terminator
            if is_comment_only(false_block.body) and isinstance(false_terminator, BranchStmt) and false_terminator.jump_if_false:
                current_index = false_index
                continue

            # The compact case-chain renderer only consumes the condition and
            # one fallthrough block per case.  A later false target can jump
            # over separately addressed case bodies, which must remain in the
            # outer region so their labels and statements are emitted.
            if false_index != case_index + 1:
                return None

            skip_index: int | None = None
            if (
                is_comment_only(false_block.body)
                and false_block.terminator is None
                and len(false_block.successor_labels) == 1
                and self.predecessor_counts.get(false_block.label, 0) <= 1
            ):
                skip_index = self.index_by_label.get(false_block.successor_labels[0])

            if len(cases) < 2:
                return None
            common_join_label: str | None = None
            if len(case_targets) == 1:
                candidate_label = next(iter(case_targets))
                if candidate_label == self.blocks[false_index].label:
                    common_join_label = candidate_label
                elif skip_index is not None and candidate_label == self.blocks[skip_index].label:
                    common_join_label = candidate_label
            return cases, false_index, skip_index, common_join_label

    def render_select_color_loop(
        self,
        index: int,
        cases: list[tuple[Expr, int]],
        indent: str,
        show_label: bool,
    ) -> list[str]:
        block = self.blocks[index]
        lines: list[str] = []

        if self.should_render_label(block.label, show_label):
            lines.append(f"{block.label}:")

        lines.append(f"{indent}while (1) {{")
        lines.extend(self.render_block_body(block, indent + "    ", show_label=False))

        for condition, preview_index in cases:
            lines.append(f"{indent}    if ({render_expr(condition, self.array_locals, self.pointer_param_sizes)}) {{")
            lines.extend(
                self.render_block_fallback(
                    preview_index,
                    indent + "        ",
                    show_label=False,
                    suppress_terminal_jump_to=block.label,
                )
            )
            lines.append(f"{indent}        continue;")
            lines.append(f"{indent}    }}")

        lines.append(f"{indent}    break;")
        lines.append(f"{indent}}}")
        return lines

    def render_positive_case_chain(
        self,
        index: int,
        cases: list[tuple[Expr, int]],
        indent: str,
        show_label: bool,
        suppress_terminal_jump_to: str | None = None,
    ) -> list[str]:
        block = self.blocks[index]
        lines = self.render_block_body(block, indent, show_label=show_label)
        for condition, case_index in cases:
            lines.append(f"{indent}if ({render_expr(condition, self.array_locals, self.pointer_param_sizes)}) {{")
            lines.extend(
                self.render_block_fallback(
                    case_index,
                    indent + "    ",
                    show_label=False,
                    suppress_terminal_jump_to=suppress_terminal_jump_to,
                )
            )
            lines.append(f"{indent}}}")
        return lines

    def match_switch(
        self,
        index: int,
        end: int,
        region_exit_labels: tuple[str, ...] = (),
    ) -> tuple[Expr, list[tuple[int, int, int]], tuple[int, int] | None, int, str | None] | None:
        block = self.blocks[index]
        if block.terminator is None or not isinstance(block.terminator, BranchStmt) or not block.terminator.jump_if_false:
            return None

        cases: list[tuple[int, int, int]] = []
        seen_case_values: set[int] = set()
        subject_expr: Expr | None = None
        subject_expr_key: str | None = None
        join_label: str | None = None
        common_join_possible = True
        current_index = index

        while current_index < end:
            current_block = self.blocks[current_index]
            terminator = current_block.terminator
            if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
                return None

            case_match = extract_switch_case(terminator.condition)
            if case_match is None:
                return None
            current_subject, case_value = case_match
            current_key = expr_key(current_subject)
            if subject_expr is None:
                subject_expr = current_subject
                subject_expr_key = current_key
            elif current_key != subject_expr_key:
                return None
            if case_value in seen_case_values:
                return None
            seen_case_values.add(case_value)

            false_label = terminator.target
            false_index = self.index_by_label.get(false_label)
            false_exits_region = false_label in region_exit_labels and (false_index is None or false_index >= end)
            if false_index is None:
                if not false_exits_region:
                    return None
                false_index = end
            elif false_index <= current_index + 1:
                return None
            elif false_index >= end:
                if not false_exits_region:
                    return None
                false_index = end

            case_start = current_index + 1
            case_end = false_index
            case_last_block = self.blocks[case_end - 1]
            if not isinstance(case_last_block.terminator, (JumpStmt, ReturnStmt)):
                return None
            if isinstance(case_last_block.terminator, JumpStmt):
                if join_label is None:
                    join_label = case_last_block.terminator.target
                elif case_last_block.terminator.target != join_label:
                    common_join_possible = False
            else:
                common_join_possible = False

            cases.append((case_value, case_start, case_end))
            if false_index == end:
                if len(cases) < 2:
                    return None
                if common_join_possible and join_label == false_label:
                    return subject_expr, cases, None, end, join_label
                if len(cases) < 3:
                    return None
                return subject_expr, cases, None, end, None

            next_block = self.blocks[false_index]
            if common_join_possible and join_label is not None and next_block.label == join_label:
                if len(cases) < 2:
                    return None
                return subject_expr, cases, None, false_index, join_label
            if isinstance(next_block.terminator, BranchStmt) and next_block.terminator.jump_if_false:
                current_index = false_index
                continue
            if common_join_possible and isinstance(next_block.terminator, JumpStmt) and join_label is not None and next_block.terminator.target == join_label:
                end_index = self.index_by_label.get(join_label)
                if end_index is None or end_index <= false_index or end_index > end:
                    return None
                return subject_expr, cases, (false_index, false_index + 1), end_index, join_label
            if isinstance(next_block.terminator, (JumpStmt, ReturnStmt)) and false_index + 1 <= end and len(cases) >= 3:
                return subject_expr, cases, (false_index, false_index + 1), false_index + 1, None
            return None

        return None

    def next_block_label(self, index: int) -> str | None:
        next_index = index + 1
        if next_index >= len(self.blocks):
            return None
        return self.blocks[next_index].label

    def branch_true_false_labels(self, index: int, branch: BranchStmt) -> tuple[str | None, str | None]:
        next_label = self.next_block_label(index)
        if branch.jump_if_false:
            return next_label, branch.target
        return branch.target, next_label

    def slice_labels(self, start: int, end: int) -> set[str]:
        return {self.blocks[index].label for index in range(start, end)}

    def slice_has_only_allowed_exits(self, start: int, end: int, allowed_exits: set[str]) -> bool:
        if start >= end:
            return False

        labels = self.slice_labels(start, end)
        allowed = labels | allowed_exits
        for index in range(start, end):
            block = self.blocks[index]
            for successor in block.successor_labels:
                if successor not in allowed:
                    return False
        return True

    def slice_is_dominated_by(self, start: int, end: int, header_label: str) -> bool:
        if start >= end:
            return False
        for index in range(start, end):
            if header_label not in self.dominators.get(self.blocks[index].label, set()):
                return False
        return True

    def match_while(self, index: int, end: int) -> int | None:
        block = self.blocks[index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None
        # Statements computing the test must run on every back edge. A plain
        # while region currently renders this block before the loop, so keep
        # the explicit CFG/dispatch form when it contains actual statements.
        if any(not isinstance(stmt, CommentStmt) for stmt in block.body):
            return None

        header_label = block.label
        loop_nodes = self.natural_loops.get(header_label)
        if not loop_nodes or len(loop_nodes) <= 1:
            return None

        exit_label = self.immediate_postdominators.get(header_label)
        if exit_label is None or terminator.target != exit_label:
            return None

        exit_index = self.index_by_label.get(exit_label)
        if exit_index is None or exit_index <= index + 1 or exit_index > end:
            return None

        loop_indices = {self.index_by_label[label] for label in loop_nodes if label in self.index_by_label}
        expected_loop_indices = set(range(index, exit_index))
        if loop_indices != expected_loop_indices:
            return None

        if not any(
            header == header_label and index < self.index_by_label.get(latch, -1) < exit_index
            for latch, header in self.back_edges
        ):
            return None

        if not self.slice_has_only_allowed_exits(index + 1, exit_index, {header_label, exit_label}):
            return None

        if not self.slice_is_dominated_by(index + 1, exit_index, header_label):
            return None

        return exit_index

    def match_if_else(self, index: int, end: int) -> tuple[int, int] | None:
        block = self.blocks[index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None

        header_label = block.label
        join_label = self.immediate_postdominators.get(header_label)
        if join_label is None or join_label == terminator.target:
            return None

        else_index = self.index_by_label.get(terminator.target)
        join_index = self.index_by_label.get(join_label)
        if else_index is None or join_index is None:
            return None
        if else_index <= index + 1 or else_index >= join_index or join_index > end:
            return None

        true_label, false_label = self.branch_true_false_labels(index, terminator)
        if false_label != terminator.target or true_label != self.blocks[index + 1].label:
            return None

        if not self.slice_has_only_allowed_exits(index + 1, else_index, {join_label}):
            return None

        if not self.slice_has_only_allowed_exits(else_index, join_index, {join_label}):
            return None

        if not self.slice_is_dominated_by(index + 1, join_index, header_label):
            return None

        return else_index, join_index

    def match_if(self, index: int, end: int) -> int | None:
        block = self.blocks[index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
            return None

        header_label = block.label
        join_label = self.immediate_postdominators.get(header_label)
        if join_label is None or join_label != terminator.target:
            return None

        join_index = self.index_by_label.get(join_label)
        if join_index is None or join_index <= index + 1 or join_index > end:
            return None

        true_label, false_label = self.branch_true_false_labels(index, terminator)
        if false_label != join_label or true_label != self.blocks[index + 1].label:
            return None

        if not self.slice_has_only_allowed_exits(index + 1, join_index, {join_label}):
            return None

        if not self.slice_is_dominated_by(index + 1, join_index, header_label):
            return None

        return join_index

    def match_conjunctive_if_chain(self, index: int, end: int) -> tuple[Expr, int, int, int] | None:
        first_block = self.blocks[index]
        first_terminator = first_block.terminator
        if not isinstance(first_terminator, BranchStmt) or not first_terminator.jump_if_false:
            return None

        join_label = self.immediate_postdominators.get(first_block.label)
        join_index = self.index_by_label.get(join_label) if join_label is not None else None
        if join_index is None or join_index <= index + 2 or join_index > end:
            return None

        conditions: list[Expr] = []
        current_index = index
        while current_index < join_index:
            block = self.blocks[current_index]
            terminator = block.terminator
            if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
                break
            if current_index != index and not is_comment_only(block.body):
                break

            true_label, false_label = self.branch_true_false_labels(current_index, terminator)
            next_index = current_index + 1
            if false_label != join_label or next_index >= join_index:
                break
            if true_label != self.blocks[next_index].label:
                break
            if self.predecessor_counts.get(self.blocks[next_index].label, 0) != 1:
                break

            conditions.append(terminator.condition)
            current_index = next_index

        if len(conditions) < 2 or current_index >= join_index:
            return None
        if not self.slice_has_only_allowed_exits(current_index, join_index, {join_label}):
            return None
        if not self.slice_is_dominated_by(current_index, join_index, first_block.label):
            return None

        return combine_conditions_with_and(conditions), current_index, join_index, len(conditions)

    def render_block_body(self, block: BasicBlock, indent: str, show_label: bool = True) -> list[str]:
        lines: list[str] = []
        if self.should_render_label(block.label, show_label):
            lines.append(f"{block.label}:")
        index = 0
        while index < len(block.body):
            select_setup = match_select_setup(block.body, index)
            if select_setup is not None:
                next_index, options, include_initialize, outline = select_setup
                lines.extend(render_select_setup(options, indent, include_initialize, outline))
                index = next_index
                continue

            lines.extend(render_stmt(block.body[index], indent, self.array_locals, self.pointer_param_sizes, self.registered_label_targets))
            index += 1
        return lines

    def render_block_fallback(
        self,
        index: int,
        indent: str,
        show_label: bool = True,
        suppress_terminal_jump_to: str | None = None,
        loop_header_label: str | None = None,
        loop_exit_label: str | None = None,
        switch_exit_label: str | None = None,
    ) -> list[str]:
        block = self.blocks[index]
        lines = self.render_block_body(block, indent, show_label=show_label)
        if isinstance(block.terminator, JumpStmt):
            if suppress_terminal_jump_to is not None and block.terminator.target == suppress_terminal_jump_to:
                return lines
            if loop_header_label is not None and block.terminator.target == loop_header_label:
                lines.append(f"{indent}continue;")
                return lines
            if loop_exit_label is not None and block.terminator.target == loop_exit_label:
                lines.append(f"{indent}break;")
                return lines
            if switch_exit_label is not None and block.terminator.target == switch_exit_label:
                lines.append(f"{indent}break;")
                return lines
            if block.terminator.target == self.next_block_label(index):
                return lines
        lines.extend(render_terminator(block, self.function, indent, self.array_locals, self.pointer_param_sizes, self.registered_label_targets))
        return lines


def emit_function(function: Function) -> list[str]:
    if function.params:
        param_types = function.param_types or ["long"] * len(function.params)
        param_list = ", ".join(
            f"{param_types[index] if index < len(param_types) else 'long'} {name}"
            for index, name in enumerate(function.params)
        )
    else:
        param_list = "void"
    lines = [f"{function.return_type} {function.name}({param_list}) {{"]
    local_declarations, array_locals = collect_function_local_declarations(function)
    pointer_param_sizes = collect_pointer_param_sizes(function)
    if local_declarations:
        lines.extend(local_declarations)
        lines.append("")
    registered_label_targets = build_registered_label_target_map(function)
    lines.extend(StructuredEmitter(function, array_locals, pointer_param_sizes, registered_label_targets).render())
    lines.append("}")
    return lines


@builtin_resolver_scope()
def emit_module(module: Module) -> str:
    lines = ['#include "bgi_defs.h"', ""]
    metadata = module.metadata

    if metadata.header_text:
        lines.append(f'BGI_HEADER("{escape_string(metadata.header_text)}");')
    if metadata.header_padding:
        lines.append(f'BGI_HEADER_PADDING("{metadata.header_padding}");')
    for import_name in metadata.imports:
        lines.append(f'BGI_IMPORT("{escape_string(import_name)}");')
    if metadata.header_text or metadata.header_padding or metadata.imports:
        lines.append("")

    for index, function in enumerate(module.functions):
        lines.extend(emit_function(function))
        if index != len(module.functions) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"
