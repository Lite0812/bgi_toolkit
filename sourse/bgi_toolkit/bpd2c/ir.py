from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntFlag
from pathlib import Path
from typing import Any
from ..bp_engine import EngineProfile, get_profile


class ExpressionKind(str, Enum):
    UNKNOWN = "unknown"
    LITERAL = "literal"
    LOCAL = "local"
    GLOBAL = "global"
    LABEL = "label"
    ADDRESS = "address"
    DEREFERENCE = "dereference"
    UNARY = "unary"
    BINARY = "binary"
    SELECT = "select"
    CALL = "call"
    TUPLE = "tuple"


class ValueType(str, Enum):
    UNKNOWN = "unknown"
    VOID = "void"
    BOOL = "bool"
    I8 = "i8"
    I16 = "i16"
    I32 = "i32"
    I64 = "i64"
    POINTER = "pointer"
    STRING = "string"
    LABEL = "label"
    TUPLE = "tuple"


def merge_value_types(types: tuple[ValueType, ...] | list[ValueType]) -> ValueType:
    """Join slot types without treating an unresolved value as a conflict."""
    known = {value for value in types if value is not ValueType.UNKNOWN}
    if not known:
        return ValueType.UNKNOWN
    if len(known) == 1:
        return next(iter(known))
    if known <= {ValueType.I8, ValueType.I16, ValueType.I32, ValueType.I64, ValueType.BOOL}:
        if ValueType.I64 in known:
            return ValueType.I64
        if ValueType.I32 in known:
            return ValueType.I32
        if ValueType.I16 in known:
            return ValueType.I16
        return ValueType.I8
    if known <= {ValueType.STRING, ValueType.POINTER}:
        return ValueType.STRING if ValueType.STRING in known else ValueType.POINTER
    if ValueType.POINTER in known and ValueType.STRING in known:
        return ValueType.STRING
    return ValueType.UNKNOWN


class Effect(IntFlag):
    PURE = 0
    READ_MEMORY = 1
    WRITE_MEMORY = 2
    CALL = 4
    CONTROL = 8
    STACK = 16
    UNKNOWN = 32


class StatementKind(str, Enum):
    UNKNOWN = "unknown"
    ASSIGN = "assign"
    STORE = "store"
    EFFECT = "effect"
    CALL = "call"
    BRANCH = "branch"
    JUMP = "jump"
    JUMP_DYNAMIC = "jump_dynamic"
    RETURN = "return"
    STACK = "stack"
    STACK_PRESERVED = "stack_preserved"
    STACK_PHI = "stack_phi"
    STATE = "state"
    TRACE = "trace"
    OPAQUE = "opaque"


@dataclass(slots=True, frozen=True)
class DirectLabelTarget:
    label: str


@dataclass(slots=True, frozen=True)
class RelativeDisplacementTarget:
    displacement: int
    resolved_label: str | None = None


@dataclass(slots=True, frozen=True)
class IndirectTarget:
    expression: str


@dataclass(slots=True, frozen=True)
class InvalidBoundaryTarget:
    expression: str
    reason: str


ControlTarget = (
    DirectLabelTarget
    | RelativeDisplacementTarget
    | IndirectTarget
    | InvalidBoundaryTarget
)


@dataclass(slots=True)
class Expression:
    text: str
    precedence: int = 100
    source_lines: tuple[int, ...] = ()
    operator: str | None = None
    operands: tuple[Expression, ...] = ()
    kind: ExpressionKind = ExpressionKind.UNKNOWN
    value_type: ValueType = ValueType.UNKNOWN
    effects: Effect = Effect.PURE
    value_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind is not ExpressionKind.UNKNOWN:
            return
        text = self.text.strip()
        if self.operator is not None:
            self.kind = ExpressionKind.UNARY if len(self.operands) == 1 else ExpressionKind.BINARY
        elif text.startswith("label_ref("):
            self.kind = ExpressionKind.LABEL
            self.value_type = ValueType.LABEL
        elif text.startswith('"') and text.endswith('"'):
            self.kind = ExpressionKind.LITERAL
            self.value_type = ValueType.STRING
        elif text in {"true", "false"}:
            self.kind = ExpressionKind.LITERAL
            self.value_type = ValueType.BOOL
        elif text.startswith(("local_", "result_L", "phi_", "edge_phi_")):
            self.kind = ExpressionKind.LOCAL
        elif text.startswith("global_"):
            self.kind = ExpressionKind.GLOBAL
            self.effects |= Effect.READ_MEMORY
        elif "(" in text and text.endswith(")"):
            self.kind = ExpressionKind.CALL
            self.effects |= Effect.CALL
        else:
            try:
                int(text, 0)
            except ValueError:
                return
            self.kind = ExpressionKind.LITERAL
            self.value_type = ValueType.I32


@dataclass(slots=True)
class Statement:
    kind: str
    text: str
    source_lines: tuple[int, ...] = ()
    target: str | None = None
    condition: str | None = None
    node_kind: StatementKind = StatementKind.UNKNOWN
    effects: Effect = Effect.PURE
    control_target: ControlTarget | None = None
    operands: tuple[Expression, ...] = ()

    def __post_init__(self) -> None:
        if self.node_kind is StatementKind.UNKNOWN:
            try:
                self.node_kind = StatementKind(self.kind)
            except ValueError:
                if self.kind in {"load", "assign"}:
                    self.node_kind = StatementKind.ASSIGN
        if self.target is not None and self.control_target is None:
            self.control_target = DirectLabelTarget(self.target)
        if self.node_kind in {
            StatementKind.BRANCH,
            StatementKind.JUMP,
            StatementKind.JUMP_DYNAMIC,
            StatementKind.RETURN,
        }:
            self.effects |= Effect.CONTROL
        if self.node_kind in {StatementKind.STORE, StatementKind.OPAQUE}:
            self.effects |= Effect.WRITE_MEMORY
        if self.node_kind in {StatementKind.CALL, StatementKind.EFFECT, StatementKind.OPAQUE}:
            self.effects |= Effect.CALL
        if self.node_kind in {StatementKind.STACK, StatementKind.STACK_PRESERVED, StatementKind.STACK_PHI}:
            self.effects |= Effect.STACK

    def set_target(self, target: str | None) -> None:
        self.target = target
        self.control_target = DirectLabelTarget(target) if target is not None else None


@dataclass(slots=True)
class StackValue:
    value_id: int
    expression: Expression
    value_type: ValueType = ValueType.UNKNOWN
    source_blocks: tuple[str, ...] = ()


@dataclass(slots=True)
class BasicBlock:
    label: str
    source_line: int
    statements: list[Statement] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)
    synthetic: bool = False
    stack_in: tuple[StackValue, ...] = ()
    stack_out: tuple[StackValue, ...] = ()
    stack_phis: tuple[StackValue, ...] = ()
    # Keep the bytecode boundary even when a jump to an epilogue becomes a
    # return during stack folding. Such a return does not start a new function.
    terminator_opcode: str | None = None


@dataclass(slots=True)
class LiftStatistics:
    instruction_count: int = 0
    lifted_count: int = 0
    opaque_count: int = 0
    stack_sync_count: int = 0


@dataclass(slots=True)
class Module:
    source_path: Path
    blocks: list[BasicBlock]
    diagnostics: list[str] = field(default_factory=list)
    stats: LiftStatistics = field(default_factory=LiftStatistics)
    cfg: Any | None = None
    stack_diagnostics: list[str] = field(default_factory=list)
    engine: EngineProfile = field(default_factory=get_profile)

    @property
    def label_count(self) -> int:
        return len(self.blocks) - (1 if self.blocks and self.blocks[0].label == "entry" else 0)
