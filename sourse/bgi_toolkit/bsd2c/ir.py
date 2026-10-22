from __future__ import annotations

from dataclasses import dataclass, field

from .builtin_functions import load_builtin_resolver


@dataclass(slots=True)
class ModuleMetadata:
    header_text: str | None = None
    header_padding: str | None = None
    imports: list[str] = field(default_factory=list)
    exports: dict[str, int] = field(default_factory=dict)
    defines: dict[str, str] = field(default_factory=dict)


class Expr:
    pass


@dataclass(slots=True)
class IntLiteral(Expr):
    value: int


@dataclass(slots=True)
class StringLiteral(Expr):
    value: str
    source_line: int | None = field(default=None, compare=False, repr=False)
    literal_index: int | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class LocalVar(Expr):
    offset: int


@dataclass(slots=True)
class NamedVar(Expr):
    name: str


@dataclass(slots=True)
class ArgRef(Expr):
    size: int
    index: int


@dataclass(slots=True)
class ParamRef(Expr):
    index: int
    name: str


@dataclass(slots=True)
class LabelRef(Expr):
    name: str


@dataclass(slots=True)
class BasePointer(Expr):
    pass


@dataclass(slots=True)
class AddressOf(Expr):
    target: Expr


@dataclass(slots=True)
class DerefExpr(Expr):
    address: Expr
    size: int


@dataclass(slots=True)
class BinaryExpr(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(slots=True)
class UnaryExpr(Expr):
    op: str
    value: Expr


@dataclass(slots=True)
class CallExpr(Expr):
    callee: str
    args: tuple[Expr, ...]
    builtin_opcode: str | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class UnknownExpr(Expr):
    text: str = "UNKNOWN"


class Stmt:
    pass


@dataclass(slots=True)
class CommentStmt(Stmt):
    text: str


@dataclass(slots=True)
class LabelStmt(Stmt):
    name: str


@dataclass(slots=True)
class StackAllocStmt(Stmt):
    size: int


@dataclass(slots=True)
class SetBaseStmt(Stmt):
    value: Expr


@dataclass(slots=True)
class AssignStmt(Stmt):
    target: Expr
    value: Expr


@dataclass(slots=True)
class ExprStmt(Stmt):
    value: Expr


@dataclass(slots=True)
class JumpStmt(Stmt):
    target: str


@dataclass(slots=True)
class BranchStmt(Stmt):
    condition: Expr
    target: str
    jump_if_false: bool = False


@dataclass(slots=True)
class RegisterExceptionStmt(Stmt):
    target: str


@dataclass(slots=True)
class UnregisterExceptionStmt(Stmt):
    pass


@dataclass(slots=True)
class ReturnStmt(Stmt):
    value: Expr | None = None


@dataclass(slots=True)
class InternalLabelCallStmt(Stmt):
    registered_name: str
    target: str


@dataclass(slots=True)
class RegisteredScriptJumpStmt(Stmt):
    registered_name: str
    script: Expr
    callee: str = field(default_factory=lambda: load_builtin_resolver().display_name("f_0f3"))
    register_callee: str | None = None


@dataclass(slots=True)
class RawStmt(Stmt):
    text: str


@dataclass(slots=True)
class BasicBlock:
    id: int
    label: str
    body: list[Stmt] = field(default_factory=list)
    terminator: Stmt | None = None
    successor_labels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Function:
    name: str
    return_type: str
    arg_count: int
    params: list[str] = field(default_factory=list)
    param_types: list[str] = field(default_factory=list)
    blocks: list[BasicBlock] = field(default_factory=list)


@dataclass(slots=True)
class Module:
    metadata: ModuleMetadata
    source_type: str
    functions: list[Function] = field(default_factory=list)
