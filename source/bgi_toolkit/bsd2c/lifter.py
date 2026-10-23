from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re

from .asm_parser import AssemblyFunction, AssemblyModule, AsmInstruction, AsmLabel
from .builtin_functions import builtin_resolver_scope, call_opcode, load_builtin_resolver, signature_opcode
from .ignore_config import DEFAULT_IGNORE_BUILTIN_CONFIG, IGNORED_BUILTIN_OPCODES, IgnoreBuiltinConfig
from .integer_semantics import trunc_div, trunc_mod
from .ir import (
    AddressOf,
    ArgRef,
    AssignStmt,
    BasePointer,
    BasicBlock,
    BinaryExpr,
    BranchStmt,
    CallExpr,
    CommentStmt,
    DerefExpr,
    Expr,
    ExprStmt,
    Function,
    IntLiteral,
    InternalLabelCallStmt,
    JumpStmt,
    LabelRef,
    LocalVar,
    Module,
    ModuleMetadata,
    NamedVar,
    ParamRef,
    RawStmt,
    RegisteredScriptJumpStmt,
    RegisterExceptionStmt,
    ReturnStmt,
    SetBaseStmt,
    StackAllocStmt,
    Stmt,
    StringLiteral,
    UnaryExpr,
    UnknownExpr,
    UnregisterExceptionStmt,
)
from .signatures import ParamSpec, Signature, SignatureLibrary, load_builtin_signature_library


ARITHMETIC_OPS = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "mod": "%",
    "and": "&",
    "or": "|",
    "xor": "^",
    "shl": "<<",
    "shr": ">>",
    "sar": ">>",
}

COMPARE_OPS = {
    "eq": "==",
    "neq": "!=",
    "lt": "<",
    "gt": ">",
    "leq": "<=",
    "geq": ">=",
    "bool_and": "&&",
    "bool_or": "||",
}

CORE_OPS = {
    "push_dword",
    "push_string",
    "push_offset",
    "push_base_offset",
    "load_base",
    "store_base",
    "load",
    "move",
    "move_arg",
    "jmp",
    "jc",
    "call",
    "ret",
    "reg_exception_handler",
    "unreg_exception_handler",
    "line",
    "not",
    "nargs",
    "bool_zero",
}


def is_bs5_like_source_type(source_type: str) -> bool:
    return source_type in {"bs5", "bsb", "bss"}


CORE_OPS.update(ARITHMETIC_OPS)
CORE_OPS.update(COMPARE_OPS)

TERMINATING_OPS = {"jmp", "jc", "ret"}


def should_ignore_builtin_call(
    builtin_name: str,
    source_type: str,
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    signature_library: SignatureLibrary | None = None,
) -> bool:
    config = ignore_builtin_config or DEFAULT_IGNORE_BUILTIN_CONFIG
    ignored_names = config.names_for_source_type(source_type)
    resolve = signature_library.resolve_opcode if signature_library is not None else load_builtin_resolver().resolve
    opcode = resolve(builtin_name)
    if opcode is not None and any(
        (IGNORED_BUILTIN_OPCODES.get(name.removeprefix("f_")) or resolve(name)) == opcode
        for name in ignored_names
    ):
        return True
    if builtin_name in ignored_names:
        return True
    if builtin_name.startswith("f_"):
        return builtin_name[2:] in ignored_names
    return f"f_{builtin_name}" in ignored_names


def is_terminating_opcode(opcode: str, source_type: str) -> bool:
    if opcode in TERMINATING_OPS:
        return True
    return source_type in {"bs5", "bss"} and load_builtin_resolver().resolve(opcode) in {"f_016", "f_017"}


@dataclass(slots=True)
class RawBlock:
    label: str
    instructions: list[AsmInstruction]


def strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1]
    return token.replace(r'\"', '"').replace(r"\\", "\\")


def resolve_builtin_call_name(opcode: str, signature: Signature | None) -> str:
    return (signature_opcode(signature) if signature is not None else None) or load_builtin_resolver().resolve(opcode) or opcode


def token_to_expr(token: str, source_line: int | None = None, literal_index: int | None = None) -> Expr:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return StringLiteral(strip_quotes(token), source_line, literal_index)
    try:
        return IntLiteral(int(token, 0))
    except ValueError:
        return LabelRef(token)


def pop_expr(stack: list[Expr], fallback: str) -> Expr:
    if stack:
        return stack.pop()
    return UnknownExpr(fallback)


def as_lvalue(expr: Expr, size: int) -> Expr:
    if isinstance(expr, AddressOf) and isinstance(expr.target, LocalVar):
        return expr.target
    return DerefExpr(expr, size)


def match_stack_adjust(expr: Expr) -> StackAllocStmt | SetBaseStmt:
    if (
        isinstance(expr, BinaryExpr)
        and isinstance(expr.left, BasePointer)
        and isinstance(expr.right, IntLiteral)
        and expr.op == "+"
    ):
        return StackAllocStmt(expr.right.value)
    return SetBaseStmt(expr)


def stack_effect(instr: AsmInstruction) -> tuple[int, int] | None:
    opcode = instr.opcode
    if opcode in {"push_dword", "push_string", "push_offset", "push_base_offset", "load_base"}:
        return 0, 1
    if opcode in {"store_base", "reg_exception_handler", "jmp", "unreg_exception_handler", "move_arg"}:
        return 1, -1
    if opcode == "load":
        return 1, 0
    if opcode == "move":
        return 2, -2
    if opcode in ARITHMETIC_OPS or opcode in COMPARE_OPS:
        return 2, -1
    if opcode in {"not", "bool_zero"}:
        return 1, 0
    if opcode == "jc":
        return 2, -2
    if opcode == "nargs":
        return 0, 0
    return None


def infer_call_arg_count_from_depth(stack_depth: int, next_item: AsmInstruction | None) -> int:
    if next_item and next_item.opcode == "move" and stack_depth:
        return max(0, stack_depth - 1)
    return stack_depth


def consume_dependency_args(stack: list[bool], arg_count: int) -> list[bool]:
    actual = min(arg_count, len(stack))
    if actual == 0:
        return []
    start = len(stack) - actual
    consumed = stack[start:]
    del stack[start:]
    return consumed


def infer_result_usage(
    prefix_depth: int,
    instructions: list[AsmInstruction],
    start_index: int = 0,
    builtin_signatures: SignatureLibrary | None = None,
) -> bool:
    stack = [False] * prefix_depth + [True]
    pending_nargs: int | None = None

    for index in range(start_index, len(instructions)):
        instr = instructions[index]
        next_item = instructions[index + 1] if index + 1 < len(instructions) else None
        opcode = instr.opcode
        if opcode not in CORE_OPS:
            resolve = builtin_signatures.resolve_opcode if builtin_signatures is not None else load_builtin_resolver().resolve
            opcode = resolve(opcode) or opcode

        if opcode in {"f_07b", "f_07e", "line"}:
            return False

        # Source line metadata separates compiled statements.  It is ignored
        # in the rendered output, but a pending call result must not be
        # inferred as an argument to a later statement across this marker.
        signature = builtin_signatures.get(opcode) if builtin_signatures is not None else None
        builtin_name = resolve_builtin_call_name(opcode, signature)
        if builtin_name in {"f_0fe", "f_0fb"}:
            return False

        if opcode == "nargs":
            pending_nargs = int(instr.args[0], 0) if instr.args else None
            continue

        if opcode in {"push_dword", "push_string", "push_offset", "push_base_offset", "load_base"}:
            stack.append(False)
            continue

        if opcode == "load":
            if not stack:
                return False
            depends_on_result = stack.pop()
            stack.append(depends_on_result)
            continue

        if opcode in ARITHMETIC_OPS or opcode in COMPARE_OPS:
            if len(stack) < 2:
                return False
            right = stack.pop()
            left = stack.pop()
            stack.append(left or right)
            continue

        if opcode in {"not", "bool_zero"}:
            if not stack:
                return False
            depends_on_result = stack.pop()
            stack.append(depends_on_result)
            continue

        if opcode == "move":
            if len(stack) < 2:
                return False
            value = stack.pop()
            target = stack.pop()
            return value or target

        if opcode == "move_arg":
            if not stack:
                return False
            target = stack.pop()
            value = stack.pop() if stack else False
            return value or target

        if opcode in {"store_base", "reg_exception_handler", "jmp", "unreg_exception_handler", "ret"}:
            if not stack:
                return False
            return stack.pop()

        if opcode == "jc":
            if len(stack) < 2:
                return False
            target = stack.pop()
            condition = stack.pop()
            return target or condition

        if opcode == "call":
            if not stack:
                return False

            callee = stack.pop()
            if callee:
                return True

            arg_total = pending_nargs if pending_nargs is not None else infer_call_arg_count_from_depth(len(stack), next_item)
            pending_nargs = None
            consumed = consume_dependency_args(stack, arg_total)
            if any(consumed):
                return True
            stack.append(False)
            continue

        if opcode == "f_01d":
            if not stack:
                return False
            return stack.pop()

        if opcode not in CORE_OPS:
            arg_total = pending_nargs if pending_nargs is not None else infer_call_arg_count_from_depth(len(stack), next_item)
            pending_nargs = None
            consumed = consume_dependency_args(stack, arg_total)
            if any(consumed):
                return True
            signature = builtin_signatures.get(opcode) if builtin_signatures is not None else None
            if signature is not None and signature.returns_value:
                stack.append(False)
            continue

        effect = stack_effect(instr)
        if effect is None:
            return False

        required, delta = effect
        if len(stack) < required:
            return False
        for _ in range(required):
            stack.pop()
        if delta > 0:
            stack.extend(False for _ in range(delta))

    return False


def consume_direct_call_args(stack: list[Expr], arg_count: int, preserve_order: bool = False) -> list[Expr]:
    actual = min(arg_count, len(stack))
    if preserve_order:
        if actual == 0:
            return []
        start = len(stack) - actual
        args = stack[start:]
        del stack[start:]
        return args
    return [stack.pop() for _ in range(actual)]


def _direct_call_param_stack_width(type_name: str) -> int:
    return 2 if type_name == "label" else 1


def expand_direct_call_arg_count(signature: Signature | None, logical_arg_count: int) -> int:
    if signature is None:
        return logical_arg_count

    expanded = 0
    bounded_count = min(logical_arg_count, len(signature.params))
    for spec in signature.params[:bounded_count]:
        expanded += _direct_call_param_stack_width(spec.type_name)
    if logical_arg_count > bounded_count:
        expanded += logical_arg_count - bounded_count
    return expanded


def normalize_direct_call_args(args: list[Expr], signature: Signature | None) -> list[Expr]:
    if signature is None or not args:
        return args

    normalized: list[Expr] = []
    raw_index = 0
    for spec in signature.params:
        if raw_index >= len(args):
            break
        if spec.type_name == "label":
            if raw_index + 1 < len(args):
                normalized.append(args[raw_index + 1])
                raw_index += 2
            else:
                normalized.append(args[raw_index])
                raw_index += 1
            continue
        normalized.append(args[raw_index])
        raw_index += 1

    if raw_index < len(args):
        normalized.extend(args[raw_index:])
    return normalized


def resolve_direct_call_args(
    item: AsmInstruction,
    stack: list[Expr],
    arg_count: int,
    preserve_order: bool = False,
) -> list[Expr]:
    inline_args = []
    literal_index = 0
    for arg in item.args:
        expr = token_to_expr(arg, item.line_no, literal_index)
        inline_args.append(expr)
        if isinstance(expr, StringLiteral):
            literal_index += 1
    if not inline_args:
        return consume_direct_call_args(stack, arg_count, preserve_order=preserve_order)
    if not stack:
        return inline_args

    remaining_arg_count = max(0, arg_count - len(inline_args))
    stack_args = consume_direct_call_args(stack, remaining_arg_count, preserve_order=preserve_order)
    if preserve_order:
        return [*stack_args, *inline_args]
    return [*inline_args, *stack_args]


def consume_user_call_args(stack: list[Expr], arg_count: int) -> list[Expr]:
    actual = min(arg_count, len(stack))
    if actual == 0:
        return []
    start = len(stack) - actual
    args = stack[start:]
    del stack[start:]
    return args


def split_user_call_args(stack: list[Expr], arg_count: int) -> tuple[list[Expr], list[Expr]]:
    actual = min(arg_count, len(stack))
    if actual == 0:
        leading = list(stack)
        stack.clear()
        return leading, []
    start = len(stack) - actual
    leading = stack[:start]
    args = stack[start:]
    stack.clear()
    return leading, args


def infer_call_arg_count(stack: list[Expr], next_item: AsmInstruction | None) -> int:
    return infer_call_arg_count_from_depth(len(stack), next_item)


def callsite_has_explicit_expression_boundary(next_item: AsmInstruction | None) -> bool:
    return next_item is None or (load_builtin_resolver().resolve(next_item.opcode) or next_item.opcode) in {
        "f_07e",
        "line",
        "move",
        "move_arg",
        "ret",
        "unreg_exception_handler",
    }


def infer_function_arg_count(function: AssemblyFunction) -> int:
    instructions = [item for item in function.items if isinstance(item, AsmInstruction)]
    index = 0

    while index < len(instructions) and instructions[index].opcode in {"load_base", "push_dword", "add", "store_base"}:
        index += 1

    count = 0
    while index + 1 < len(instructions):
        current = instructions[index]
        following = instructions[index + 1]
        if current.opcode == "push_base_offset" and following.opcode == "move_arg":
            count += 1
            index += 2
            continue
        break
    return count


def build_internal_arity_map(module: AssemblyModule) -> dict[str, int]:
    return {function.name: infer_function_arg_count(function) for function in module.functions}


def build_internal_call_name_map(module: AssemblyModule) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for function in module.functions:
        if not function.items:
            continue
        for item in function.items:
            if not isinstance(item, AsmLabel):
                break
            mapping[item.name] = function.name
    return mapping


def make_synthetic_label(function_name: str, block_index: int) -> str:
    return f"{function_name}__bb{block_index}"


def split_basic_blocks(function: AssemblyFunction, source_type: str) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    current_label: str | None = None
    current_instructions: list[AsmInstruction] = []
    synthetic_index = 0

    def ensure_label() -> str:
        nonlocal current_label, synthetic_index
        if current_label is None:
            current_label = make_synthetic_label(function.name, synthetic_index)
            synthetic_index += 1
        return current_label

    def flush() -> None:
        nonlocal current_label, current_instructions
        if current_label is None and not current_instructions:
            return
        blocks.append(RawBlock(label=ensure_label(), instructions=current_instructions))
        current_label = None
        current_instructions = []

    for item in function.items:
        if isinstance(item, AsmLabel):
            if current_instructions:
                flush()
            current_label = item.name
            continue

        ensure_label()
        current_instructions.append(item)
        if is_terminating_opcode(item.opcode, source_type):
            flush()

    if current_label is not None or current_instructions:
        flush()

    return blocks


def lift_direct_call(
    item: AsmInstruction,
    stack: list[Expr],
    next_item: AsmInstruction | None,
    following: list[AsmInstruction],
    pending_nargs: int | None,
    builtin_signatures: SignatureLibrary,
    source_type: str,
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    statement_stack_base_depth: int,
    following_index: int = 0,
) -> tuple[ExprStmt | None, Expr | None]:
    opcode = item.opcode
    signature = builtin_signatures.get(opcode)
    builtin_name = resolve_builtin_call_name(opcode, signature)
    statement_stack_depth = max(0, len(stack) - min(statement_stack_base_depth, len(stack)))
    inferred_arg_total = infer_call_arg_count_from_depth(statement_stack_depth, next_item) + len(item.args)
    required_arg_total = expand_direct_call_arg_count(signature, signature.required_arg_count) if signature is not None else 0
    signature_arg_total = expand_direct_call_arg_count(signature, signature.arg_count) if signature is not None else 0
    if pending_nargs is not None:
        arg_total = expand_direct_call_arg_count(signature, pending_nargs)
        if signature is not None and signature.variadic:
            arg_total = max(arg_total, required_arg_total)
    elif signature is not None:
        if signature.variadic:
            arg_total = max(required_arg_total, len(item.args), inferred_arg_total)
        elif callsite_has_explicit_expression_boundary(next_item):
            arg_total = inferred_arg_total
        else:
            arg_total = min(signature_arg_total, max(required_arg_total, inferred_arg_total))
    elif item.args:
        arg_total = len(item.args)
    else:
        arg_total = inferred_arg_total
    raw_args = resolve_direct_call_args(item, stack, arg_total, preserve_order=is_bs5_like_source_type(source_type))
    args = normalize_direct_call_args(raw_args, signature)
    if should_ignore_builtin_call(builtin_name, source_type, ignore_builtin_config, builtin_signatures):
        return None, None
    call_expr = CallExpr(callee=opcode, args=tuple(args), builtin_opcode=builtin_signatures.resolve_opcode(opcode) or "")
    # These opcodes are compiler statement metadata, with inline operands and
    # no VM result. Keeping them visible must not add a phantom argument to
    # the next real call (text scans keep them; normal C output hides them).
    if builtin_name in {"f_07b", "f_07e"} or (signature is not None and not signature.returns_value):
        return ExprStmt(call_expr), None
    if infer_result_usage(len(stack), following, following_index, builtin_signatures):
        return None, call_expr
    return ExprStmt(call_expr), None


def extract_registered_label_name(args: list[Expr]) -> str | None:
    if len(args) != 1:
        return None
    if not isinstance(args[0], StringLiteral):
        return None
    return args[0].value


def flush_pending_side_effects(block: BasicBlock, stack: list[Expr]) -> None:
    """Materialize call results that cannot cross a source statement marker."""
    remaining: list[Expr] = []
    for expr in stack:
        if isinstance(expr, CallExpr):
            block.body.append(ExprStmt(expr))
        else:
            remaining.append(expr)
    stack[:] = remaining


def lift_user_call(
    stack: list[Expr],
    next_item: AsmInstruction | None,
    following: list[AsmInstruction],
    pending_nargs: int | None,
    script_arities: dict[str, int],
    internal_call_names: dict[str, str],
    script_signatures: dict[str, Signature],
    builtin_signatures: SignatureLibrary,
    following_index: int = 0,
) -> tuple[ExprStmt | None, Expr | None]:
    callee_expr = pop_expr(stack, "call_target")
    if isinstance(callee_expr, LabelRef):
        callee_name = internal_call_names.get(callee_expr.name, callee_expr.name)
    else:
        callee_name = "call"
    if callee_name in script_arities:
        arg_total = script_arities[callee_name]
    else:
        arg_total = pending_nargs if pending_nargs is not None else infer_call_arg_count(stack, next_item)
    args = consume_user_call_args(stack, arg_total)
    call_expr = CallExpr(callee=callee_name, args=tuple(args), builtin_opcode="")
    signature = script_signatures.get(callee_name)
    if signature is not None and not signature.returns_value:
        return ExprStmt(call_expr), None
    if infer_result_usage(len(stack), following, following_index, builtin_signatures):
        return None, call_expr
    return ExprStmt(call_expr), None


def lift_block(
    raw_block: RawBlock,
    arg_count: int,
    source_type: str,
    script_arities: dict[str, int],
    internal_call_names: dict[str, str],
    script_signatures: dict[str, Signature],
    builtin_signatures: SignatureLibrary,
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    call_observer=None,
) -> BasicBlock:
    block = BasicBlock(id=-1, label=raw_block.label)
    instructions = raw_block.instructions
    stack: list[Expr] = []
    arg_index = 0
    pending_nargs: int | None = None
    pending_registered_label: str | None = None
    pending_register_callee: str | None = None
    statement_stack_base_depth = 0
    index = 0

    while index < len(instructions):
        item = instructions[index]
        next_item = instructions[index + 1] if index + 1 < len(instructions) else None
        following_index = index + 1
        opcode = item.opcode
        if opcode not in CORE_OPS:
            opcode = builtin_signatures.resolve_opcode(opcode) or opcode

        if opcode == "push_dword":
            stack.append(token_to_expr(item.args[0]))
            index += 1
            continue

        if opcode == "push_string":
            stack.append(token_to_expr(item.args[0], item.line_no))
            index += 1
            continue

        if opcode == "push_offset":
            stack.append(LabelRef(item.args[0]))
            index += 1
            continue

        if opcode == "push_base_offset":
            stack.append(AddressOf(LocalVar(int(item.args[0], 0))))
            index += 1
            continue

        if opcode == "load_base":
            stack.append(BasePointer())
            index += 1
            continue

        if opcode == "store_base":
            value = pop_expr(stack, "store_base")
            block.body.append(match_stack_adjust(value))
            statement_stack_base_depth = len(stack)
            index += 1
            continue

        if opcode in ARITHMETIC_OPS:
            right = pop_expr(stack, f"{opcode}_rhs")
            left = pop_expr(stack, f"{opcode}_lhs")
            stack.append(BinaryExpr(ARITHMETIC_OPS[opcode], left, right))
            index += 1
            continue

        if opcode in COMPARE_OPS:
            right = pop_expr(stack, f"{opcode}_rhs")
            left = pop_expr(stack, f"{opcode}_lhs")
            stack.append(BinaryExpr(COMPARE_OPS[opcode], left, right))
            index += 1
            continue

        if opcode == "bool_zero":
            stack.append(BinaryExpr("==", pop_expr(stack, "bool_zero"), IntLiteral(0)))
            index += 1
            continue

        if opcode == "not":
            stack.append(UnaryExpr("~", pop_expr(stack, "not")))
            index += 1
            continue

        if opcode == "load":
            size = int(item.args[0], 0)
            address = pop_expr(stack, "load_addr")
            if isinstance(address, AddressOf) and isinstance(address.target, LocalVar):
                stack.append(address.target)
            else:
                stack.append(DerefExpr(address, size))
            index += 1
            continue

        if opcode == "move":
            size = int(item.args[0], 0)
            value = pop_expr(stack, "move_value")
            target = as_lvalue(pop_expr(stack, "move_target"), size)
            block.body.append(AssignStmt(target=target, value=value))
            statement_stack_base_depth = len(stack)
            index += 1
            continue

        if opcode == "move_arg":
            size = int(item.args[0], 0)
            target = as_lvalue(pop_expr(stack, "move_arg_target"), size)
            if stack:
                value = pop_expr(stack, "move_arg_value")
                block.body.append(AssignStmt(target=target, value=value))
            else:
                final_index = arg_count - 1 - arg_index if source_type in {"bsc", "bss"} and arg_count else arg_index
                block.body.append(AssignStmt(target=target, value=ArgRef(size=size, index=max(0, final_index))))
                arg_index += 1
            statement_stack_base_depth = len(stack)
            index += 1
            continue

        if opcode == "reg_exception_handler":
            target = pop_expr(stack, "exception_target")
            if isinstance(target, LabelRef):
                block.body.append(RegisterExceptionStmt(target.name))
            else:
                block.body.append(RawStmt("reg_exception_handler();"))
            index += 1
            continue

        if opcode == "unreg_exception_handler":
            block.body.append(UnregisterExceptionStmt())
            index += 1
            continue

        if opcode == "line":
            statement_stack_base_depth = len(stack)
            index += 1
            continue

        if opcode == "nargs":
            pending_nargs = int(item.args[0], 0) if item.args else None
            index += 1
            continue

        if source_type in {"bs5", "bss"} and opcode == "f_016":
            target = pop_expr(stack, "f_016_target")
            pending_nargs = None
            if isinstance(target, LabelRef):
                block.terminator = JumpStmt(target.name)
            else:
                block.terminator = RawStmt("f_016();")
            break

        if source_type in {"bs5", "bss"} and opcode == "f_017":
            target = pop_expr(stack, "f_017_target")
            condition = pop_expr(stack, "f_017_condition")
            pending_nargs = None
            if isinstance(target, LabelRef):
                block.terminator = BranchStmt(condition=condition, target=target.name, jump_if_false=True)
            else:
                block.terminator = RawStmt("f_017();")
            break

        if opcode == "jmp":
            target = pop_expr(stack, "jmp_target")
            if isinstance(target, LabelRef):
                block.terminator = JumpStmt(target.name)
            else:
                block.terminator = RawStmt("jmp();")
            break

        if opcode == "jc":
            target = pop_expr(stack, "jc_target")
            condition = pop_expr(stack, "jc_condition")
            if isinstance(target, LabelRef):
                block.terminator = BranchStmt(condition=condition, target=target.name, jump_if_false=True)
            else:
                block.terminator = RawStmt("jc();")
            break

        if opcode == "ret":
            flush_pending_side_effects(block, stack)
            block.terminator = ReturnStmt()
            break

        if opcode == "call":
            if pending_registered_label is not None and stack and isinstance(stack[-1], LabelRef):
                callee_expr = pop_expr(stack, "call_target")
                arg_total = pending_nargs if pending_nargs is not None else infer_call_arg_count(stack, next_item)
                args = consume_user_call_args(stack, arg_total)
                if isinstance(callee_expr, LabelRef) and not args:
                    block.body.append(
                        InternalLabelCallStmt(
                            registered_name=pending_registered_label,
                            target=callee_expr.name,
                        )
                    )
                    pending_registered_label = None
                    pending_nargs = None
                    index += 1
                    continue
                stack.append(callee_expr)
                stack.extend(args)
                pending_registered_label = None

            stmt, result_expr = lift_user_call(
                stack,
                next_item,
                instructions,
                pending_nargs,
                script_arities,
                internal_call_names,
                script_signatures,
                builtin_signatures,
                following_index=following_index,
            )
            pending_nargs = None
            if call_observer is not None:
                call_expr = stmt.value if isinstance(stmt, ExprStmt) else result_expr
                if isinstance(call_expr, CallExpr):
                    call_observer(item, call_expr, block)
            if stmt is not None:
                block.body.append(stmt)
                statement_stack_base_depth = len(stack)
            if result_expr is not None:
                stack.append(result_expr)
            index += 1
            continue

        if opcode == "f_01d":
            args = consume_direct_call_args(stack, 1)
            block.terminator = ReturnStmt(args[0] if args else IntLiteral(0))
            maybe_push = next_item
            maybe_jump = raw_block.instructions[index + 2] if index + 2 < len(raw_block.instructions) else None
            if maybe_push and maybe_push.opcode == "push_offset" and maybe_jump and maybe_jump.opcode == "jmp":
                index += 3
            break

        if opcode not in CORE_OPS:
            signature = builtin_signatures.get(opcode)
            builtin_name = resolve_builtin_call_name(opcode, signature)
            if builtin_name in {"f_07b", "f_0fe", "f_0fb"} and stack:
                flush_pending_side_effects(block, stack)
                statement_stack_base_depth = 0
            if builtin_name == "f_0f6":
                if pending_nargs is not None:
                    arg_total = pending_nargs
                elif signature is not None and signature.variadic:
                    arg_total = max(signature.required_arg_count, infer_call_arg_count(stack, next_item))
                else:
                    arg_total = signature.arg_count if signature is not None else 1
                args = consume_direct_call_args(stack, arg_total, preserve_order=is_bs5_like_source_type(source_type))
                registered_label = extract_registered_label_name(args)
                is_default_script_entry = (
                    len(args) == 1 and isinstance(args[0], IntLiteral) and args[0].value == 0
                )
                next_builtin_name: str | None = None
                if next_item is not None and next_item.opcode not in CORE_OPS:
                    next_signature = builtin_signatures.get(next_item.opcode)
                    next_builtin_name = resolve_builtin_call_name(next_item.opcode, next_signature)
                next_is_internal_label_call = (
                    next_item is not None
                    and next_item.opcode == "push_offset"
                    and index + 2 < len(instructions)
                    and instructions[index + 2].opcode == "call"
                )
                if registered_label is not None and (
                    (next_item is not None and next_item.opcode == "call")
                    or next_is_internal_label_call
                    or next_builtin_name in {"f_0f3", "f_0f0"}
                ):
                    pending_registered_label = registered_label
                    pending_register_callee = item.opcode
                elif is_default_script_entry and next_builtin_name in {"f_0f3", "f_0f0"}:
                    # A zero registration selects the target script's default entry.
                    # It is call metadata, not a standalone RegisterLabel invocation.
                    pending_registered_label = None
                else:
                    block.body.append(ExprStmt(CallExpr(callee=item.opcode, args=tuple(args), builtin_opcode=opcode)))
                    statement_stack_base_depth = len(stack)
                    pending_registered_label = None
                pending_nargs = None
                index += 1
                continue

            if pending_registered_label is not None and builtin_name in {"f_0f3", "f_0f0"}:
                if item.args:
                    script_arg = token_to_expr(item.args[0])
                else:
                    script_arg = pop_expr(stack, "script_target")
                block.body.append(
                    RegisteredScriptJumpStmt(
                        registered_name=pending_registered_label,
                        script=script_arg,
                        callee=item.opcode,
                        register_callee=pending_register_callee,
                    )
                )
                pending_registered_label = None
                pending_nargs = None
                index += 1
                continue

            if opcode == "f_01c" and stack and isinstance(stack[-1], StringLiteral):
                target = stack.pop()
                callee_name = target.value if isinstance(target, StringLiteral) else "f_01c"
                fallback_arg_total = pending_nargs if pending_nargs is not None else infer_call_arg_count(stack, next_item)
                arg_total = script_arities.get(callee_name, fallback_arg_total)
                signature = script_signatures.get(callee_name)
                returns_value = signature is None or signature.returns_value
                # Like a local call, an imported function consumes its own
                # arguments, leaving an assignment address or outer operands
                # below them. Do not turn that address into an extra argument.
                args = consume_user_call_args(stack, arg_total)
                call_expr = CallExpr(callee=callee_name, args=tuple(args), builtin_opcode="")
                if returns_value and infer_result_usage(len(stack), instructions, following_index, builtin_signatures):
                    stmt, result_expr = None, call_expr
                else:
                    if stack and all(isinstance(expr, CallExpr) for expr in stack):
                        block.body.extend(ExprStmt(expr) for expr in stack)
                        stack.clear()
                    stmt, result_expr = ExprStmt(call_expr), None
            else:
                stmt, result_expr = lift_direct_call(
                    item,
                    stack,
                    next_item,
                    instructions,
                    pending_nargs,
                    builtin_signatures,
                    source_type,
                    ignore_builtin_config,
                    statement_stack_base_depth,
                    following_index=following_index,
                )
            pending_nargs = None
            if call_observer is not None:
                call_expr = stmt.value if isinstance(stmt, ExprStmt) else result_expr
                if isinstance(call_expr, CallExpr):
                    call_observer(item, call_expr, block)
            if stmt is not None:
                block.body.append(stmt)
                statement_stack_base_depth = len(stack)
            pending_registered_label = None
            if result_expr is not None:
                stack.append(result_expr)
            if opcode == "f_07e":
                statement_stack_base_depth = len(stack)
            index += 1
            continue

        block.body.append(RawStmt(f"{item.text};"))
        statement_stack_base_depth = len(stack)
        index += 1

    return block


def finalize_cfg(blocks: list[BasicBlock]) -> None:
    for index, block in enumerate(blocks):
        successors: list[str] = []
        terminator = block.terminator
        next_label = blocks[index + 1].label if index + 1 < len(blocks) else None

        if isinstance(terminator, JumpStmt):
            successors.append(terminator.target)
        elif isinstance(terminator, BranchStmt):
            successors.append(terminator.target)
            if next_label is not None:
                successors.append(next_label)
        elif isinstance(terminator, ReturnStmt):
            successors = []
        elif next_label is not None:
            successors.append(next_label)

        block.successor_labels = successors


def prune_unreachable_blocks(blocks: list[BasicBlock]) -> list[BasicBlock]:
    if not blocks:
        return blocks

    block_by_label = {block.label: block for block in blocks}
    reachable: set[str] = set()
    pending = [blocks[0].label]

    while pending:
        label = pending.pop()
        if label in reachable:
            continue
        block = block_by_label.get(label)
        if block is None:
            continue
        reachable.add(label)
        pending.extend(successor for successor in block.successor_labels if successor in block_by_label)

    pruned = [block for block in blocks if block.label in reachable]
    for index, block in enumerate(pruned):
        block.id = index
    return pruned


def trim_leading_entry_blocks(blocks: list[BasicBlock]) -> list[BasicBlock]:
    trimmed = list(blocks)

    while len(trimmed) > 1:
        entry = trimmed[0]
        if entry.body:
            break
        if any(entry.label in block.successor_labels for block in trimmed[1:]):
            break

        if entry.terminator is None:
            trimmed = trimmed[1:]
            continue

        if isinstance(entry.terminator, JumpStmt) and trimmed[1].label == entry.terminator.target:
            trimmed = trimmed[1:]
            continue

        break

    for index, block in enumerate(trimmed):
        block.id = index
    return trimmed


def infer_return_type(blocks: list[BasicBlock]) -> str:
    for block in blocks:
        if isinstance(block.terminator, ReturnStmt) and block.terminator.value is not None:
            return "long"
    return "void"


def evaluate_int_constant(expr: Expr) -> int | None:
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, UnaryExpr):
        value = evaluate_int_constant(expr.value)
        if value is None:
            return None
        if expr.op == "-":
            return -value
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
    return None


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
    if isinstance(stmt, BranchStmt):
        return (stmt.condition,)
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return (stmt.value,)
    if isinstance(stmt, SetBaseStmt):
        return (stmt.value,)
    return ()


def local_use_count_in_expr(expr: Expr, counts: Counter[int]) -> None:
    if isinstance(expr, LocalVar):
        counts[expr.offset] += 1
        return
    for child in iter_expr_children(expr):
        local_use_count_in_expr(child, counts)


def local_use_count_in_stmt(stmt) -> Counter[int]:
    counts: Counter[int] = Counter()
    for expr in iter_stmt_exprs(stmt):
        local_use_count_in_expr(expr, counts)
    return counts


# Keep the existing value-call optimization policy stable when prototypes or
# display names are edited. These are the engine IDs previously selected by
# the Get/Load/Convert name prefixes.
PURE_VALUE_CALL_OPCODES = frozenset({
    "f_0aa", "f_210", "f_0a8", "f_0ac", "f_2ee", "f_2e2", "f_12f",
    "f_0e7", "f_2e5", "f_222", "f_2e4", "f_094", "f_0e5", "f_18f",
    "f_2e1", "f_2f4", "f_0ea", "f_0e8", "f_2ec", "f_266", "f_2e0",
    "f_102", "f_22a", "f_320", "f_2e7", "f_0a0", "f_0ec", "f_135",
    "f_099", "f_101", "f_100", "f_128", "f_443", "f_0e3", "f_0e1",
})


def is_side_effect_free_expr(expr: Expr, builtin_signatures: SignatureLibrary) -> bool:
    if isinstance(expr, (IntLiteral, StringLiteral, LocalVar, ParamRef, LabelRef, BasePointer)):
        return True
    if isinstance(expr, AddressOf):
        return is_side_effect_free_expr(expr.target, builtin_signatures)
    if isinstance(expr, DerefExpr):
        return is_side_effect_free_expr(expr.address, builtin_signatures)
    if isinstance(expr, BinaryExpr):
        return is_side_effect_free_expr(expr.left, builtin_signatures) and is_side_effect_free_expr(expr.right, builtin_signatures)
    if isinstance(expr, UnaryExpr):
        return is_side_effect_free_expr(expr.value, builtin_signatures)
    if isinstance(expr, CallExpr):
        opcode = builtin_signatures.resolve_opcode(expr.callee) if expr.builtin_opcode is None else call_opcode(expr)
        if opcode not in PURE_VALUE_CALL_OPCODES:
            return False
        return all(is_side_effect_free_expr(arg, builtin_signatures) for arg in expr.args)
    return False


def replace_local_in_expr(expr: Expr, offset: int, replacement: Expr) -> Expr:
    if isinstance(expr, LocalVar) and expr.offset == offset:
        return replacement
    if isinstance(expr, AddressOf):
        return AddressOf(replace_local_in_expr(expr.target, offset, replacement))
    if isinstance(expr, DerefExpr):
        return DerefExpr(replace_local_in_expr(expr.address, offset, replacement), expr.size)
    if isinstance(expr, BinaryExpr):
        return BinaryExpr(
            expr.op,
            replace_local_in_expr(expr.left, offset, replacement),
            replace_local_in_expr(expr.right, offset, replacement),
        )
    if isinstance(expr, UnaryExpr):
        return UnaryExpr(expr.op, replace_local_in_expr(expr.value, offset, replacement))
    if isinstance(expr, CallExpr):
        return CallExpr(expr.callee, tuple(replace_local_in_expr(arg, offset, replacement) for arg in expr.args), expr.builtin_opcode)
    return expr


def replace_local_in_stmt(stmt, offset: int, replacement: Expr):
    if isinstance(stmt, AssignStmt):
        return AssignStmt(stmt.target, replace_local_in_expr(stmt.value, offset, replacement))
    if isinstance(stmt, ExprStmt):
        return ExprStmt(replace_local_in_expr(stmt.value, offset, replacement))
    if isinstance(stmt, BranchStmt):
        return BranchStmt(replace_local_in_expr(stmt.condition, offset, replacement), stmt.target, stmt.jump_if_false)
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return ReturnStmt(replace_local_in_expr(stmt.value, offset, replacement))
    if isinstance(stmt, SetBaseStmt):
        return SetBaseStmt(replace_local_in_expr(stmt.value, offset, replacement))
    return stmt


def negate_condition_expr(expr: Expr) -> Expr:
    if isinstance(expr, UnaryExpr) and expr.op == "!":
        return expr.value
    return UnaryExpr("!", expr)


def count_function_local_uses(function: Function) -> Counter[int]:
    counts: Counter[int] = Counter()
    for block in function.blocks:
        for stmt in block.body:
            counts.update(local_use_count_in_stmt(stmt))
        if block.terminator is not None:
            counts.update(local_use_count_in_stmt(block.terminator))
    return counts


def collect_boolean_assignment_sites(function: Function) -> dict[int, list[tuple[str, int, int]]]:
    assignments: dict[int, list[tuple[str, int, int]]] = {}
    invalid_offsets: set[int] = set()

    for block in function.blocks:
        for index, stmt in enumerate(block.body):
            if not isinstance(stmt, AssignStmt) or not isinstance(stmt.target, LocalVar):
                continue
            offset = stmt.target.offset
            if isinstance(stmt.value, IntLiteral) and stmt.value.value in {0, 1}:
                assignments.setdefault(offset, []).append((block.label, index, stmt.value.value))
            else:
                invalid_offsets.add(offset)

    for offset in invalid_offsets:
        assignments.pop(offset, None)
    return {offset: sites for offset, sites in assignments.items() if len(sites) == 2}


def branch_path_labels(function: Function, block_index: int, branch: BranchStmt) -> tuple[str | None, str | None]:
    next_label = function.blocks[block_index + 1].label if block_index + 1 < len(function.blocks) else None
    if branch.jump_if_false:
        return next_label, branch.target
    return branch.target, next_label


def single_successor_label(block: BasicBlock) -> str | None:
    if len(block.successor_labels) != 1:
        return None
    return block.successor_labels[0]


def boolean_condition_replacement(condition: Expr, true_value: int, false_value: int) -> Expr | None:
    if true_value == 1 and false_value == 0:
        return condition
    if true_value == 0 and false_value == 1:
        return negate_condition_expr(condition)
    return None


def replace_local_uses_from_join(function: Function, dominators: dict[str, set[str]], offset: int, replacement: Expr, join_label: str) -> None:
    for block in function.blocks:
        if join_label not in dominators.get(block.label, set()):
            continue
        block.body = [replace_local_in_stmt(stmt, offset, replacement) for stmt in block.body]
        if block.terminator is not None:
            block.terminator = replace_local_in_stmt(block.terminator, offset, replacement)


def is_empty_passthrough_block(block: BasicBlock, join_label: str) -> bool:
    if block.body:
        return False
    if isinstance(block.terminator, JumpStmt):
        return block.terminator.target == join_label
    return block.terminator is None and single_successor_label(block) == join_label


def replace_label_target(stmt, old_label: str, new_label: str):
    if isinstance(stmt, JumpStmt) and stmt.target == old_label:
        return JumpStmt(new_label)
    if isinstance(stmt, BranchStmt) and stmt.target == old_label:
        return BranchStmt(stmt.condition, new_label, stmt.jump_if_false)
    if isinstance(stmt, RegisterExceptionStmt) and stmt.target == old_label:
        return RegisterExceptionStmt(new_label)
    return stmt


def fold_boolean_flag_locals(functions: list[Function]) -> None:
    for function in functions:
        if not function.blocks:
            continue

        changed = True
        while changed:
            changed = False
            block_by_label = {block.label: block for block in function.blocks}
            block_indices = {block.label: index for index, block in enumerate(function.blocks)}
            predecessors = compute_predecessor_labels(function)
            dominators = compute_dominators(function)
            assignment_sites = collect_boolean_assignment_sites(function)

            for offset, sites in assignment_sites.items():
                replacement_info: tuple[str, Expr, list[tuple[str, int]]] | None = None

                for block_label, stmt_index, block_value in sites:
                    branch_block = block_by_label.get(block_label)
                    if branch_block is None or not isinstance(branch_block.terminator, BranchStmt):
                        continue
                    if stmt_index != len(branch_block.body) - 1:
                        continue

                    branch_index = block_indices[block_label]
                    true_label, false_label = branch_path_labels(function, branch_index, branch_block.terminator)
                    if true_label is None or false_label is None:
                        continue

                    other_site = next(
                        (
                            (other_label, other_index, other_value)
                            for other_label, other_index, other_value in sites
                            if (other_label, other_index) != (block_label, stmt_index)
                        ),
                        None,
                    )
                    if other_site is None:
                        continue

                    other_label, other_index, other_value = other_site
                    if other_label not in {true_label, false_label}:
                        continue

                    other_block = block_by_label.get(other_label)
                    join_label = false_label if other_label == true_label else true_label
                    if other_block is None or join_label is None or single_successor_label(other_block) != join_label:
                        continue

                    true_value = other_value if other_label == true_label else block_value
                    false_value = other_value if other_label == false_label else block_value
                    replacement = boolean_condition_replacement(branch_block.terminator.condition, true_value, false_value)
                    if replacement is None:
                        continue

                    replacement_info = (join_label, replacement, [(block_label, stmt_index), (other_label, other_index)])
                    break

                if replacement_info is None:
                    first_site, second_site = sites
                    common_predecessors = predecessors.get(first_site[0], set()) & predecessors.get(second_site[0], set())

                    for predecessor_label in common_predecessors:
                        predecessor_block = block_by_label.get(predecessor_label)
                        if predecessor_block is None or not isinstance(predecessor_block.terminator, BranchStmt):
                            continue

                        predecessor_index = block_indices[predecessor_label]
                        true_label, false_label = branch_path_labels(function, predecessor_index, predecessor_block.terminator)
                        if {true_label, false_label} != {first_site[0], second_site[0]}:
                            continue

                        true_block = block_by_label.get(true_label) if true_label is not None else None
                        false_block = block_by_label.get(false_label) if false_label is not None else None
                        join_label = single_successor_label(true_block) if true_block is not None else None
                        if join_label is None or false_block is None or single_successor_label(false_block) != join_label:
                            continue

                        true_site = first_site if first_site[0] == true_label else second_site
                        false_site = first_site if first_site[0] == false_label else second_site
                        replacement = boolean_condition_replacement(
                            predecessor_block.terminator.condition,
                            true_site[2],
                            false_site[2],
                        )
                        if replacement is None:
                            continue

                        replacement_info = (
                            join_label,
                            replacement,
                            [(true_site[0], true_site[1]), (false_site[0], false_site[1])],
                        )
                        break

                if replacement_info is None:
                    continue

                before_uses = count_function_local_uses(function)[offset]
                join_label, replacement, removals = replacement_info
                replace_local_uses_from_join(function, dominators, offset, replacement, join_label)
                after_uses = count_function_local_uses(function)[offset]
                if after_uses >= before_uses:
                    continue

                if after_uses == 0:
                    removals_by_label: dict[str, list[int]] = {}
                    for label, stmt_index in removals:
                        removals_by_label.setdefault(label, []).append(stmt_index)
                    for label, indices in removals_by_label.items():
                        block = block_by_label[label]
                        for stmt_index in sorted(indices, reverse=True):
                            del block.body[stmt_index]

                changed = True
                break


def collapse_empty_branch_diamonds(functions: list[Function]) -> None:
    for function in functions:
        if not function.blocks:
            continue

        changed = True
        while changed:
            changed = False
            block_by_label = {block.label: block for block in function.blocks}

            for index, block in enumerate(function.blocks):
                if not isinstance(block.terminator, BranchStmt):
                    continue

                true_label, false_label = branch_path_labels(function, index, block.terminator)
                if true_label is None or false_label is None:
                    continue

                true_block = block_by_label.get(true_label)
                false_block = block_by_label.get(false_label)
                if true_block is None or false_block is None:
                    continue

                join_label = single_successor_label(true_block)
                if join_label is None or single_successor_label(false_block) != join_label:
                    continue
                if not is_empty_passthrough_block(true_block, join_label):
                    continue
                if not is_empty_passthrough_block(false_block, join_label):
                    continue

                block.terminator = JumpStmt(join_label)
                finalize_cfg(function.blocks)
                function.blocks = prune_unreachable_blocks(function.blocks)
                changed = True
                break


def bypass_empty_jump_blocks(functions: list[Function]) -> None:
    for function in functions:
        if not function.blocks:
            continue

        changed = True
        while changed:
            changed = False
            protected_labels = {function.blocks[0].label}
            for block in function.blocks:
                for stmt in block.body:
                    if isinstance(stmt, RegisterExceptionStmt):
                        protected_labels.add(stmt.target)

            for block in function.blocks:
                if block.label in protected_labels:
                    continue
                if block.body:
                    continue

                target_label: str | None = None
                if isinstance(block.terminator, JumpStmt):
                    target_label = block.terminator.target
                elif block.terminator is None and len(block.successor_labels) == 1:
                    target_label = block.successor_labels[0]

                if target_label is None or target_label == block.label:
                    continue

                redirected = False
                for holder in function.blocks:
                    updated_body = []
                    for stmt in holder.body:
                        updated_stmt = replace_label_target(stmt, block.label, target_label)
                        redirected = redirected or updated_stmt != stmt
                        updated_body.append(updated_stmt)
                    holder.body = updated_body

                    if holder.terminator is not None:
                        updated_terminator = replace_label_target(holder.terminator, block.label, target_label)
                        redirected = redirected or updated_terminator != holder.terminator
                        holder.terminator = updated_terminator

                if not redirected:
                    continue

                finalize_cfg(function.blocks)
                function.blocks = prune_unreachable_blocks(function.blocks)
                changed = True
                break


def restore_missing_exception_cleanup_blocks(functions: list[Function]) -> None:
    for function in functions:
        if not function.blocks:
            continue

        stack_size: int | None = None
        for stmt in function.blocks[0].body:
            if isinstance(stmt, StackAllocStmt):
                stack_size = stmt.size
                break

        known_labels = {block.label for block in function.blocks}
        missing_targets: list[str] = []
        for block in function.blocks:
            for stmt in block.body:
                if not isinstance(stmt, RegisterExceptionStmt):
                    continue
                if stmt.target in known_labels or stmt.target in missing_targets:
                    continue
                missing_targets.append(stmt.target)

        if not missing_targets:
            continue

        next_block_id = max((block.id for block in function.blocks), default=-1) + 1
        for target in missing_targets:
            cleanup_body: list[Stmt] = [UnregisterExceptionStmt()]
            if stack_size is not None:
                cleanup_body.append(SetBaseStmt(BinaryExpr("-", BasePointer(), IntLiteral(stack_size))))
            function.blocks.append(
                BasicBlock(
                    id=next_block_id,
                    label=target,
                    body=cleanup_body,
                    terminator=ReturnStmt(),
                    successor_labels=[],
                )
            )
            known_labels.add(target)
            next_block_id += 1


def compute_predecessor_labels(function: Function) -> dict[str, set[str]]:
    predecessors = {block.label: set() for block in function.blocks}
    for block in function.blocks:
        for successor in block.successor_labels:
            if successor in predecessors:
                predecessors[successor].add(block.label)
    return predecessors


def compute_dominators(function: Function) -> dict[str, set[str]]:
    if not function.blocks:
        return {}

    labels = {block.label for block in function.blocks}
    entry = function.blocks[0].label
    predecessors = compute_predecessor_labels(function)
    dominators = {label: set(labels) for label in labels}
    dominators[entry] = {entry}

    changed = True
    while changed:
        changed = False
        for block in function.blocks[1:]:
            preds = predecessors[block.label]
            if preds:
                new_dominators = {block.label}.union(set.intersection(*(dominators[pred] for pred in preds)))
            else:
                new_dominators = {block.label}
            if new_dominators != dominators[block.label]:
                dominators[block.label] = new_dominators
                changed = True

    return dominators


def is_simple_alias_expr(expr: Expr) -> bool:
    return isinstance(expr, (IntLiteral, StringLiteral, LocalVar, ParamRef, LabelRef))


def propagate_single_assignment_aliases(functions: list[Function]) -> None:
    for function in functions:
        if not function.blocks:
            continue

        dominators = compute_dominators(function)
        use_counts = Counter[int]()
        definitions: dict[int, tuple[str, int, Expr]] = {}
        assigned_multiple_times: set[int] = set()

        for block in function.blocks:
            for stmt in block.body:
                use_counts.update(local_use_count_in_stmt(stmt))
                if not isinstance(stmt, AssignStmt) or not isinstance(stmt.target, LocalVar):
                    continue
                offset = stmt.target.offset
                if offset in definitions:
                    assigned_multiple_times.add(offset)
                    continue
                if is_simple_alias_expr(stmt.value):
                    body_index = len([item for item in block.body[: block.body.index(stmt)]])
                    definitions[offset] = (block.label, body_index, stmt.value)
            if block.terminator is not None:
                use_counts.update(local_use_count_in_stmt(block.terminator))

        for offset in assigned_multiple_times:
            definitions.pop(offset, None)

        for offset, (definition_label, definition_index, replacement) in definitions.items():
            if use_counts[offset] == 0:
                continue

            for block in function.blocks:
                if definition_label not in dominators.get(block.label, set()):
                    continue

                start_index = definition_index + 1 if block.label == definition_label else 0
                for index in range(start_index, len(block.body)):
                    block.body[index] = replace_local_in_stmt(block.body[index], offset, replacement)
                if block.terminator is not None:
                    block.terminator = replace_local_in_stmt(block.terminator, offset, replacement)

            remaining_uses = Counter[int]()
            for block in function.blocks:
                for stmt in block.body:
                    remaining_uses.update(local_use_count_in_stmt(stmt))
                if block.terminator is not None:
                    remaining_uses.update(local_use_count_in_stmt(block.terminator))

            if remaining_uses[offset] != 0:
                continue

            for block in function.blocks:
                for index, stmt in enumerate(block.body):
                    if isinstance(stmt, AssignStmt) and isinstance(stmt.target, LocalVar) and stmt.target.offset == offset:
                        del block.body[index]
                        break
                else:
                    continue
                break


def inline_single_use_temporaries(functions: list[Function], builtin_signatures: SignatureLibrary) -> None:
    for function in functions:
        changed = True
        while changed:
            changed = False
            function_usage = count_function_local_uses(function)

            for block in function.blocks:
                usage = Counter[int]()
                for stmt in block.body:
                    usage.update(local_use_count_in_stmt(stmt))
                if block.terminator is not None:
                    usage.update(local_use_count_in_stmt(block.terminator))

                for index, stmt in enumerate(block.body):
                    if not isinstance(stmt, AssignStmt) or not isinstance(stmt.target, LocalVar):
                        continue
                    total_uses = usage[stmt.target.offset]
                    if total_uses == 0:
                        continue
                    if function_usage[stmt.target.offset] != total_uses:
                        continue
                    if not is_side_effect_free_expr(stmt.value, builtin_signatures):
                        continue
                    if total_uses > 1 and any(isinstance(node, CallExpr) for node in iter_expr_tree(stmt.value)):
                        continue

                    next_stmt = block.body[index + 1] if index + 1 < len(block.body) else block.terminator
                    if next_stmt is None:
                        continue
                    if local_use_count_in_stmt(next_stmt).get(stmt.target.offset, 0) != total_uses:
                        continue

                    replacement = replace_local_in_stmt(next_stmt, stmt.target.offset, stmt.value)
                    if index + 1 < len(block.body):
                        block.body[index + 1] = replacement
                    else:
                        block.terminator = replacement
                    del block.body[index]
                    changed = True
                    break

                if changed:
                    break


@dataclass(slots=True, frozen=True)
class ReachingValue:
    expr: Expr
    dependencies: frozenset[int]


DATAFLOW_UNARY_OPS = {"+", "-", "!", "~"}
DATAFLOW_BINARY_OPS = {
    "+",
    "-",
    "*",
    "/",
    "%",
    "<<",
    ">>",
    "&",
    "|",
    "^",
    "==",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "&&",
    "||",
}


def expr_tree_size(expr: Expr) -> int:
    return 1 + sum(expr_tree_size(child) for child in iter_expr_children(expr))


def collect_expr_local_dependencies(expr: Expr) -> frozenset[int]:
    offsets: set[int] = set()

    def visit(node: Expr) -> None:
        if isinstance(node, LocalVar):
            offsets.add(node.offset)

    for node in iter_expr_tree(expr):
        visit(node)
    return frozenset(offsets)


def is_dataflow_propagatable_expr(expr: Expr) -> bool:
    if expr_tree_size(expr) > 12:
        return False
    if isinstance(expr, (IntLiteral, StringLiteral, LocalVar, ParamRef, LabelRef, NamedVar, BasePointer)):
        return True
    if isinstance(expr, AddressOf):
        return isinstance(expr.target, (LocalVar, ParamRef, NamedVar))
    if isinstance(expr, UnaryExpr):
        return expr.op in DATAFLOW_UNARY_OPS and is_dataflow_propagatable_expr(expr.value)
    if isinstance(expr, BinaryExpr):
        return (
            expr.op in DATAFLOW_BINARY_OPS
            and is_dataflow_propagatable_expr(expr.left)
            and is_dataflow_propagatable_expr(expr.right)
        )
    return False


def simplify_dataflow_expr(expr: Expr) -> Expr:
    if isinstance(expr, AddressOf):
        return AddressOf(simplify_dataflow_expr(expr.target))
    if isinstance(expr, DerefExpr):
        return DerefExpr(simplify_dataflow_expr(expr.address), expr.size)
    if isinstance(expr, UnaryExpr):
        value = simplify_dataflow_expr(expr.value)
        constant = evaluate_lifter_int_constant(UnaryExpr(expr.op, value))
        if constant is not None:
            return IntLiteral(constant)
        if expr.op == "+":
            return value
        if expr.op == "!" and isinstance(value, UnaryExpr) and value.op == "!":
            return simplify_dataflow_expr(BinaryExpr("!=", value.value, IntLiteral(0)))
        return UnaryExpr(expr.op, value)
    if isinstance(expr, BinaryExpr):
        left = simplify_dataflow_expr(expr.left)
        right = simplify_dataflow_expr(expr.right)
        constant = evaluate_lifter_int_constant(BinaryExpr(expr.op, left, right))
        if constant is not None:
            return IntLiteral(constant)

        if expr.op == "+":
            if is_int_literal_value(left, 0):
                return right
            if is_int_literal_value(right, 0):
                return left
        if expr.op == "-" and is_int_literal_value(right, 0):
            return left
        if expr.op == "*":
            if is_int_literal_value(left, 0) or is_int_literal_value(right, 0):
                return IntLiteral(0)
            if is_int_literal_value(left, 1):
                return right
            if is_int_literal_value(right, 1):
                return left
        if expr.op == "/" and is_int_literal_value(right, 1):
            return left
        if expr.op in {"<<", ">>"} and is_int_literal_value(right, 0):
            return left
        if expr.op == "|":
            if is_int_literal_value(left, 0):
                return right
            if is_int_literal_value(right, 0):
                return left
        if expr.op == "^":
            if is_int_literal_value(left, 0):
                return right
            if is_int_literal_value(right, 0):
                return left
        if expr.op == "&":
            if is_int_literal_value(left, 0) or is_int_literal_value(right, 0):
                return IntLiteral(0)
        if expr.op == "&&":
            if is_int_literal_value(left, 1):
                return simplify_dataflow_expr(BinaryExpr("!=", right, IntLiteral(0)))
            if is_int_literal_value(right, 1):
                return simplify_dataflow_expr(BinaryExpr("!=", left, IntLiteral(0)))
        if expr.op == "||":
            if is_int_literal_value(left, 0):
                return simplify_dataflow_expr(BinaryExpr("!=", right, IntLiteral(0)))
            if is_int_literal_value(right, 0):
                return simplify_dataflow_expr(BinaryExpr("!=", left, IntLiteral(0)))
        if expr.op == "==" and left == right:
            return IntLiteral(1)
        if expr.op == "!=" and left == right:
            return IntLiteral(0)
        return BinaryExpr(expr.op, left, right)
    if isinstance(expr, CallExpr):
        return CallExpr(expr.callee, tuple(simplify_dataflow_expr(arg) for arg in expr.args), expr.builtin_opcode)
    return expr


def expand_expr_with_reaching_values(
    expr: Expr,
    state: dict[int, ReachingValue],
    visiting: frozenset[int] = frozenset(),
) -> Expr:
    if isinstance(expr, LocalVar):
        value = state.get(expr.offset)
        if value is None or expr.offset in visiting:
            return expr
        return expand_expr_with_reaching_values(value.expr, state, visiting | {expr.offset})
    if isinstance(expr, AddressOf):
        return simplify_dataflow_expr(AddressOf(expand_expr_with_reaching_values(expr.target, state, visiting)))
    if isinstance(expr, DerefExpr):
        return simplify_dataflow_expr(DerefExpr(expand_expr_with_reaching_values(expr.address, state, visiting), expr.size))
    if isinstance(expr, UnaryExpr):
        return simplify_dataflow_expr(UnaryExpr(expr.op, expand_expr_with_reaching_values(expr.value, state, visiting)))
    if isinstance(expr, BinaryExpr):
        return simplify_dataflow_expr(
            BinaryExpr(
                expr.op,
                expand_expr_with_reaching_values(expr.left, state, visiting),
                expand_expr_with_reaching_values(expr.right, state, visiting),
            )
        )
    if isinstance(expr, CallExpr):
        return CallExpr(expr.callee, tuple(expand_expr_with_reaching_values(arg, state, visiting) for arg in expr.args), expr.builtin_opcode)
    return expr


def rewrite_stmt_with_reaching_values(stmt, state: dict[int, ReachingValue]) -> Stmt:
    if isinstance(stmt, AssignStmt):
        target = stmt.target
        if not isinstance(target, LocalVar):
            target = expand_expr_with_reaching_values(target, state)
        value = expand_expr_with_reaching_values(stmt.value, state)
        return AssignStmt(target, value)
    if isinstance(stmt, ExprStmt):
        return ExprStmt(expand_expr_with_reaching_values(stmt.value, state))
    if isinstance(stmt, BranchStmt):
        return BranchStmt(expand_expr_with_reaching_values(stmt.condition, state), stmt.target, stmt.jump_if_false)
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return ReturnStmt(expand_expr_with_reaching_values(stmt.value, state))
    if isinstance(stmt, SetBaseStmt):
        return SetBaseStmt(expand_expr_with_reaching_values(stmt.value, state))
    return stmt


def invalidate_reaching_values_for_offset(
    state: dict[int, ReachingValue],
    offset: int,
) -> dict[int, ReachingValue]:
    return {
        candidate_offset: value
        for candidate_offset, value in state.items()
        if candidate_offset != offset and offset not in value.dependencies
    }


def advance_reaching_value_state(stmt, state: dict[int, ReachingValue]) -> dict[int, ReachingValue]:
    if not isinstance(stmt, AssignStmt) or not isinstance(stmt.target, LocalVar):
        return state

    next_state = invalidate_reaching_values_for_offset(state, stmt.target.offset)
    value = simplify_dataflow_expr(stmt.value)
    if not is_dataflow_propagatable_expr(value):
        return next_state

    dependencies = collect_expr_local_dependencies(value)
    if dependencies or stmt.target.offset in dependencies:
        return next_state

    next_state[stmt.target.offset] = ReachingValue(value, dependencies)
    return next_state


def merge_reaching_value_states(states: list[dict[int, ReachingValue]]) -> dict[int, ReachingValue]:
    if not states:
        return {}

    common_offsets = set(states[0])
    for state in states[1:]:
        common_offsets.intersection_update(state)

    merged: dict[int, ReachingValue] = {}
    for offset in common_offsets:
        first = states[0][offset]
        if all(state[offset] == first for state in states[1:]):
            merged[offset] = first
    return merged


def compute_reaching_value_states(function: Function) -> dict[str, dict[int, ReachingValue]]:
    predecessors = compute_predecessor_labels(function)
    in_states = {block.label: {} for block in function.blocks}
    out_states = {block.label: {} for block in function.blocks}
    entry_label = function.blocks[0].label if function.blocks else None

    changed = True
    while changed:
        changed = False
        for block in function.blocks:
            if block.label == entry_label:
                incoming: dict[int, ReachingValue] = {}
            else:
                predecessor_states = [out_states[label] for label in predecessors.get(block.label, set()) if label in out_states]
                incoming = merge_reaching_value_states(predecessor_states)

            state = dict(incoming)
            for stmt in block.body:
                rewritten_stmt = rewrite_stmt_with_reaching_values(stmt, state)
                state = advance_reaching_value_state(rewritten_stmt, state)

            if incoming != in_states[block.label] or state != out_states[block.label]:
                in_states[block.label] = incoming
                out_states[block.label] = state
                changed = True

    return in_states


def propagate_reaching_values(functions: list[Function]) -> None:
    for function in functions:
        if not function.blocks:
            continue

        in_states = compute_reaching_value_states(function)
        for block in function.blocks:
            state = dict(in_states.get(block.label, {}))
            for index, stmt in enumerate(block.body):
                rewritten_stmt = rewrite_stmt_with_reaching_values(stmt, state)
                block.body[index] = rewritten_stmt
                state = advance_reaching_value_state(rewritten_stmt, state)

            if block.terminator is not None:
                block.terminator = rewrite_stmt_with_reaching_values(block.terminator, state)


def fold_constant_branches(functions: list[Function]) -> None:
    for function in functions:
        changed = False
        for block in function.blocks:
            terminator = block.terminator
            if not isinstance(terminator, BranchStmt):
                continue
            condition = simplify_dataflow_expr(terminator.condition)
            condition_value = evaluate_lifter_int_constant(condition)
            if condition_value is None:
                if condition != terminator.condition:
                    block.terminator = BranchStmt(condition, terminator.target, terminator.jump_if_false)
                    changed = True
                continue

            jump_taken = condition_value == 0 if terminator.jump_if_false else condition_value != 0
            block.terminator = JumpStmt(terminator.target) if jump_taken else None
            changed = True

        if changed:
            finalize_cfg(function.blocks)
            function.blocks = prune_unreachable_blocks(function.blocks)


def stmt_local_use_offsets(stmt) -> set[int]:
    offsets: set[int] = set()

    def add_expr(expr: Expr) -> None:
        offsets.update(collect_expr_local_dependencies(expr))

    if isinstance(stmt, AssignStmt):
        if not isinstance(stmt.target, LocalVar):
            add_expr(stmt.target)
        add_expr(stmt.value)
    elif isinstance(stmt, ExprStmt):
        add_expr(stmt.value)
    elif isinstance(stmt, BranchStmt):
        add_expr(stmt.condition)
    elif isinstance(stmt, ReturnStmt) and stmt.value is not None:
        add_expr(stmt.value)
    elif isinstance(stmt, SetBaseStmt):
        add_expr(stmt.value)
    return offsets


def stmt_local_def_offsets(stmt) -> set[int]:
    if isinstance(stmt, AssignStmt) and isinstance(stmt.target, LocalVar):
        return {stmt.target.offset}
    return set()


def compute_live_out_sets(function: Function) -> dict[str, set[int]]:
    block_use: dict[str, set[int]] = {}
    block_def: dict[str, set[int]] = {}
    live_in = {block.label: set() for block in function.blocks}
    live_out = {block.label: set() for block in function.blocks}

    for block in function.blocks:
        uses: set[int] = set()
        defs: set[int] = set()
        statements = [*block.body, block.terminator] if block.terminator is not None else list(block.body)
        for stmt in statements:
            stmt_uses = stmt_local_use_offsets(stmt)
            uses.update(stmt_uses - defs)
            defs.update(stmt_local_def_offsets(stmt))
        block_use[block.label] = uses
        block_def[block.label] = defs

    changed = True
    while changed:
        changed = False
        for block in reversed(function.blocks):
            new_live_out = set().union(*(live_in.get(label, set()) for label in block.successor_labels))
            new_live_in = block_use[block.label] | (new_live_out - block_def[block.label])
            if new_live_out != live_out[block.label] or new_live_in != live_in[block.label]:
                live_out[block.label] = new_live_out
                live_in[block.label] = new_live_in
                changed = True

    return live_out


def eliminate_dead_local_assignments(functions: list[Function], builtin_signatures: SignatureLibrary) -> None:
    for function in functions:
        changed = True
        while changed:
            changed = False
            live_out = compute_live_out_sets(function)

            for block in reversed(function.blocks):
                live = set(live_out.get(block.label, set()))
                if block.terminator is not None:
                    live = stmt_local_use_offsets(block.terminator) | (live - stmt_local_def_offsets(block.terminator))

                for index in range(len(block.body) - 1, -1, -1):
                    stmt = block.body[index]
                    if (
                        isinstance(stmt, AssignStmt)
                        and isinstance(stmt.target, LocalVar)
                        and stmt.target.offset not in live
                        and not isinstance(stmt.value, StringLiteral)
                        and is_side_effect_free_expr(stmt.value, builtin_signatures)
                    ):
                        del block.body[index]
                        changed = True
                        continue

                    live = stmt_local_use_offsets(stmt) | (live - stmt_local_def_offsets(stmt))


def is_int_literal_value(expr: Expr, value: int) -> bool:
    return isinstance(expr, IntLiteral) and expr.value == value


def evaluate_lifter_int_constant(expr: Expr) -> int | None:
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, UnaryExpr):
        value = evaluate_lifter_int_constant(expr.value)
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
    left = evaluate_lifter_int_constant(expr.left)
    right = evaluate_lifter_int_constant(expr.right)
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
    if expr.op == ">":
        return int(left > right)
    if expr.op == "<=":
        return int(left <= right)
    if expr.op == ">=":
        return int(left >= right)
    if expr.op == "&&":
        return int(bool(left) and bool(right))
    if expr.op == "||":
        return int(bool(left) or bool(right))
    return None


BOOLEAN_RESULT_OPS = {"==", "!=", "<", ">", "<=", ">=", "&&", "||"}


def vote_parameter_hint(
    candidate_type: str | None,
    type_votes: Counter[str],
) -> None:
    if candidate_type:
        type_votes[candidate_type] += 1


def promote_pointer_type(type_name: str | None) -> str | None:
    if not type_name:
        return None
    return f"{type_name}*"


def informative_parameter_type(type_name: str | None) -> str | None:
    if not type_name or type_name == "long":
        return None
    return type_name


def collect_param_hints_from_expr(
    expr: Expr,
    candidate_type: str,
    type_votes: list[Counter[str]],
) -> None:
    if isinstance(expr, ParamRef):
        vote_parameter_hint(candidate_type, type_votes[expr.index])
        return
    if isinstance(expr, CallExpr):
        return
    if isinstance(expr, UnaryExpr):
        if expr.op == "!" and candidate_type == "bool":
            collect_param_hints_from_expr(expr.value, candidate_type, type_votes)
            return
        collect_param_hints_from_expr(expr.value, candidate_type, type_votes)
        return
    if isinstance(expr, BinaryExpr):
        if expr.op in BOOLEAN_RESULT_OPS and candidate_type == "bool":
            collect_param_hints_from_expr(expr.left, candidate_type, type_votes)
            collect_param_hints_from_expr(expr.right, candidate_type, type_votes)
            return
        if expr.op not in BOOLEAN_RESULT_OPS:
            collect_param_hints_from_expr(expr.left, candidate_type, type_votes)
            collect_param_hints_from_expr(expr.right, candidate_type, type_votes)
        return
    if isinstance(expr, (AddressOf, DerefExpr)):
        return
    for child in iter_expr_children(expr):
        collect_param_hints_from_expr(child, candidate_type, type_votes)


def collect_internal_callee_hints_from_expr(
    expr: Expr,
    caller_params: list[str],
    caller_param_types: list[str],
    type_votes: Counter[str],
) -> None:
    if isinstance(expr, ParamRef):
        caller_type = caller_param_types[expr.index] if expr.index < len(caller_param_types) else "long"
        vote_parameter_hint(informative_parameter_type(caller_type), type_votes)
        return
    if isinstance(expr, StringLiteral):
        type_votes["char*"] += 1
        return
    if isinstance(expr, AddressOf):
        if isinstance(expr.target, ParamRef):
            caller_type = caller_param_types[expr.target.index] if expr.target.index < len(caller_param_types) else "long"
            vote_parameter_hint(promote_pointer_type(caller_type), type_votes)
            return
        type_votes["long*"] += 1
        return
    if isinstance(expr, UnaryExpr) and expr.op == "!":
        type_votes["bool"] += 1
        return
    if isinstance(expr, BinaryExpr) and expr.op in BOOLEAN_RESULT_OPS:
        type_votes["bool"] += 1
        return


def collect_boolean_condition_hints(expr: Expr, type_votes: list[Counter[str]]) -> None:
    if isinstance(expr, ParamRef):
        type_votes[expr.index]["bool"] += 1
        return
    if isinstance(expr, UnaryExpr) and expr.op == "!":
        collect_boolean_condition_hints(expr.value, type_votes)
        return
    if isinstance(expr, BinaryExpr) and expr.op in {"&&", "||"}:
        collect_boolean_condition_hints(expr.left, type_votes)
        collect_boolean_condition_hints(expr.right, type_votes)


def make_internal_signature(function: Function) -> Signature:
    params = tuple(
        ParamSpec(
            name=function.params[index] if index < len(function.params) else f"arg{index}",
            type_name=function.param_types[index] if index < len(function.param_types) else "long",
        )
        for index in range(function.arg_count)
    )
    return Signature(name=function.name, return_type=function.return_type, params=params, source="script")


def _apply_seed_signature(function: Function, signature: Signature) -> None:
    function.return_type = signature.return_type
    function.params = [spec.name for spec in signature.params[: function.arg_count]]
    function.param_types = [spec.type_name for spec in signature.params[: function.arg_count]]
    while len(function.params) < function.arg_count:
        function.params.append(f"arg{len(function.params)}")
    while len(function.param_types) < function.arg_count:
        function.param_types.append("long")


def choose_parameter_type(votes: Counter[str]) -> str:
    if not votes:
        return "long"
    return max(votes.items(), key=lambda item: (item[1], item[0] != "long"))[0]


def display_callee_name(expr_callee: str, signature: Signature | None) -> str:
    if expr_callee.startswith("f_") or load_builtin_resolver().resolve(expr_callee) is not None:
        return expr_callee
    if signature is not None:
        return signature.name
    return expr_callee


def rewrite_expr_for_output(expr: Expr, params: list[str], builtin_signatures: SignatureLibrary) -> Expr:
    if isinstance(expr, ParamRef):
        name = params[expr.index] if expr.index < len(params) else expr.name
        return ParamRef(expr.index, name)
    if isinstance(expr, AddressOf):
        return AddressOf(rewrite_expr_for_output(expr.target, params, builtin_signatures))
    if isinstance(expr, DerefExpr):
        return DerefExpr(rewrite_expr_for_output(expr.address, params, builtin_signatures), expr.size)
    if isinstance(expr, BinaryExpr):
        return BinaryExpr(
            expr.op,
            rewrite_expr_for_output(expr.left, params, builtin_signatures),
            rewrite_expr_for_output(expr.right, params, builtin_signatures),
        )
    if isinstance(expr, UnaryExpr):
        return UnaryExpr(expr.op, rewrite_expr_for_output(expr.value, params, builtin_signatures))
    if isinstance(expr, CallExpr):
        args = tuple(rewrite_expr_for_output(arg, params, builtin_signatures) for arg in expr.args)
        signature = builtin_signatures.get(expr.callee)
        callee = display_callee_name(expr.callee, signature)
        return CallExpr(callee, args, expr.builtin_opcode)
    return expr


def rewrite_stmt_for_output(stmt, params: list[str], builtin_signatures: SignatureLibrary):
    if isinstance(stmt, AssignStmt):
        return AssignStmt(
            rewrite_expr_for_output(stmt.target, params, builtin_signatures),
            rewrite_expr_for_output(stmt.value, params, builtin_signatures),
        )
    if isinstance(stmt, ExprStmt):
        return ExprStmt(rewrite_expr_for_output(stmt.value, params, builtin_signatures))
    if isinstance(stmt, BranchStmt):
        return BranchStmt(rewrite_expr_for_output(stmt.condition, params, builtin_signatures), stmt.target, stmt.jump_if_false)
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return ReturnStmt(rewrite_expr_for_output(stmt.value, params, builtin_signatures))
    if isinstance(stmt, SetBaseStmt):
        return SetBaseStmt(rewrite_expr_for_output(stmt.value, params, builtin_signatures))
    return stmt


def infer_parameter_metadata(
    functions: list[Function],
    builtin_signatures: SignatureLibrary,
    external_script_signatures: dict[str, Signature] | None = None,
) -> None:
    seeded_signatures = external_script_signatures or {}
    for function in functions:
        seeded_signature = seeded_signatures.get(function.name)
        if seeded_signature is not None:
            _apply_seed_signature(function, seeded_signature)
            continue
        if not function.params:
            function.params = [f"arg{index}" for index in range(function.arg_count)]
        if not function.param_types:
            function.param_types = ["long"] * function.arg_count

    signatures_are_complete = bool(functions) and all(
        (signature := seeded_signatures.get(function.name)) is not None
        and len(signature.params) >= function.arg_count
        for function in functions
    )
    if signatures_are_complete:
        for function in functions:
            for block in function.blocks:
                block.body = [rewrite_stmt_for_output(stmt, function.params, builtin_signatures) for stmt in block.body]
                if block.terminator is not None:
                    block.terminator = rewrite_stmt_for_output(block.terminator, function.params, builtin_signatures)
        return

    for _ in range(4):
        internal_signatures = {function.name: make_internal_signature(function) for function in functions}
        internal_functions = {function.name: function for function in functions}
        known_script_signatures = dict(external_script_signatures or {})
        known_script_signatures.update(internal_signatures)
        changed = False

        all_type_votes = {function.name: [Counter[str]() for _ in range(function.arg_count)] for function in functions}

        for function in functions:
            type_votes = all_type_votes[function.name]

            for block in function.blocks:
                statements = list(block.body)
                if block.terminator is not None:
                    statements.append(block.terminator)
                for stmt in statements:
                    if isinstance(stmt, BranchStmt):
                        collect_boolean_condition_hints(stmt.condition, type_votes)
                    for expr in iter_stmt_exprs(stmt):
                        for node in iter_expr_tree(expr):
                            if not isinstance(node, CallExpr):
                                continue
                            signature = builtin_signatures.get(node.callee) or known_script_signatures.get(node.callee)
                            if signature is None:
                                continue
                            for index, arg in enumerate(node.args[: len(signature.params)]):
                                spec = signature.params[index]
                                collect_param_hints_from_expr(arg, spec.type_name, type_votes)

                            internal_callee = internal_functions.get(node.callee)
                            if internal_callee is None:
                                continue

                            callee_type_votes = all_type_votes[internal_callee.name]
                            for index, arg in enumerate(node.args[: internal_callee.arg_count]):
                                collect_internal_callee_hints_from_expr(
                                    arg,
                                    function.params,
                                    function.param_types,
                                    callee_type_votes[index],
                                )

        for function in functions:
            type_votes = all_type_votes[function.name]
            seeded_signature = seeded_signatures.get(function.name)
            if seeded_signature is not None:
                new_names = [spec.name for spec in seeded_signature.params[: function.arg_count]]
                new_types = [spec.type_name for spec in seeded_signature.params[: function.arg_count]]
                while len(new_names) < function.arg_count:
                    new_names.append(f"arg{len(new_names)}")
                while len(new_types) < function.arg_count:
                    new_types.append(choose_parameter_type(type_votes[len(new_types)]))
            else:
                new_names = list(function.params)
                new_types = [choose_parameter_type(type_votes[index]) for index in range(function.arg_count)]

            if new_names != function.params or new_types != function.param_types:
                function.params = new_names
                function.param_types = new_types
                changed = True

        if not changed:
            break

    for function in functions:
        for block in function.blocks:
            block.body = [rewrite_stmt_for_output(stmt, function.params, builtin_signatures) for stmt in block.body]
            if block.terminator is not None:
                block.terminator = rewrite_stmt_for_output(block.terminator, function.params, builtin_signatures)


def replace_expr(expr: Expr, param_by_offset: dict[int, ParamRef]) -> Expr:
    if isinstance(expr, LocalVar):
        return param_by_offset.get(expr.offset, expr)
    if isinstance(expr, AddressOf):
        return AddressOf(replace_expr(expr.target, param_by_offset))
    if isinstance(expr, DerefExpr):
        return DerefExpr(replace_expr(expr.address, param_by_offset), expr.size)
    if isinstance(expr, BinaryExpr):
        return BinaryExpr(expr.op, replace_expr(expr.left, param_by_offset), replace_expr(expr.right, param_by_offset))
    if isinstance(expr, UnaryExpr):
        return UnaryExpr(expr.op, replace_expr(expr.value, param_by_offset))
    if isinstance(expr, CallExpr):
        return CallExpr(expr.callee, tuple(replace_expr(arg, param_by_offset) for arg in expr.args), expr.builtin_opcode)
    return expr


def rewrite_stmt(stmt, param_by_offset: dict[int, ParamRef]):
    if isinstance(stmt, AssignStmt):
        return AssignStmt(replace_expr(stmt.target, param_by_offset), replace_expr(stmt.value, param_by_offset))
    if isinstance(stmt, ExprStmt):
        return ExprStmt(replace_expr(stmt.value, param_by_offset))
    if isinstance(stmt, BranchStmt):
        return BranchStmt(replace_expr(stmt.condition, param_by_offset), stmt.target, stmt.jump_if_false)
    if isinstance(stmt, ReturnStmt) and stmt.value is not None:
        return ReturnStmt(replace_expr(stmt.value, param_by_offset))
    if isinstance(stmt, SetBaseStmt):
        return SetBaseStmt(replace_expr(stmt.value, param_by_offset))
    return stmt


def apply_parameter_rewrite(function: Function) -> Function:
    if not function.blocks:
        return function

    entry = function.blocks[0]
    param_by_offset: dict[int, ParamRef] = {}
    params: list[str] = [f"arg{index}" for index in range(function.arg_count)]
    filtered_body = []

    for stmt in entry.body:
        if isinstance(stmt, AssignStmt) and isinstance(stmt.target, LocalVar) and isinstance(stmt.value, ArgRef):
            param_by_offset[stmt.target.offset] = ParamRef(stmt.value.index, params[stmt.value.index])
            continue
        filtered_body.append(stmt)

    entry.body = filtered_body
    function.params = params
    function.param_types = ["long"] * function.arg_count

    for block in function.blocks:
        block.body = [rewrite_stmt(stmt, param_by_offset) for stmt in block.body]
        if block.terminator is not None:
            block.terminator = rewrite_stmt(block.terminator, param_by_offset)

    return function


def lift_function(
    function: AssemblyFunction,
    source_type: str,
    script_arities: dict[str, int],
    internal_call_names: dict[str, str],
    script_signatures: dict[str, Signature],
    builtin_signatures: SignatureLibrary,
    ignore_builtin_config: IgnoreBuiltinConfig | None,
    known_signature: Signature | None = None,
) -> Function:
    arg_count = infer_function_arg_count(function)
    if known_signature is not None:
        arg_count = max(arg_count, known_signature.arg_count)
    raw_blocks = split_basic_blocks(function, source_type)
    blocks = [
        lift_block(
            raw_block,
            arg_count,
            source_type,
            script_arities,
            internal_call_names,
            script_signatures,
            builtin_signatures,
            ignore_builtin_config,
        )
        for raw_block in raw_blocks
    ]

    finalize_cfg(blocks)
    blocks = prune_unreachable_blocks(blocks)
    if is_bs5_like_source_type(source_type):
        blocks = trim_leading_entry_blocks(blocks)
    return_type = known_signature.return_type if known_signature is not None else infer_return_type(blocks)
    lifted = apply_parameter_rewrite(Function(name=function.name, return_type=return_type, arg_count=arg_count, blocks=blocks))
    if known_signature is not None:
        _apply_seed_signature(lifted, known_signature)
    return lifted


@builtin_resolver_scope()
def lift_module(
    module: AssemblyModule,
    override_source_type: str | None = None,
    external_script_signatures: dict[str, Signature] | None = None,
    signature_library: SignatureLibrary | None = None,
    ignore_builtin_config: IgnoreBuiltinConfig | None = None,
    optimize: bool = True,
) -> Module:
    source_type = override_source_type or module.source_type
    metadata = ModuleMetadata(
        header_text=module.header_text,
        header_padding=module.header_padding,
        imports=list(module.imports),
        exports=dict(module.exports),
        defines=dict(module.defines),
    )
    script_arities = build_internal_arity_map(module)
    internal_call_names = build_internal_call_name_map(module)
    known_script_signatures = external_script_signatures or {}
    seeded_module_signatures: dict[str, Signature] = {}
    if external_script_signatures:
        for name, signature in external_script_signatures.items():
            script_arities[name] = max(script_arities.get(name, 0), signature.arg_count)
        seeded_module_signatures = {
            function.name: external_script_signatures[function.name]
            for function in module.functions
            if function.name in external_script_signatures
        }
    known_signatures = signature_library or load_builtin_signature_library()
    functions = [
        lift_function(
            function,
            source_type,
            script_arities,
            internal_call_names,
            known_script_signatures,
            known_signatures,
            ignore_builtin_config,
            seeded_module_signatures.get(function.name),
        )
        for function in module.functions
    ]
    infer_parameter_metadata(functions, known_signatures, external_script_signatures)
    lifted = Module(metadata=metadata, source_type=source_type, functions=functions)
    return optimize_module(lifted, known_signatures) if optimize else lifted


@builtin_resolver_scope()
def optimize_module(
    module: Module,
    signature_library: SignatureLibrary | None = None,
) -> Module:
    functions = module.functions
    known_signatures = signature_library or load_builtin_signature_library()

    propagate_single_assignment_aliases(functions)
    propagate_reaching_values(functions)
    fold_constant_branches(functions)
    fold_boolean_flag_locals(functions)
    propagate_reaching_values(functions)
    fold_constant_branches(functions)
    collapse_empty_branch_diamonds(functions)
    bypass_empty_jump_blocks(functions)
    inline_single_use_temporaries(functions, known_signatures)
    eliminate_dead_local_assignments(functions, known_signatures)
    fold_constant_branches(functions)
    collapse_empty_branch_diamonds(functions)
    bypass_empty_jump_blocks(functions)
    restore_missing_exception_cleanup_blocks(functions)
    return module
