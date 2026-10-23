from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import cast

from .builtin_functions import builtin_resolver_scope, call_opcode, load_builtin_resolver

from .emitter import (
    ATOM_PRECEDENCE,
    PREFIX_PRECEDENCE,
    BlockBodyRegion,
    BlockRegion,
    DoWhileRegion,
    EmptyRegion,
    GotoRegion,
    IfRegion,
    InfiniteLoopRegion,
    InlineExitRegion,
    PositiveCaseChainRegion,
    RegionNode,
    SelectColorLoopRegion,
    SequenceRegion,
    StructuredEmitter,
    SwitchRegion,
    WhileRegion,
    collect_function_local_array_sizes,
    collect_non_cfg_label_refs,
    evaluate_int_constant,
    match_local_array_index,
    match_select_setup,
    should_wrap_child_for_clarity,
    should_wrap_left_equal_precedence_child,
)
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
    InternalLabelCallStmt,
    IntLiteral,
    JumpStmt,
    LabelRef,
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
    Stmt,
    StringLiteral,
    UnaryExpr,
    UnknownExpr,
    UnregisterExceptionStmt,
)


CALL_NAME_ALIASES = {}
LABEL_SANITIZE_RE = re.compile(r"[^0-9A-Za-z_]+")
THREE_DIGIT_PREFIX_RE = re.compile(r"^\d{3}_")
INTERNAL_JUMP_RE = re.compile(r"^\s*jump\s+([A-Za-z_][\w.#]*)\s*$")
LABEL_DECL_RE = re.compile(r"^label\s+([A-Za-z_][\w.#]*)\s*:\s*$")
LABEL_REF_RE = re.compile(r"^\s*(?:jump|call)\s+([A-Za-z_][\w.#]*)\b")
LABEL_HIERARCHY_SEPARATOR = "_"

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

PYTHON_BINARY_OPS = {
    "&&": "and",
    "||": "or",
}

# Python puts comparisons below bitwise operations and ``not`` below both.
# Reusing C precedence can change the expression or produce ``a & not b``.
PYTHON_BINARY_PRECEDENCE = {
    "||": 1, "&&": 2,
    "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "|": 5, "^": 6, "&": 7, "<<": 8, ">>": 8,
    "+": 9, "-": 9, "*": 10, "/": 10, "%": 10,
}

REGISTERED_LABEL_SPECIAL_NAMES = {"_BS5HExit", "FButtonWait"}
SIGNED_INTEGER_HELPERS = {"bgi_signed8", "bgi_signed16"}


@dataclass(slots=True)
class MenuMatch:
    start_index: int
    end_index: int
    choices: list["MenuChoice"]
    consumed_blocks: int
    after_label: str | None = None
    outline: str | None = None
    prefix_statements: tuple[Stmt, ...] = ()


@dataclass(slots=True)
class MenuChoice:
    text: str
    block_index: int
    block: BasicBlock
    nested_menu: MenuMatch | None = None


@dataclass(slots=True)
class MenuSetupMatch:
    start_stmt_index: int
    next_stmt_index: int
    options: list[str]
    outline: str | None = None


@dataclass(slots=True)
class InlineSelectMenuMatch:
    setup: MenuSetupMatch
    choice_targets: list[str]
    consumed_blocks: int


@dataclass(slots=True)
class RpyProjectScript:
    script_name: str
    script_label: str
    module: Module


@dataclass(slots=True)
class RpyProjectCallSite:
    script: RpyProjectScript
    function: Function
    function_index: int
    block_index: int


@dataclass(slots=True)
class RpyProjectContext:
    scripts: tuple[RpyProjectScript, ...]
    scripts_by_name: dict[str, tuple[RpyProjectScript, ...]]
    call_sites_by_callee: dict[str, tuple[RpyProjectCallSite, ...]] = field(default_factory=dict)
    registered_names_by_script: dict[str, frozenset[str]] = field(default_factory=dict)
    helper_info_cache: dict[str, CrossFileSelectHelperInfo | None] = field(default_factory=dict)
    caller_match_cache: dict[str, list[CrossFileSelectCallerMatch]] = field(default_factory=dict)
    registered_jump_label_cache: dict[tuple[str, str, bool, bool], str | None] = field(default_factory=dict)


@dataclass(slots=True)
class CrossFileSelectHelperInfo:
    function_name: str
    block_index: int
    setup: MenuSetupMatch


@dataclass(slots=True)
class CrossFileSelectCallerMatch:
    caller_script_name: str
    function_name: str
    block_index: int
    call_stmt_index: int
    callee_script_name: str
    callee_script_label: str
    choice_target_indices: list[int]
    choice_target_labels: list[str]
    consumed_blocks: int


@dataclass(slots=True)
class BgiLabelRegistrationBootstrap:
    entry_label: str
    skip_indices: set[int]


@dataclass(slots=True)
class RpyEmitOptions:
    strip_double_zero_prefix: bool = True
    strip_three_digit_prefix: bool = False
    suppress_bgi_label_registration: bool = True
    lowercase_labels: bool = False


def normalize_script_stem(stem: str, options: RpyEmitOptions | None = None) -> str:
    resolved_options = options or RpyEmitOptions()
    if resolved_options.strip_three_digit_prefix:
        stem = THREE_DIGIT_PREFIX_RE.sub("", stem, count=1)
    if resolved_options.strip_double_zero_prefix and stem.startswith("00") and len(stem) > 2:
        return stem[1:]
    return stem


def derive_script_name(name: str) -> str:
    normalized = name.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = normalized.lower()
    if lowered.endswith(".bsd.txt"):
        normalized = normalized[:-8]
    elif lowered.endswith(".bsd"):
        normalized = normalized[:-4]
    return normalized.lower()


def build_rpy_project_context(scripts: list[RpyProjectScript]) -> RpyProjectContext:
    scripts_by_name: dict[str, list[RpyProjectScript]] = {}
    call_sites_by_callee: dict[str, list[RpyProjectCallSite]] = {}
    registered_names_by_script: dict[str, set[str]] = {}
    for script in scripts:
        scripts_by_name.setdefault(script.script_name, []).append(script)
        for function_index, function in enumerate(script.module.functions):
            for block_index, block in enumerate(function.blocks):
                script_call = _find_script_call_stmt(block)
                if script_call is not None:
                    _stmt_index, callee_script_name = script_call
                    call_sites_by_callee.setdefault(callee_script_name, []).append(
                        RpyProjectCallSite(script, function, function_index, block_index)
                    )
                for stmt in _iter_block_statements(block):
                    if not isinstance(stmt, RegisteredScriptJumpStmt) or not isinstance(stmt.script, StringLiteral):
                        continue
                    target_script_name = derive_script_name(stmt.script.value)
                    registered_names_by_script.setdefault(target_script_name, set()).add(stmt.registered_name)
    return RpyProjectContext(
        scripts=tuple(scripts),
        scripts_by_name={name: tuple(group) for name, group in scripts_by_name.items()},
        call_sites_by_callee={name: tuple(group) for name, group in call_sites_by_callee.items()},
        registered_names_by_script={name: frozenset(group) for name, group in registered_names_by_script.items()},
    )


def _iter_block_statements(block: BasicBlock):
    yield from block.body
    if block.terminator is not None:
        yield block.terminator


def _find_script_call_stmt(block: BasicBlock) -> tuple[int, str] | None:
    for stmt_index, stmt in enumerate(block.body):
        if not isinstance(stmt, ExprStmt) or not isinstance(stmt.value, CallExpr):
            continue
        if call_opcode(stmt.value) != "f_0f0":
            continue
        if len(stmt.value.args) != 1 or not isinstance(stmt.value.args[0], StringLiteral):
            continue
        if any(not isinstance(later_stmt, CommentStmt) for later_stmt in block.body[stmt_index + 1 :]):
            return None
        return stmt_index, derive_script_name(stmt.value.args[0].value)
    return None


@builtin_resolver_scope()
def emit_module(
    module: Module,
    script_label: str | None = None,
    script_name: str | None = None,
    project_context: RpyProjectContext | None = None,
    emit_options: RpyEmitOptions | None = None,
) -> str:
    emitter = (
        BscPythonEmitter(script_label, script_name=script_name, project_context=project_context, emit_options=emit_options)
        if module.source_type == "bsc"
        else RpyEmitter(script_label, script_name=script_name, project_context=project_context, emit_options=emit_options)
    )
    return emitter.emit_module(module)


class RpyEmitter:
    def __init__(
        self,
        script_label: str | None = None,
        script_name: str | None = None,
        project_context: RpyProjectContext | None = None,
        emit_options: RpyEmitOptions | None = None,
    ) -> None:
        self.script_label = script_label
        self.script_name = script_name
        self.project_context = project_context
        self.emit_options = emit_options or RpyEmitOptions()
        self.user_function_names: set[str] = set()
        self.user_function_labels: dict[str, str] = {}
        self._active_label_map: dict[str, str] | None = None
        self._active_registered_label_map: dict[str, str] | None = None
        self._active_array_sizes: dict[int, int] = {}
        self._required_helpers: set[str] = set()
        self._helper_info_cache = project_context.helper_info_cache if project_context is not None else {}
        self._caller_match_cache = project_context.caller_match_cache if project_context is not None else {}
        self._registered_jump_label_cache = (
            project_context.registered_jump_label_cache if project_context is not None else {}
        )

    def _lookup_unique_project_script(self, script_name: str) -> RpyProjectScript | None:
        if self.project_context is None:
            return None
        matches = self.project_context.scripts_by_name.get(script_name)
        if not matches or len(matches) != 1:
            return None
        return matches[0]

    def emit_module(self, module: Module) -> str:
        self._required_helpers.clear()
        self.user_function_names = {function.name for function in module.functions}
        self.user_function_labels = {
            function.name: self._function_label(function, index)
            for index, function in enumerate(module.functions)
        }
        if self.project_context is not None:
            for import_name in module.metadata.imports:
                imported_script = self._lookup_unique_project_script(derive_script_name(import_name))
                if imported_script is None:
                    continue
                self.user_function_names.update(
                    function.name
                    for function in imported_script.module.functions
                )
                for index, function in enumerate(imported_script.module.functions):
                    self.user_function_labels.setdefault(
                        function.name,
                        self._project_function_label(imported_script, function, index),
                    )
        chunks: list[str] = []
        for index, function in enumerate(module.functions):
            top_label = self._function_label(function, index)
            chunks.append(self._emit_function(function, top_label, index))
        rendered = "\n\n".join(chunk for chunk in chunks if chunk).rstrip()
        helper_lines = self._python_helper_definition_lines()
        if helper_lines:
            helper_block = "\n".join(["init python:", *self._indent_lines(helper_lines, 1)])
            rendered = f"{helper_block}\n\n{rendered}" if rendered else helper_block
        return rendered + "\n"

    def _python_helper_definition_lines(self) -> list[str]:
        lines: list[str] = []
        for helper, operator in (("bgi_bool_and", "and"), ("bgi_bool_or", "or")):
            if helper in self._required_helpers:
                lines.extend([
                    f"def {helper}(left, right):",
                    f"    return int(left != 0 {operator} right != 0)",
                    "",
                ])
        if "bgi_div" in self._required_helpers:
            lines.extend([
                "def bgi_div(left, right):",
                "    quotient = abs(left) // abs(right)",
                "    return -quotient if (left < 0) != (right < 0) else quotient",
                "",
            ])
        if "bgi_mod" in self._required_helpers:
            lines.extend([
                "def bgi_mod(left, right):",
                "    return left - bgi_div(left, right) * right",
                "",
            ])
        if "bgi_signed8" in self._required_helpers:
            lines.extend(
                [
                    "def bgi_signed8(value):",
                    "    return (value & 127) - (value & 128)",
                ]
            )
        if "bgi_signed16" in self._required_helpers:
            if lines:
                lines.append("")
            lines.extend(
                [
                    "def bgi_signed16(value):",
                    "    return (value & 32767) - (value & 32768)",
                ]
            )
        return lines

    def _emit_function(self, function: Function, top_label: str, function_index: int) -> str:
        if not function.blocks:
            return f"{self._function_declaration(function, top_label)}\n    pass"

        array_sizes = collect_function_local_array_sizes(function)

        bootstrap = (
            self._match_bgi_label_registration_bootstrap(function)
            if self.emit_options.suppress_bgi_label_registration
            else None
        )
        top_label_block = bootstrap.entry_label if bootstrap is not None else None
        skip_indices = bootstrap.skip_indices if bootstrap is not None else set()
        label_map = self._build_label_map(function, top_label, top_label_block=top_label_block)
        registered_label_map = self._build_registered_label_map(function, label_map)
        previous_label_map = self._active_label_map
        previous_registered_label_map = self._active_registered_label_map
        previous_array_sizes = self._active_array_sizes
        self._active_label_map = label_map
        self._active_registered_label_map = registered_label_map
        self._active_array_sizes = array_sizes
        try:
            index_by_label = {block.label: index for index, block in enumerate(function.blocks)}
            forced_starts = sorted(
                set(self._collect_forced_segment_starts(function))
                | self._collect_cross_file_forced_starts(function, function_index)
            )
            structured_emitter = StructuredRpySegmentEmitter(self, function, label_map)
            lines: list[str] = []
            index = 0
            while index < len(function.blocks):
                if index in skip_indices:
                    index += 1
                    continue
                block = function.blocks[index]
                if lines:
                    lines.append("")
                mapped_label = label_map[block.label]
                if mapped_label == top_label:
                    lines.append(self._function_declaration(function, mapped_label))
                    lines.extend(
                        f"    $ local_{offset} = [0] * {size}"
                        for offset, size in sorted(array_sizes.items())
                    )
                else:
                    lines.append(f"label {mapped_label}:")

                menu_match = self._match_menu(function.blocks, index)
                if menu_match is not None:
                    lines.extend(self._indent_lines(self._emit_menu(menu_match, label_map, index_by_label), 1))
                    index += menu_match.consumed_blocks
                    continue

                inline_select_menu = self._match_inline_select_menu(function.blocks, index)
                if inline_select_menu is not None:
                    lines.extend(self._indent_lines(self._emit_inline_select_menu(function.blocks[index], inline_select_menu, label_map), 1))
                    index += inline_select_menu.consumed_blocks
                    continue

                cross_file_helper = self._match_cross_file_helper(function, index)
                if cross_file_helper is not None:
                    lines.extend(self._indent_lines(self._emit_cross_file_helper(function.blocks[index], cross_file_helper, label_map), 1))
                    index += 1
                    continue

                cross_file_caller = self._match_cross_file_caller(function, function_index, index)
                if cross_file_caller is not None:
                    lines.extend(self._indent_lines(self._emit_cross_file_caller(function.blocks[index], cross_file_caller, label_map), 1))
                    index += cross_file_caller.consumed_blocks
                    continue

                standalone_menu = self._match_standalone_menu(function.blocks[index])
                if standalone_menu is not None:
                    lines.extend(self._indent_lines(self._emit_standalone_menu(function.blocks[index], standalone_menu, label_map), 1))
                    index += 1
                    continue

                segment_end = self._next_forced_segment_start(index, forced_starts, len(function.blocks))
                block_lines, next_index = structured_emitter.render_largest_safe_segment(index, segment_end)
                if not block_lines:
                    block_lines = ["pass"]
                lines.extend(self._indent_lines(block_lines, 1))
                index = next_index

            preserved_labels = {top_label}
            preserved_labels.update(self._collect_cross_file_referenced_labels(function))
            return "\n".join(self._remove_unreferenced_internal_labels(lines, preserved_labels))
        finally:
            self._active_label_map = previous_label_map
            self._active_registered_label_map = previous_registered_label_map
            self._active_array_sizes = previous_array_sizes

    def _collect_cross_file_referenced_labels(self, function: Function) -> set[str]:
        if self.script_name is None:
            return set()

        preserved_labels: set[str] = set()
        for matches in self._caller_match_cache.values():
            for match in matches:
                if match.caller_script_name == self.script_name and match.function_name == function.name:
                    preserved_labels.update(self._sanitize_label(label) for label in match.choice_target_labels)
        preserved_labels.update(self._collect_registered_script_jump_references())
        return preserved_labels

    def _collect_registered_script_jump_references(self) -> set[str]:
        if self.script_name is None or self.project_context is None or self._active_registered_label_map is None:
            return set()

        preserved_labels: set[str] = set()
        registered_names = self.project_context.registered_names_by_script.get(derive_script_name(self.script_name), ())
        for registered_name in registered_names:
            mapped_label = self._active_registered_label_map.get(registered_name)
            if mapped_label is not None:
                preserved_labels.add(mapped_label)
        return preserved_labels

    def _iter_statements(self, function: Function):
        for block in function.blocks:
            yield from block.body
            if block.terminator is not None:
                yield block.terminator

    def _match_bgi_label_registration_bootstrap(self, function: Function) -> BgiLabelRegistrationBootstrap | None:
        if len(function.blocks) < 2:
            return None

        index_by_label = {block.label: index for index, block in enumerate(function.blocks)}
        entry = function.blocks[0]
        if entry.body or not isinstance(entry.terminator, JumpStmt):
            return None

        dispatcher_index = index_by_label.get(entry.terminator.target)
        if dispatcher_index is None or dispatcher_index <= 0:
            return None

        current_index = dispatcher_index
        skip_indices = {0}
        registered_targets: dict[str, str] = {}
        seen: set[int] = set()

        while 0 <= current_index < len(function.blocks) and current_index not in seen:
            seen.add(current_index)
            block = function.blocks[current_index]
            terminator = block.terminator
            if block.body or not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
                break

            registered_name = self._registered_label_dispatch_name(terminator.condition)
            if registered_name is None:
                break

            registered_targets[registered_name] = terminator.target
            skip_indices.add(current_index)
            current_index += 1

        # BS5 scripts use ``f_IsRegisteredLabel(0)`` for the default entry.
        # The lifter represents that integer argument as the empty dispatch
        # name.  A few hand-authored/older scripts spell the same entry as
        # ``_BS5HExit``, so keep accepting that alias as well.
        entry_label = registered_targets.get("")
        if entry_label is None:
            entry_label = registered_targets.get("_BS5HExit")
        if entry_label is None or entry_label not in index_by_label:
            return None

        if current_index < len(function.blocks) and self._is_bgi_label_registration_error_block(function.blocks[current_index]):
            skip_indices.add(current_index)

        fbutton_label = registered_targets.get("FButtonWait")
        if fbutton_label is not None:
            helper_indices = self._collect_fbutton_wait_helper_indices(
                function, fbutton_label, index_by_label, ignored_indices=skip_indices
            )
            helper_labels = {function.blocks[index].label for index in helper_indices}
            referenced_names = (
                self.project_context.registered_names_by_script.get(derive_script_name(self.script_name), ())
                if self.project_context is not None and self.script_name is not None
                else ()
            )
            if entry_label not in helper_labels and not any(
                registered_targets.get(name) in helper_labels for name in referenced_names
            ):
                skip_indices.update(helper_indices)

        return BgiLabelRegistrationBootstrap(entry_label=entry_label, skip_indices=skip_indices)

    def _registered_label_dispatch_name(self, expr: Expr) -> str | None:
        if not isinstance(expr, CallExpr):
            return None
        if call_opcode(expr) != "f_0f7" or len(expr.args) != 1:
            return None
        argument = expr.args[0]
        if isinstance(argument, IntLiteral):
            return ""
        if isinstance(argument, StringLiteral):
            return argument.value
        return None

    def _is_bgi_label_registration_error_block(self, block: BasicBlock) -> bool:
        if block.terminator is not None or len(block.body) != 2:
            return False
        callees = [call_opcode(stmt.value) for stmt in block.body if isinstance(stmt, ExprStmt) and isinstance(stmt.value, CallExpr)]
        return callees == ["f_0f9", "f_0f4"]

    def _collect_fbutton_wait_helper_indices(
        self,
        function: Function,
        start_label: str,
        index_by_label: dict[str, int],
        ignored_indices: set[int] | None = None,
    ) -> set[int]:
        start_index = index_by_label.get(start_label)
        if start_index is None or not self._looks_like_fbutton_wait_helper_start(function.blocks[start_index]):
            return set()

        collected: set[int] = set()
        pending = [start_index]
        while pending:
            index = pending.pop()
            if index in collected or index < 0 or index >= len(function.blocks):
                continue
            # Never remove only part of a larger helper/control-flow region.
            if len(collected) >= 8:
                return set()
            block = function.blocks[index]
            collected.add(index)
            if isinstance(block.terminator, ReturnStmt):
                continue
            for successor in block.successor_labels:
                successor_index = index_by_label.get(successor)
                if successor_index is not None:
                    pending.append(successor_index)
                else:
                    return set()
        helper_labels = {function.blocks[index].label for index in collected}
        if helper_labels & collect_non_cfg_label_refs(function):
            return set()
        ignored = ignored_indices or set()
        for index, block in enumerate(function.blocks):
            if index not in collected and index not in ignored:
                if helper_labels.intersection(block.successor_labels):
                    return set()
        return collected

    def _looks_like_fbutton_wait_helper_start(self, block: BasicBlock) -> bool:
        for stmt in block.body:
            if not isinstance(stmt, ExprStmt) or not isinstance(stmt.value, CallExpr):
                continue
            # Newer bs5.h calls WaitForFButton directly; older versions store
            # its result in local work before entering the same helper loop.
            if call_opcode(stmt.value) == "f_139":
                return True
            if call_opcode(stmt.value) != "f_0e0" or len(stmt.value.args) != 2:
                continue
            value = stmt.value.args[1]
            if isinstance(value, CallExpr) and call_opcode(value) == "f_139":
                return True
        return False

    def _remove_unreferenced_internal_labels(self, lines: list[str], preserved_labels: set[str]) -> list[str]:
        referenced_labels = set(preserved_labels)
        for line in lines:
            match = LABEL_REF_RE.match(line)
            if match is not None:
                referenced_labels.add(match.group(1))

        pruned: list[str] = []
        has_label_scope = False
        for line in lines:
            match = LABEL_DECL_RE.match(line)
            if match is not None and match.group(1) not in referenced_labels and has_label_scope:
                if pruned and pruned[-1] == "":
                    pruned.pop()
                continue
            if match is not None:
                # Removing the first label would strand its indented body
                # at file scope when an unfamiliar helper is kept intact.
                has_label_scope = True
            pruned.append(line)
        return pruned

    def _emit_inline_select_menu(
        self,
        block: BasicBlock,
        inline_menu: InlineSelectMenuMatch,
        label_map: dict[str, str],
    ) -> list[str]:
        setup = inline_menu.setup
        lines: list[str] = []
        for stmt in block.body[: setup.start_stmt_index]:
            lines.extend(self._emit_stmt(stmt, label_map))

        lines.append("menu:")
        if setup.outline is not None:
            lines.append(f"    {self._render_string(setup.outline)}")

        common_lines: list[str] = []
        for stmt in block.body[setup.next_stmt_index :]:
            common_lines.extend(self._emit_stmt(stmt, label_map))

        for option_text, target_label in zip(setup.options, inline_menu.choice_targets, strict=False):
            lines.append(f"    {self._render_string(option_text)}:")
            choice_lines = list(common_lines)
            choice_lines.append(f"jump {self._map_internal_label(target_label, label_map)}")
            lines.extend(self._indent_lines(choice_lines, 2))

        return lines

    def _match_inline_select_menu(self, blocks: list[BasicBlock], start_index: int) -> InlineSelectMenuMatch | None:
        if start_index >= len(blocks):
            return None

        setup = self._extract_select_setup_with_suffix(blocks[start_index])
        if setup is None or not setup.options:
            return None

        cursor = start_index
        choice_targets: list[str] = []
        for option_index in range(len(setup.options) - 1):
            if cursor + 2 >= len(blocks):
                return None

            condition_block = blocks[cursor]
            choice_block = blocks[cursor + 1]
            next_condition_block = blocks[cursor + 2]

            if not self._is_select_branch(condition_block.terminator, option_index):
                return None
            assert isinstance(condition_block.terminator, BranchStmt)
            if condition_block.terminator.target != next_condition_block.label:
                return None
            if choice_block.body:
                return None
            if not isinstance(choice_block.terminator, JumpStmt):
                return None

            choice_targets.append(choice_block.terminator.target)
            cursor += 2

        final_choice_match = self._match_terminal_menu_choice(blocks, cursor, len(setup.options) - 1)
        if final_choice_match is None:
            return None
        final_choice_index, final_consumed_blocks = final_choice_match
        final_choice_block = blocks[final_choice_index]
        if final_choice_block.body:
            return None
        if not isinstance(final_choice_block.terminator, JumpStmt):
            return None
        choice_targets.append(final_choice_block.terminator.target)

        consumed_blocks = (cursor + final_consumed_blocks) - start_index
        if consumed_blocks <= 0:
            return None

        return InlineSelectMenuMatch(
            setup=setup,
            choice_targets=choice_targets,
            consumed_blocks=consumed_blocks,
        )

    def _emit_standalone_menu(
        self,
        block: BasicBlock,
        setup: MenuSetupMatch,
        label_map: dict[str, str],
    ) -> list[str]:
        lines: list[str] = []
        for stmt in block.body[: setup.start_stmt_index]:
            lines.extend(self._emit_stmt(stmt, label_map))

        lines.append("menu:")
        if setup.outline is not None:
            lines.append(f"    {self._render_string(setup.outline)}")

        common_lines: list[str] = []
        for stmt in block.body[setup.next_stmt_index :]:
            common_lines.extend(self._emit_stmt(stmt, label_map))
        if block.terminator is not None:
            common_lines.extend(self._emit_stmt(block.terminator, label_map))

        for option_text in setup.options:
            lines.append(f"    {self._render_string(option_text)}:")
            choice_lines = common_lines or ["pass"]
            lines.extend(self._indent_lines(choice_lines, 2))

        return lines

    def _match_standalone_menu(self, block: BasicBlock) -> MenuSetupMatch | None:
        setup = self._extract_select_setup_with_suffix(block)
        if setup is None:
            return None
        if self._block_uses_select_result_after_setup(block, setup.next_stmt_index):
            return None
        return setup

    def _block_uses_select_result_after_setup(self, block: BasicBlock, start_stmt_index: int) -> bool:
        for stmt in block.body[start_stmt_index:]:
            if self._stmt_references_select_result(stmt):
                return True
        if block.terminator is not None and self._stmt_references_select_result(block.terminator):
            return True
        return False

    def _stmt_references_select_result(self, stmt: Stmt) -> bool:
        if isinstance(stmt, AssignStmt):
            return self._expr_references_select_result(stmt.target) or self._expr_references_select_result(stmt.value)
        if isinstance(stmt, ExprStmt):
            return self._expr_references_select_result(stmt.value)
        if isinstance(stmt, BranchStmt):
            return self._expr_references_select_result(stmt.condition)
        if isinstance(stmt, ReturnStmt):
            return stmt.value is not None and self._expr_references_select_result(stmt.value)
        if isinstance(stmt, SetBaseStmt):
            return self._expr_references_select_result(stmt.value)
        return False

    def _expr_references_select_result(self, expr: Expr) -> bool:
        if self._is_load_local_work(expr, 0):
            return True
        if isinstance(expr, AddressOf):
            return self._expr_references_select_result(expr.target)
        if isinstance(expr, DerefExpr):
            return self._expr_references_select_result(expr.address)
        if isinstance(expr, BinaryExpr):
            return self._expr_references_select_result(expr.left) or self._expr_references_select_result(expr.right)
        if isinstance(expr, UnaryExpr):
            return self._expr_references_select_result(expr.value)
        if isinstance(expr, CallExpr):
            return any(self._expr_references_select_result(arg) for arg in expr.args)
        return False

    def _collect_forced_segment_starts(self, function: Function) -> list[int]:
        index_by_label = {block.label: index for index, block in enumerate(function.blocks)}
        starts = {0}
        for label in collect_non_cfg_label_refs(function):
            block_index = index_by_label.get(label)
            if block_index is not None:
                starts.add(block_index)
        for block_index, block in enumerate(function.blocks):
            if isinstance(block.terminator, JumpStmt):
                target_index = index_by_label.get(block.terminator.target)
                if target_index is not None and target_index != block_index + 1:
                    starts.add(target_index)
            if isinstance(block.terminator, BranchStmt):
                target_index = index_by_label.get(block.terminator.target)
                if target_index is not None:
                    starts.add(target_index)

            menu_match = self._match_menu(function.blocks, block_index)
            if menu_match is None:
                inline_menu_match = self._match_inline_select_menu(function.blocks, block_index)
                if inline_menu_match is None:
                    continue
                starts.add(block_index)
                for target_label in inline_menu_match.choice_targets:
                    target_index = index_by_label.get(target_label)
                    if target_index is not None:
                        starts.add(target_index)
                continue
            starts.add(block_index)
            for choice in menu_match.choices:
                choice_block = choice.block
                if not isinstance(choice_block.terminator, JumpStmt):
                    continue
                target_index = index_by_label.get(choice_block.terminator.target)
                if target_index is not None:
                    starts.add(target_index)
        return sorted(starts)

    def _collect_cross_file_forced_starts(self, function: Function, function_index: int) -> set[int]:
        starts: set[int] = set()
        if self.script_name is not None:
            helper_info = self._find_select_helper_info(self.script_name)
            if helper_info is not None and helper_info.function_name == function.name:
                callers = self._find_cross_file_callers(self.script_name)
                if len(callers) == 1:
                    starts.add(helper_info.block_index)
        for block_index in range(len(function.blocks)):
            caller_match = self._match_cross_file_caller(function, function_index, block_index)
            if caller_match is not None:
                starts.update(caller_match.choice_target_indices)
        return starts

    def _match_cross_file_helper(self, function: Function, start_index: int) -> CrossFileSelectCallerMatch | None:
        if self.project_context is None or self.script_name is None:
            return None

        helper_info = self._find_select_helper_info(self.script_name)
        if helper_info is None or helper_info.function_name != function.name or helper_info.block_index != start_index:
            return None

        callers = self._find_cross_file_callers(self.script_name)
        if len(callers) != 1:
            return None
        return callers[0]

    def _emit_cross_file_helper(
        self,
        block: BasicBlock,
        caller_match: CrossFileSelectCallerMatch,
        label_map: dict[str, str],
    ) -> list[str]:
        if self.script_name is None:
            return self._emit_block(block, label_map)

        helper_info = self._find_select_helper_info(self.script_name)
        if helper_info is None:
            return self._emit_block(block, label_map)

        lines: list[str] = []
        for stmt in block.body[: helper_info.setup.start_stmt_index]:
            lines.extend(self._emit_stmt(stmt, label_map))

        lines.append("menu:")
        if helper_info.setup.outline is not None:
            lines.append(f"    {self._render_string(helper_info.setup.outline)}")

        for option_text, target_label in zip(helper_info.setup.options, caller_match.choice_target_labels, strict=False):
            lines.append(f"    {self._render_string(option_text)}:")
            choice_lines: list[str] = []
            for stmt in block.body[helper_info.setup.next_stmt_index :]:
                choice_lines.extend(self._emit_stmt(stmt, label_map))
            choice_lines.append(f"jump {self._sanitize_label(target_label)}")
            lines.extend(self._indent_lines(choice_lines, 2))

        return lines

    def _match_cross_file_caller(
        self,
        function: Function,
        function_index: int,
        start_index: int,
    ) -> CrossFileSelectCallerMatch | None:
        if self.project_context is None or self.script_name is None:
            return None

        block = function.blocks[start_index]
        call_stmt = self._find_script_call_stmt(block)
        if call_stmt is None:
            return None
        _call_stmt_index, callee_script_name = call_stmt

        callers = self._find_cross_file_callers(callee_script_name)
        if len(callers) != 1:
            return None

        caller_match = callers[0]
        if caller_match.caller_script_name != self.script_name:
            return None
        if caller_match.function_name != function.name or caller_match.block_index != start_index:
            return None
        return caller_match

    def _emit_cross_file_caller(
        self,
        block: BasicBlock,
        caller_match: CrossFileSelectCallerMatch,
        label_map: dict[str, str],
    ) -> list[str]:
        lines: list[str] = []
        for stmt in block.body[: caller_match.call_stmt_index]:
            lines.extend(self._emit_stmt(stmt, label_map))
        lines.append(f"jump {self._sanitize_label(caller_match.callee_script_label)}")
        return lines

    def _extract_select_setup_with_suffix(self, block: BasicBlock) -> MenuSetupMatch | None:
        statements = cast(list[object], block.body)
        for stmt_index in range(len(block.body)):
            select_setup = match_select_setup(statements, stmt_index)
            if select_setup is None:
                continue
            next_stmt_index, options, _include_initialize, outline = select_setup
            return MenuSetupMatch(stmt_index, next_stmt_index, options, outline)
        return None

    def _find_select_helper_info(self, script_name: str) -> CrossFileSelectHelperInfo | None:
        if script_name in self._helper_info_cache:
            return self._helper_info_cache[script_name]
        if self.project_context is None:
            self._helper_info_cache[script_name] = None
            return None

        project_script = self._lookup_unique_project_script(script_name)
        if project_script is None:
            self._helper_info_cache[script_name] = None
            return None

        for function in project_script.module.functions:
            for block_index, block in enumerate(function.blocks):
                setup = self._extract_select_setup_with_suffix(block)
                if setup is None or not setup.options:
                    continue
                if block.terminator is not None and not isinstance(block.terminator, ReturnStmt):
                    continue
                helper_info = CrossFileSelectHelperInfo(function.name, block_index, setup)
                self._helper_info_cache[script_name] = helper_info
                return helper_info

        self._helper_info_cache[script_name] = None
        return None

    def _find_cross_file_callers(self, helper_script_name: str) -> list[CrossFileSelectCallerMatch]:
        if helper_script_name in self._caller_match_cache:
            return self._caller_match_cache[helper_script_name]

        helper_info = self._find_select_helper_info(helper_script_name)
        if helper_info is None or self.project_context is None:
            self._caller_match_cache[helper_script_name] = []
            return []

        matches: list[CrossFileSelectCallerMatch] = []
        for call_site in self.project_context.call_sites_by_callee.get(helper_script_name, ()):
            project_script = call_site.script
            if project_script.script_name == helper_script_name:
                continue
            match = self._match_cross_file_caller_candidate(
                project_script,
                call_site.function,
                call_site.function_index,
                call_site.block_index,
                helper_script_name,
                helper_info,
            )
            if match is not None:
                matches.append(match)

        self._caller_match_cache[helper_script_name] = matches
        return matches

    def _match_cross_file_caller_candidate(
        self,
        project_script: RpyProjectScript,
        function: Function,
        function_index: int,
        block_index: int,
        helper_script_name: str,
        helper_info: CrossFileSelectHelperInfo,
    ) -> CrossFileSelectCallerMatch | None:
        block = function.blocks[block_index]
        call_stmt = self._find_script_call_stmt(block)
        if call_stmt is None:
            return None

        call_stmt_index, callee_script_name = call_stmt
        if callee_script_name != helper_script_name:
            return None

        choice_target_indices = self._trace_cross_file_choice_targets(function, block_index, len(helper_info.setup.options))
        if choice_target_indices is None:
            return None

        consumed_blocks = min(choice_target_indices) - block_index
        if consumed_blocks <= 0:
            return None
        for skipped_index in range(block_index + 1, block_index + consumed_blocks):
            if function.blocks[skipped_index].body:
                return None

        label_helper = RpyEmitter(
            project_script.script_label,
            script_name=project_script.script_name,
            emit_options=self.emit_options,
        )
        top_label = label_helper._function_label(function, function_index)
        label_map = label_helper._build_label_map(function, top_label)
        choice_target_labels = [label_map[function.blocks[index].label] for index in choice_target_indices]
        if self.project_context is None:
            return None
        helper_script = self._lookup_unique_project_script(helper_script_name)
        if helper_script is None:
            return None

        return CrossFileSelectCallerMatch(
            caller_script_name=project_script.script_name,
            function_name=function.name,
            block_index=block_index,
            call_stmt_index=call_stmt_index,
            callee_script_name=helper_script_name,
            callee_script_label=helper_script.script_label,
            choice_target_indices=choice_target_indices,
            choice_target_labels=choice_target_labels,
            consumed_blocks=consumed_blocks,
        )

    def _find_script_call_stmt(self, block: BasicBlock) -> tuple[int, str] | None:
        return _find_script_call_stmt(block)

    def _trace_cross_file_choice_targets(
        self,
        function: Function,
        start_index: int,
        option_count: int,
    ) -> list[int] | None:
        index_by_label = {block.label: index for index, block in enumerate(function.blocks)}
        choice_targets: list[int] = []
        selection_index: int | None = None

        for option_index in range(option_count):
            choice_target = self._follow_cross_file_choice_target(
                function,
                start_index,
                option_index,
                index_by_label,
                selection_index,
            )
            if choice_target is None:
                return None
            target_index, selection_index = choice_target
            choice_targets.append(target_index)

        return choice_targets

    def _follow_cross_file_choice_target(
        self,
        function: Function,
        start_index: int,
        option_index: int,
        index_by_label: dict[str, int],
        selection_index: int | None,
    ) -> tuple[int, int] | None:
        current_index = start_index
        seen: set[int] = set()
        tracked_selection_index = selection_index

        while current_index not in seen:
            seen.add(current_index)
            block = function.blocks[current_index]
            if current_index != start_index and (block.body or block.terminator is None or isinstance(block.terminator, ReturnStmt)):
                if tracked_selection_index is None:
                    return None
                return current_index, tracked_selection_index

            terminator = block.terminator
            if isinstance(terminator, JumpStmt):
                next_index = index_by_label.get(terminator.target)
                if next_index is None:
                    return None
                current_index = next_index
                continue

            if isinstance(terminator, BranchStmt):
                branch_target = self._evaluate_cross_file_choice_branch(
                    terminator,
                    option_index,
                    current_index,
                    len(function.blocks),
                    index_by_label,
                    tracked_selection_index,
                )
                if branch_target is None:
                    return None
                current_index, tracked_selection_index = branch_target
                continue

            return None

        return None

    def _evaluate_cross_file_choice_branch(
        self,
        branch: BranchStmt,
        option_index: int,
        current_index: int,
        block_count: int,
        index_by_label: dict[str, int],
        selection_index: int | None,
    ) -> tuple[int, int] | None:
        comparison = self._match_local_work_compare(branch.condition)
        if comparison is None:
            return None

        work_index, expected_value, negate = comparison
        if selection_index is not None and work_index != selection_index:
            return None

        condition_holds = option_index == expected_value
        if negate:
            condition_holds = not condition_holds

        target_index = index_by_label.get(branch.target)
        fallthrough_index = current_index + 1
        if target_index is None or fallthrough_index >= block_count:
            return None

        if branch.jump_if_false:
            next_index = fallthrough_index if condition_holds else target_index
        else:
            next_index = target_index if condition_holds else fallthrough_index
        return next_index, work_index

    def _match_local_work_compare(self, expr: Expr) -> tuple[int, int, bool] | None:
        if not isinstance(expr, BinaryExpr) or expr.op not in {"==", "!="}:
            return None

        left_index = self._match_local_work_load_index(expr.left)
        right_index = self._match_local_work_load_index(expr.right)
        if left_index is not None and isinstance(expr.right, IntLiteral):
            return left_index, expr.right.value, expr.op == "!="
        if right_index is not None and isinstance(expr.left, IntLiteral):
            return right_index, expr.left.value, expr.op == "!="
        return None

    def _match_local_work_load_index(self, expr: Expr) -> int | None:
        if not isinstance(expr, CallExpr):
            return None
        if call_opcode(expr) != "f_0e1":
            return None
        if len(expr.args) != 1 or not isinstance(expr.args[0], IntLiteral):
            return None
        return expr.args[0].value

    def _next_forced_segment_start(self, start_index: int, forced_starts: list[int], block_count: int) -> int:
        for block_index in forced_starts:
            if block_index > start_index:
                return block_index
        return block_count

    def _emit_block(self, block: BasicBlock, label_map: dict[str, str]) -> list[str]:
        lines: list[str] = []
        for stmt in block.body:
            lines.extend(self._emit_stmt(stmt, label_map))
        if block.terminator is not None:
            lines.extend(self._emit_stmt(block.terminator, label_map))
        return lines

    def _emit_stmt(self, stmt: Stmt, label_map: dict[str, str]) -> list[str]:
        if isinstance(stmt, CommentStmt):
            return [f"# {stmt.text}"]
        if isinstance(stmt, AssignStmt):
            return [self._render_assignment_stmt(stmt.target, stmt.value)]
        if isinstance(stmt, ExprStmt):
            return self._emit_expr_stmt(stmt.value, label_map)
        if isinstance(stmt, JumpStmt):
            return [f"jump {self._map_internal_label(stmt.target, label_map)}"]
        if isinstance(stmt, BranchStmt):
            condition = self._render_condition_expr(stmt.condition, negate=stmt.jump_if_false)
            return [f"if {condition}:", f"    jump {self._map_internal_label(stmt.target, label_map)}"]
        if isinstance(stmt, ReturnStmt):
            if stmt.value is None:
                return ["return"]
            return [f"return {self._render_expr(stmt.value)}"]
        if isinstance(stmt, InternalLabelCallStmt):
            return [f"call {self._map_registered_label_call(stmt.registered_name, stmt.target, label_map)}"]
        if isinstance(stmt, RegisteredScriptJumpStmt):
            target_label = self._lookup_registered_script_jump_label(stmt)
            if target_label is not None:
                callee = CALL_NAME_ALIASES.get(stmt.callee, stmt.callee)
                return [self._render_bgi_statement(callee, (StringLiteral(target_label),))]
            return self._render_registered_script_jump_fallback(stmt)
        if isinstance(stmt, RawStmt):
            return [f"# RAW: {stmt.text}"]
        if isinstance(stmt, RegisterExceptionStmt):
            return [f"# register_exception {stmt.target}"]
        if isinstance(stmt, UnregisterExceptionStmt):
            return ["# unregister_exception"]
        if isinstance(stmt, StackAllocStmt | SetBaseStmt):
            return []
        return [f"# unsupported {stmt!r}"]

    def _emit_expr_stmt(self, expr: Expr, label_map: dict[str, str]) -> list[str]:
        if not isinstance(expr, CallExpr):
            return [f"$ {self._render_expr(expr)}"]

        return [self._render_call_stmt(expr)]

    def _emit_menu(
        self,
        menu_match: MenuMatch,
        label_map: dict[str, str],
        index_by_label: dict[str, int],
    ) -> list[str]:
        lines: list[str] = []
        lines.extend(self._emit_statements_with_standalone_select_menus(list(menu_match.prefix_statements), label_map))

        lines.append("menu:")
        if menu_match.outline is not None:
            lines.append(f"    {self._render_string(menu_match.outline)}")
        for choice in menu_match.choices:
            lines.append(f"    {self._render_string(choice.text)}:")
            choice_lines = self._emit_menu_choice_body(choice, menu_match, label_map, index_by_label)
            if not choice_lines:
                choice_lines = ["pass"]
            lines.extend(self._indent_lines(choice_lines, 2))
        return lines

    def _emit_statements_with_standalone_select_menus(
        self,
        statements: list[Stmt],
        label_map: dict[str, str],
    ) -> list[str]:
        lines: list[str] = []
        cursor = 0
        raw_statements = cast(list[object], statements)
        while cursor < len(statements):
            select_setup = match_select_setup(raw_statements, cursor)
            if select_setup is None:
                lines.extend(self._emit_stmt(statements[cursor], label_map))
                cursor += 1
                continue

            next_stmt_index, options, _include_initialize, outline = select_setup
            if not options:
                cursor = next_stmt_index
                continue

            if any(self._stmt_references_select_result(stmt) for stmt in statements[next_stmt_index:]):
                lines.extend(self._emit_stmt(statements[cursor], label_map))
                cursor += 1
                continue

            lines.extend(self._emit_option_only_menu(options, outline))
            cursor = next_stmt_index

        return lines

    def _emit_option_only_menu(self, options: list[str], outline: str | None) -> list[str]:
        lines: list[str] = ["menu:"]
        if outline is not None:
            lines.append(f"    {self._render_string(outline)}")
        for option_text in options:
            lines.append(f"    {self._render_string(option_text)}:")
            lines.append("        pass")
        return lines

    def _emit_menu_choice_body(
        self,
        choice: MenuChoice,
        menu_match: MenuMatch,
        label_map: dict[str, str],
        index_by_label: dict[str, int],
    ) -> list[str]:
        if choice.nested_menu is not None:
            prefix_lines: list[str] = []
            nested_setup = self._extract_select_setup(choice.block)
            if nested_setup is not None and nested_setup.start_stmt_index > 0:
                for stmt in choice.block.body[: nested_setup.start_stmt_index]:
                    prefix_lines.extend(self._emit_stmt(stmt, label_map))
            prefix_lines.extend(self._emit_menu(choice.nested_menu, label_map, index_by_label))
            return prefix_lines

        lines: list[str] = []
        for stmt in choice.block.body:
            lines.extend(self._emit_stmt(stmt, label_map))

        terminator = choice.block.terminator
        if isinstance(terminator, JumpStmt):
            target_index = index_by_label.get(terminator.target)
            if (
                menu_match.after_label is not None
                and target_index is not None
                and menu_match.start_index < target_index < menu_match.end_index
            ):
                lines.append(f"jump {self._map_internal_label(menu_match.after_label, label_map)}")
                return lines

        if terminator is not None:
            lines.extend(self._emit_stmt(terminator, label_map))
        return lines

    def _match_menu(
        self,
        blocks: list[BasicBlock],
        start_index: int,
        active_starts: frozenset[int] | None = None,
    ) -> MenuMatch | None:
        if active_starts is None:
            active_starts = frozenset()
        if start_index in active_starts:
            return None

        next_active_starts = active_starts | {start_index}
        setup_match = self._extract_select_setup(blocks[start_index])
        if setup_match is None:
            return None

        options = setup_match.options
        cursor = start_index
        choices: list[MenuChoice] = []
        for option_index, option_text in enumerate(options[:-1]):
            if cursor + 2 >= len(blocks):
                return None
            condition_block = blocks[cursor]
            body_block = blocks[cursor + 1]
            next_condition_block = blocks[cursor + 2]
            if not self._is_select_branch(condition_block.terminator, option_index):
                return None
            assert isinstance(condition_block.terminator, BranchStmt)
            if condition_block.terminator.target != next_condition_block.label:
                return None
            choices.append(self._build_menu_choice(blocks, cursor + 1, option_text, next_active_starts))
            cursor += 2

        final_choice_match = self._match_terminal_menu_choice(blocks, cursor, len(options) - 1)
        if final_choice_match is None:
            return None
        final_choice_index, final_consumed_blocks = final_choice_match
        choices.append(self._build_menu_choice(blocks, final_choice_index, options[-1], next_active_starts))

        consumed_end = cursor + final_consumed_blocks
        for choice in choices:
            if choice.nested_menu is not None:
                consumed_end = max(consumed_end, choice.nested_menu.end_index)

        final_choice_block = blocks[final_choice_index]
        after_label = final_choice_block.terminator.target if isinstance(final_choice_block.terminator, JumpStmt) else None
        return MenuMatch(
            start_index=start_index,
            end_index=consumed_end,
            choices=choices,
            consumed_blocks=consumed_end - start_index,
            after_label=after_label,
            outline=setup_match.outline,
            prefix_statements=tuple(blocks[start_index].body[: setup_match.start_stmt_index]),
        )

    def _build_menu_choice(
        self,
        blocks: list[BasicBlock],
        block_index: int,
        option_text: str,
        active_starts: frozenset[int],
    ) -> MenuChoice:
        block = blocks[block_index]
        nested_menu = self._match_menu(blocks, block_index, active_starts)
        return MenuChoice(text=option_text, block_index=block_index, block=block, nested_menu=nested_menu)

    def _match_terminal_menu_choice(
        self,
        blocks: list[BasicBlock],
        cursor: int,
        option_index: int,
    ) -> tuple[int, int] | None:
        if cursor >= len(blocks):
            return None

        terminal_block = blocks[cursor]
        if cursor + 1 < len(blocks) and self._is_select_branch(terminal_block.terminator, option_index):
            return cursor + 1, 2
        return cursor, 1

    def _extract_select_setup(self, block: BasicBlock) -> MenuSetupMatch | None:
        statements = cast(list[object], block.body)
        for stmt_index in range(len(block.body)):
            select_setup = match_select_setup(statements, stmt_index)
            if select_setup is None:
                continue
            next_stmt_index, options, _include_initialize, outline = select_setup
            if any(not isinstance(stmt, CommentStmt) for stmt in block.body[next_stmt_index:]):
                continue
            return MenuSetupMatch(stmt_index, next_stmt_index, options, outline)
        return None

    def _render_assignment_stmt(self, target: Expr, value: Expr, python_mode: bool = False) -> str:
        special_assignment = self._render_work_store_assignment(target, value, python_mode=python_mode)
        if special_assignment is not None:
            return special_assignment
        prefix = "" if python_mode else "$ "
        if isinstance(target, DerefExpr):
            local_array_access = match_local_array_index(target.address)
            if local_array_access is None or local_array_access[0] not in self._active_array_sizes:
                return f"{prefix}set_deref({self._render_expr(target.address)}, {target.size}, {self._render_expr(value)})"
        return f"{prefix}{self._render_expr(target)} = {self._render_expr(value)}"

    def _render_work_store_assignment(self, target: Expr, value: Expr, python_mode: bool = False) -> str | None:
        matched = self._match_work_load_target(target)
        if matched is None:
            return None
        store_callee, index_expr = matched
        if not python_mode:
            return self._render_bgi_statement(store_callee, (index_expr, value))
        prefix = "" if python_mode else "$ "
        return f"{prefix}{store_callee}([{self._render_expr(index_expr)}, {self._render_expr(value)}])"

    def _match_work_load_target(self, expr: Expr) -> tuple[str, Expr] | None:
        if not isinstance(expr, CallExpr) or len(expr.args) != 1:
            return None

        store_opcode = {"f_0e1": "f_0e0", "f_0e3": "f_0e2"}.get(call_opcode(expr))
        if store_opcode is not None:
            return load_builtin_resolver().display_name(store_opcode, like=expr.callee), expr.args[0]
        return None

    def _is_select_branch(self, stmt: Stmt | None, option_index: int) -> bool:
        if not isinstance(stmt, BranchStmt) or not stmt.jump_if_false:
            return False
        condition = stmt.condition
        if not isinstance(condition, BinaryExpr) or condition.op != "==":
            return False
        return self._is_load_local_work(condition.left, 0) and self._is_int_literal(condition.right, option_index) or self._is_load_local_work(condition.right, 0) and self._is_int_literal(condition.left, option_index)

    def _is_load_local_work(self, expr: Expr, index: int) -> bool:
        return (
            isinstance(expr, CallExpr)
            and call_opcode(expr) == "f_0e1"
            and len(expr.args) == 1
            and isinstance(expr.args[0], IntLiteral)
            and expr.args[0].value == index
        )

    def _is_int_literal(self, expr: Expr, value: int) -> bool:
        return isinstance(expr, IntLiteral) and expr.value == value

    def _render_call_stmt(self, call: CallExpr) -> str:
        callee = CALL_NAME_ALIASES.get(call.callee, call.callee)
        if (
            call_opcode(call) in {"f_0f0", "f_0f3"}
            and len(call.args) == 1
            and isinstance(call.args[0], StringLiteral)
            and self.emit_options.lowercase_labels
        ):
            target_label = self._lookup_external_script_label(call.args[0].value)
            if target_label is None:
                target_label = derive_script_label(call.args[0].value, self.emit_options)
            return self._render_bgi_statement(callee, (StringLiteral(target_label),))
        return self._render_bgi_statement(callee, call.args)

    def _render_bgi_statement(self, callee: str, args: tuple[Expr, ...]) -> str:
        opcode = self._sanitize_callable_name(callee)
        if not args:
            return f"bgi {opcode}"
        rendered_args = [self._render_expr(arg) for arg in args]
        return f"bgi {opcode} [{', '.join(rendered_args)}]"

    def _lookup_external_script_label(self, script_name: str) -> str | None:
        if self.project_context is None:
            return None
        matches = self.project_context.scripts_by_name.get(derive_script_name(script_name))
        if not matches:
            return None
        labels = {self._sanitize_label(project_script.script_label) for project_script in matches}
        if len(labels) != 1:
            return None
        return next(iter(labels))

    def _lookup_registered_script_jump_label(self, stmt: RegisteredScriptJumpStmt) -> str | None:
        if not isinstance(stmt.script, StringLiteral):
            return None
        script_name = derive_script_name(stmt.script.value)
        cache_key = (
            script_name,
            stmt.registered_name,
            self.emit_options.suppress_bgi_label_registration,
            self.emit_options.lowercase_labels,
        )
        if cache_key in self._registered_jump_label_cache:
            return self._registered_jump_label_cache[cache_key]

        project_script = self._lookup_unique_project_script(script_name)
        if project_script is None:
            self._registered_jump_label_cache[cache_key] = None
            return None

        for index, function in enumerate(project_script.module.functions):
            top_label = self._project_function_label(project_script, function, index)
            bootstrap = (
                self._match_bgi_label_registration_bootstrap(function)
                if self.emit_options.suppress_bgi_label_registration
                else None
            )
            top_label_block = bootstrap.entry_label if bootstrap is not None else None
            label_map = self._build_label_map(function, top_label, top_label_block=top_label_block)
            index_by_label = {block.label: block_index for block_index, block in enumerate(function.blocks)}
            matching_targets = {
                block.terminator.target
                for block_index, block in enumerate(function.blocks)
                if isinstance(block.terminator, BranchStmt)
                and block.terminator.jump_if_false
                and (target_index := index_by_label.get(block.terminator.target)) is not None
                and target_index < block_index
                and self._extract_registered_label_argument(block.terminator.condition) == stmt.registered_name
            }
            if len(matching_targets) == 1:
                mapped_label = label_map.get(next(iter(matching_targets)))
                if mapped_label is not None:
                    self._registered_jump_label_cache[cache_key] = mapped_label
                    return mapped_label
        self._registered_jump_label_cache[cache_key] = None
        return None

    def _project_function_label(self, project_script: RpyProjectScript, function: Function, index: int) -> str:
        if index == 0 and function.name == "main":
            return self._sanitize_label(project_script.script_label)
        return self._namespaced_function_label(project_script.script_label, function.name)

    def _render_registered_script_jump_fallback(self, stmt: RegisteredScriptJumpStmt) -> list[str]:
        register_call = self._render_bgi_statement(
            stmt.register_callee or load_builtin_resolver().display_name("f_0f6"),
            (StringLiteral(stmt.registered_name),),
        )
        callee = CALL_NAME_ALIASES.get(stmt.callee, stmt.callee)
        script_args = (stmt.script,)
        script_call = self._render_bgi_statement(callee, script_args)
        return [register_call, script_call]

    def _render_expr(self, expr: Expr) -> str:
        expr = self._simplify_expr(expr)
        if isinstance(expr, IntLiteral):
            return str(expr.value)
        if isinstance(expr, StringLiteral):
            return self._render_string(expr.value)
        if isinstance(expr, LocalVar):
            if expr.offset in self._active_array_sizes:
                return f"local_{expr.offset}[0]"
            return f"local_{expr.offset}"
        if isinstance(expr, NamedVar):
            return expr.name
        if isinstance(expr, ArgRef):
            return f"arg_{expr.index}"
        if isinstance(expr, ParamRef):
            return expr.name or f"param_{expr.index}"
        if isinstance(expr, LabelRef):
            if self._active_label_map is not None:
                return self._map_internal_label(expr.name, self._active_label_map)
            return self._sanitize_label(expr.name)
        if isinstance(expr, BasePointer):
            return "base"
        if isinstance(expr, AddressOf):
            if isinstance(expr.target, LocalVar) and expr.target.offset in self._active_array_sizes:
                return f"local_{expr.target.offset}"
            return f"addressof({self._render_expr(expr.target)})"
        if isinstance(expr, DerefExpr):
            local_array_access = match_local_array_index(expr.address)
            if local_array_access is not None:
                offset, index_expr = local_array_access
                if offset in self._active_array_sizes and index_expr is not None:
                    return f"local_{offset}[{self._render_expr(index_expr)}]"
            return f"deref({self._render_expr(expr.address)}, {expr.size})"
        if isinstance(expr, BinaryExpr):
            if expr.op in {"&&", "||"}:
                # VM operands are already evaluated on the stack. A helper
                # preserves both evaluations and returns a numeric 0 or 1.
                helper = "bgi_bool_and" if expr.op == "&&" else "bgi_bool_or"
                self._required_helpers.add(helper)
                return f"{helper}({self._render_expr(expr.left)}, {self._render_expr(expr.right)})"
            if expr.op in {"/", "%"}:
                helper = "bgi_div" if expr.op == "/" else "bgi_mod"
                self._required_helpers.update({"bgi_div", helper})
                return f"{helper}({self._render_expr(expr.left)}, {self._render_expr(expr.right)})"
            operator = PYTHON_BINARY_OPS.get(expr.op, expr.op)
            precedence = self._expr_precedence(expr)
            left = self._render_expr_with_parent(expr.left, precedence)
            right = self._render_expr_with_parent(expr.right, precedence, is_right_child=True)
            if should_wrap_left_equal_precedence_child(expr, expr.left):
                left = f"({left})"
            if should_wrap_child_for_clarity(expr, expr.left):
                left = f"({left})"
            if should_wrap_child_for_clarity(expr, expr.right):
                right = f"({right})"
            return self._wrap_rendered_expr(f"{left} {operator} {right}", precedence)
        if isinstance(expr, UnaryExpr):
            if expr.op == "!":
                return self._wrap_rendered_expr(f"not {self._wrap_expr(expr.value)}", self._expr_precedence(expr))
            return self._wrap_rendered_expr(f"{expr.op}{self._wrap_expr(expr.value)}", PREFIX_PRECEDENCE)
        if isinstance(expr, CallExpr):
            callee = self._sanitize_callable_name(expr.callee)
            if expr.callee in SIGNED_INTEGER_HELPERS and len(expr.args) == 1:
                self._required_helpers.add(expr.callee)
                return f"{callee}({self._render_expr(expr.args[0])})"
            if expr.callee in self.user_function_names:
                rendered_args = ", ".join(self._render_expr(arg) for arg in expr.args)
                return f"{callee}({rendered_args})"
            if len(expr.args) == 1:
                return f"{callee}([{self._render_expr(expr.args[0])}])"
            rendered_args = ", ".join(self._render_expr(arg) for arg in expr.args)
            return f"{callee}({rendered_args})"
        if isinstance(expr, UnknownExpr):
            return expr.text
        return repr(expr)

    def _render_condition_expr(self, expr: Expr, negate: bool = False) -> str:
        if negate:
            return self._render_expr(UnaryExpr("!", expr))
        return self._render_expr(expr)

    def _wrap_expr(self, expr: Expr) -> str:
        rendered = self._render_expr(expr)
        if isinstance(expr, BinaryExpr):
            return f"({rendered})"
        if isinstance(expr, UnaryExpr):
            return f"({rendered})"
        return rendered

    def _render_expr_with_parent(self, expr: Expr, parent_precedence: int, is_right_child: bool = False) -> str:
        rendered = self._render_expr(expr)
        precedence = self._expr_precedence(expr)
        if precedence < parent_precedence or (is_right_child and precedence == parent_precedence):
            return f"({rendered})"
        return rendered

    def _wrap_rendered_expr(self, rendered: str, precedence: int, parent_precedence: int = 0, is_right_child: bool = False) -> str:
        if precedence < parent_precedence or (is_right_child and precedence == parent_precedence):
            return f"({rendered})"
        return rendered

    def _expr_precedence(self, expr: Expr) -> int:
        expr = self._simplify_expr(expr)
        if isinstance(expr, BinaryExpr):
            if expr.op in {"/", "%", "&&", "||"}:
                return ATOM_PRECEDENCE
            return PYTHON_BINARY_PRECEDENCE.get(expr.op, ATOM_PRECEDENCE)
        if isinstance(expr, UnaryExpr):
            return 3 if expr.op == "!" else PREFIX_PRECEDENCE
        return ATOM_PRECEDENCE

    def _render_case_value(self, value: int | str) -> str:
        if isinstance(value, str):
            return self._render_string(value)
        return str(value)

    def _is_simple_expr(self, expr: Expr) -> bool:
        expr = self._simplify_expr(expr)
        return isinstance(expr, (IntLiteral, StringLiteral, LocalVar, NamedVar, ArgRef, ParamRef, LabelRef)) or (
            isinstance(expr, CallExpr)
            and call_opcode(expr) == "f_0e1"
            and len(expr.args) == 1
            and isinstance(expr.args[0], IntLiteral)
        )

    def _simplify_expr(self, expr: Expr) -> Expr:
        if isinstance(expr, AddressOf):
            return AddressOf(self._simplify_expr(expr.target))
        if isinstance(expr, DerefExpr):
            return DerefExpr(self._simplify_expr(expr.address), expr.size)
        if isinstance(expr, UnaryExpr):
            value = self._simplify_expr(expr.value)
            if expr.op == "!":
                if isinstance(value, UnaryExpr) and value.op == "!":
                    return self._simplify_expr(BinaryExpr("!=", value.value, IntLiteral(0)))
                if isinstance(value, BinaryExpr) and value.op in NEGATED_COMPARE_OPS:
                    return self._simplify_expr(BinaryExpr(NEGATED_COMPARE_OPS[value.op], value.left, value.right))
            constant = evaluate_int_constant(UnaryExpr(expr.op, value))
            if constant is not None:
                return IntLiteral(constant)
            return UnaryExpr(expr.op, value)
        if isinstance(expr, BinaryExpr):
            left = self._simplify_expr(expr.left)
            right = self._simplify_expr(expr.right)

            constant = evaluate_int_constant(BinaryExpr(expr.op, left, right))
            if constant is not None:
                return IntLiteral(constant)

            sign_extension = self._simplify_sign_extension(BinaryExpr(expr.op, left, right))
            if sign_extension is not None:
                return sign_extension

            sign_bit_test = self._simplify_sign_bit_test(left, right)
            if sign_bit_test is not None:
                return sign_bit_test
            sign_bit_test = self._simplify_sign_bit_test(right, left)
            if sign_bit_test is not None:
                return sign_bit_test

            if expr.op in SWAPPED_COMPARE_OPS:
                left_constant = evaluate_int_constant(left)
                right_constant = evaluate_int_constant(right)
                if left_constant is not None and right_constant is None:
                    return self._simplify_expr(BinaryExpr(SWAPPED_COMPARE_OPS[expr.op], right, left))

            if expr.op == "+":
                if evaluate_int_constant(left) == 0:
                    return right
                if evaluate_int_constant(right) == 0:
                    return left
            if expr.op == "-" and evaluate_int_constant(right) == 0:
                return left
            if expr.op == "*":
                if evaluate_int_constant(left) == 0 or evaluate_int_constant(right) == 0:
                    return IntLiteral(0)
                if evaluate_int_constant(left) == 1:
                    return right
                if evaluate_int_constant(right) == 1:
                    return left
            if expr.op == "/" and evaluate_int_constant(right) == 1:
                return left
            if expr.op in {"<<", ">>"} and evaluate_int_constant(right) == 0:
                return left
            if expr.op == "&&":
                if evaluate_int_constant(left) == 1:
                    return self._simplify_expr(BinaryExpr("!=", right, IntLiteral(0)))
                if evaluate_int_constant(right) == 1:
                    return self._simplify_expr(BinaryExpr("!=", left, IntLiteral(0)))
            if expr.op == "||":
                if evaluate_int_constant(left) == 0:
                    return self._simplify_expr(BinaryExpr("!=", right, IntLiteral(0)))
                if evaluate_int_constant(right) == 0:
                    return self._simplify_expr(BinaryExpr("!=", left, IntLiteral(0)))
            return BinaryExpr(expr.op, left, right)
        if isinstance(expr, CallExpr):
            return CallExpr(expr.callee, tuple(self._simplify_expr(arg) for arg in expr.args), expr.builtin_opcode)
        return expr

    def _simplify_sign_extension(self, expr: BinaryExpr) -> Expr | None:
        if expr.op != ">>":
            return None
        right_shift = evaluate_int_constant(expr.right)
        if right_shift is None or not isinstance(expr.left, BinaryExpr) or expr.left.op != "<<":
            return None
        left_shift = evaluate_int_constant(expr.left.right)
        if left_shift is None:
            return None

        value = expr.left.left
        if left_shift == 16 and right_shift == 16:
            return CallExpr("bgi_signed16", (value,))
        if left_shift == 24 and right_shift == 24:
            return CallExpr("bgi_signed8", (value,))
        if left_shift == 16 and right_shift == 24:
            shifted_value = self._simplify_expr(BinaryExpr(">>", value, IntLiteral(8)))
            return CallExpr("bgi_signed8", (shifted_value,))
        return None

    def _simplify_sign_bit_test(self, left: Expr, right: Expr) -> Expr | None:
        constant = evaluate_int_constant(right)
        if constant != 1:
            return None
        if not isinstance(left, BinaryExpr) or left.op != ">>":
            return None
        shift = evaluate_int_constant(left.right)
        if shift != 31:
            return None
        return self._simplify_expr(BinaryExpr("<", left.left, IntLiteral(0)))

    def _function_label(self, function: Function, index: int) -> str:
        if index == 0 and function.name == "main":
            return self._sanitize_label(self.script_label or function.name.upper())
        if self.script_label is None:
            return self._sanitize_label(function.name)
        return self._namespaced_function_label(self.script_label, function.name)

    def _namespaced_function_label(self, script_label: str, function_name: str) -> str:
        return self._sanitize_label(f"{script_label}__{function_name}")

    def _function_declaration(self, function: Function, label: str) -> str:
        params = (
            function.params
            if len(function.params) == function.arg_count
            else [f"arg{index}" for index in range(function.arg_count)]
        )
        rendered_params = f"({', '.join(params)})" if params else ""
        return f"label {label}{rendered_params}:"

    def _build_label_map(self, function: Function, top_label: str, top_label_block: str | None = None) -> dict[str, str]:
        if not function.blocks:
            return {}

        preferred_names = self._collect_registered_label_aliases(function)
        top_block = top_label_block or function.blocks[0].label
        used_labels = {top_label}
        label_map: dict[str, str] = {}
        for block in function.blocks:
            if block.label == top_block:
                label_map[block.label] = top_label
                continue

            preferred_name = preferred_names.get(block.label)
            suffix_name = preferred_name or self._sanitize_label(block.label)
            if top_label.endswith("_") or suffix_name.startswith("_"):
                base_name = f"{top_label}{suffix_name}"
            else:
                base_name = f"{top_label}{LABEL_HIERARCHY_SEPARATOR}{suffix_name}"
            candidate = base_name
            suffix = 2
            while candidate in used_labels:
                candidate = f"{base_name}_{suffix}"
                suffix += 1
            label_map[block.label] = candidate
            used_labels.add(candidate)
        return label_map

    def _build_registered_label_map(self, function: Function, label_map: dict[str, str]) -> dict[str, str]:
        registered_label_map: dict[str, str] = {}
        blocked_names: set[str] = set()
        indices = {block.label: index for index, block in enumerate(function.blocks)}
        for index, block in enumerate(function.blocks):
            branch = block.terminator
            if not isinstance(branch, BranchStmt) or not branch.jump_if_false:
                continue
            target_index = indices.get(branch.target)
            if target_index is None or target_index >= index:
                continue
            name = self._extract_registered_label_argument(branch.condition)
            mapped_label = label_map.get(branch.target)
            if name is None or mapped_label is None or name in blocked_names:
                continue
            previous = registered_label_map.get(name)
            if previous is not None and previous != mapped_label:
                registered_label_map.pop(name, None)
                blocked_names.add(name)
            else:
                # Keep the original registration as identity: Japanese names,
                # punctuation and case can collide after label sanitization.
                registered_label_map[name] = mapped_label
        return registered_label_map

    def _collect_registered_label_aliases(self, function: Function) -> dict[str, str]:
        aliases: dict[str, str] = {}
        blocked_targets: set[str] = set()
        index_by_label = {block.label: index for index, block in enumerate(function.blocks)}

        for block_index, block in enumerate(function.blocks):
            terminator = block.terminator
            if not isinstance(terminator, BranchStmt) or not terminator.jump_if_false:
                continue

            target_index = index_by_label.get(terminator.target)
            if target_index is None or target_index >= block_index:
                continue

            registered_label = self._extract_registered_label_name(terminator.condition)
            if registered_label is None:
                continue

            alias = self._registered_label_alias(registered_label)
            if terminator.target in blocked_targets:
                continue
            existing_alias = aliases.get(terminator.target)
            if existing_alias is None:
                aliases[terminator.target] = alias
                continue
            if existing_alias != alias:
                aliases.pop(terminator.target, None)
                blocked_targets.add(terminator.target)

        return aliases

    def _extract_registered_label_name(self, expr: Expr) -> str | None:
        registered_name = self._extract_registered_label_argument(expr)
        if registered_name is None:
            return None
        return registered_name

    def _extract_registered_label_argument(self, expr: Expr) -> str | None:
        if not isinstance(expr, CallExpr):
            return None
        if call_opcode(expr) != "f_0f7":
            return None
        if len(expr.args) != 1:
            return None
        argument = expr.args[0]
        if not isinstance(argument, StringLiteral):
            return None
        return argument.value or None

    def _registered_label_alias(self, registered_name: str) -> str:
        if registered_name in REGISTERED_LABEL_SPECIAL_NAMES:
            return registered_name.lower() if self.emit_options.lowercase_labels else registered_name
        return self._sanitize_label(registered_name)

    def _map_registered_label_call(self, registered_name: str, target: str, label_map: dict[str, str]) -> str:
        if self._active_registered_label_map is not None:
            mapped = self._active_registered_label_map.get(registered_name)
            if mapped is not None:
                return mapped
        return self._map_internal_label(target, label_map)

    def _map_internal_label(self, label: str, label_map: dict[str, str]) -> str:
        return label_map.get(label, self._sanitize_label(label))

    def _sanitize_label(self, label: str) -> str:
        sanitized = LABEL_SANITIZE_RE.sub("_", label).strip("_")
        if not sanitized:
            sanitized = "label"
        if sanitized[0].isdigit():
            sanitized = f"L_{sanitized}"
        if self.emit_options.lowercase_labels:
            sanitized = sanitized.lower()
        return sanitized

    def _sanitize_callable_name(self, name: str) -> str:
        sanitized = LABEL_SANITIZE_RE.sub("_", name)
        if not sanitized:
            sanitized = "callable"
        if sanitized[0].isdigit():
            sanitized = f"f_{sanitized}"
        return sanitized

    def _render_string(self, value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _indent_lines(self, lines: list[str], levels: int) -> list[str]:
        prefix = "    " * levels
        return [f"{prefix}{line}" if line else "" for line in lines]


def derive_script_label(name: str, emit_options: RpyEmitOptions | None = None) -> str:
    lowered = name.lower()
    if lowered.endswith(".bsd.txt"):
        name = name[:-8]
    elif lowered.endswith(".bsd"):
        name = name[:-4]
    name = name.replace("\\", "_").replace("/", "_")
    name = normalize_script_stem(name, emit_options)
    sanitized = LABEL_SANITIZE_RE.sub("_", name).strip("_")
    if not sanitized:
        sanitized = "script"
    sanitized = sanitized.lower()
    if sanitized[0].isdigit():
        sanitized = f"l_{sanitized}"
    return sanitized


class StructuredRpySegmentEmitter(StructuredEmitter):
    def __init__(self, rpy_emitter: RpyEmitter, function: Function, label_map: dict[str, str]) -> None:
        super().__init__(function)
        self.rpy_emitter = rpy_emitter
        self.label_map = label_map
        self.reverse_label_map = {mapped: raw for raw, mapped in label_map.items()}
        self.switch_temp_counter = 0

    def render_largest_safe_segment(self, start: int, end: int) -> tuple[list[str], int]:
        current_end = end
        while current_end > start:
            lines = self.render_segment(start, current_end)
            internal_targets = self.collect_internal_jump_targets(lines, start, current_end)
            if not internal_targets:
                return lines, current_end
            next_end = min(internal_targets)
            if next_end <= start:
                break
            current_end = next_end

        fallback_end = min(start + 1, len(self.blocks))
        return self.render_segment(start, fallback_end), fallback_end

    def render_segment(self, start: int, end: int) -> list[str]:
        root = self.build_region_tree(start, end, suppress_first_label=True)
        return self.render_region_node(root, "")

    def collect_internal_jump_targets(self, lines: list[str], start: int, end: int) -> set[int]:
        targets: set[int] = set()
        for line in lines:
            match = INTERNAL_JUMP_RE.match(line)
            if match is None:
                continue
            raw_label = self.reverse_label_map.get(match.group(1))
            if raw_label is None:
                continue
            index = self.index_by_label.get(raw_label)
            if index is not None and start < index < end:
                targets.add(index)
        return targets

    def should_render_label(self, label: str, show_label: bool) -> bool:
        return False

    def render_region_node(self, region, indent: str) -> list[str]:
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
                show_label=False,
                suppress_terminal_jump_to=region.suppress_terminal_jump_to,
                loop_header_label=region.loop_header_label,
                loop_exit_label=region.loop_exit_label,
                switch_exit_label=region.switch_exit_label,
            )
        if isinstance(region, BlockBodyRegion):
            return self.render_block_body(self.blocks[region.index], indent, show_label=False)
        if isinstance(region, GotoRegion):
            return [f"{indent}jump {self._map_internal_label(region.target)}"]
        if isinstance(region, InlineExitRegion):
            return self.render_inline_exit_block(region.block_index, indent)
        if isinstance(region, IfRegion):
            return self.render_if_region(region, indent)
        if isinstance(region, DoWhileRegion):
            header_index = region.header_index
            header_label = self.blocks[header_index].label if header_index is not None else None
            lines = self.render_region_node(region.body_region, indent)
            if not lines:
                lines = [f"{indent}pass"]
            if header_label is not None:
                lines.append(f"{indent}if {self.rpy_emitter._render_expr(region.condition)}:")
                lines.append(f"{indent}    jump {self._map_internal_label(header_label)}")
            return lines
        if isinstance(region, WhileRegion):
            lines = [f"{indent}while {self.rpy_emitter._render_expr(region.condition)}:"]
            body_lines = self.render_region_node(region.body_region, indent + "    ")
            if not body_lines:
                body_lines = [f"{indent}    pass"]
            lines.extend(body_lines)
            return lines
        if isinstance(region, InfiniteLoopRegion):
            lines = [f"{indent}while True:"]
            body_lines = self.render_region_node(region.body_region, indent + "    ")
            if not body_lines:
                body_lines = [f"{indent}    pass"]
            lines.extend(body_lines)
            return lines
        if isinstance(region, SwitchRegion):
            return self.render_switch_region(region, indent)
        if isinstance(region, SelectColorLoopRegion):
            return self.render_select_color_loop(region.header_index, region.cases, indent, show_label=False)
        if isinstance(region, PositiveCaseChainRegion):
            return self.render_positive_case_chain(
                region.header_index,
                region.cases,
                indent,
                show_label=False,
                suppress_terminal_jump_to=region.suppress_terminal_jump_to,
            )
        return [f"{indent}# unsupported region {region!r}"]

    def render_if_region(self, region: IfRegion, indent: str) -> list[str]:
        lines: list[str] = []
        current = region
        keyword = "if"

        while True:
            lines.append(f"{indent}{keyword} {self.rpy_emitter._render_expr(current.condition)}:")
            then_lines = self.render_region_node(current.then_region, indent + "    ")
            if not then_lines:
                then_lines = [f"{indent}    pass"]
            lines.extend(then_lines)

            if current.else_region is None:
                return lines

            normalized_else = self.normalize_statement_region(current.else_region)
            if isinstance(normalized_else, IfRegion):
                current = normalized_else
                keyword = "elif"
                continue

            lines.append(f"{indent}else:")
            else_lines = self.render_region_node(normalized_else, indent + "    ")
            if not else_lines:
                else_lines = [f"{indent}    pass"]
            lines.extend(else_lines)
            return lines

    def render_switch_region(self, region: SwitchRegion, indent: str) -> list[str]:
        subject_expr = self.rpy_emitter._simplify_expr(region.subject)
        subject_rendered = self.rpy_emitter._render_expr(subject_expr)
        lines: list[str] = []
        subject_name = subject_rendered
        if len(region.cases) > 1 and not self.rpy_emitter._is_simple_expr(subject_expr):
            self.switch_temp_counter += 1
            subject_name = f"switch_value_{region.header_index}_{self.switch_temp_counter}"
            lines.append(f"{indent}$ {subject_name} = {subject_rendered}")

        for case_index, case in enumerate(region.cases):
            keyword = "if" if case_index == 0 else "elif"
            lines.append(f"{indent}{keyword} {subject_name} == {self.rpy_emitter._render_case_value(case.value)}:")
            case_lines = self.render_region_node(case.body, indent + "    ")
            if not case_lines:
                case_lines = [f"{indent}    pass"]
            lines.extend(case_lines)

        if region.default_region is not None:
            lines.append(f"{indent}else:")
            default_lines = self.render_region_node(region.default_region, indent + "    ")
            if not default_lines:
                default_lines = [f"{indent}    pass"]
            lines.extend(default_lines)
        return lines

    def render_inline_exit_block(self, index: int, indent: str) -> list[str]:
        block = self.blocks[index]
        lines: list[str] = []
        for stmt in block.body:
            lines.extend(self._indent_stmt_lines(stmt, indent))
        if block.terminator is not None:
            lines.extend(self._indent_stmt_lines(block.terminator, indent))
        return lines

    def render_select_color_loop(
        self,
        index: int,
        cases: list[tuple[Expr, int]],
        indent: str,
        show_label: bool,
    ) -> list[str]:
        del show_label
        block = self.blocks[index]
        lines = self.render_block_body(block, indent, show_label=False)

        for condition, preview_index in cases:
            lines.append(f"{indent}if {self.rpy_emitter._render_expr(condition)}:")
            preview_lines = self.render_block_fallback(
                preview_index,
                indent + "    ",
                show_label=False,
                suppress_terminal_jump_to=block.label,
            )
            if not preview_lines:
                preview_lines = [f"{indent}    pass"]
            lines.extend(preview_lines)
            lines.append(f"{indent}    jump {self._map_internal_label(block.label)}")

        return lines

    def render_positive_case_chain(
        self,
        index: int,
        cases: list[tuple[Expr, int]],
        indent: str,
        show_label: bool,
        suppress_terminal_jump_to: str | None = None,
    ) -> list[str]:
        del show_label
        lines = self.render_block_body(self.blocks[index], indent, show_label=False)
        for case_index, (condition, target_index) in enumerate(cases):
            keyword = "if" if case_index == 0 else "elif"
            lines.append(f"{indent}{keyword} {self.rpy_emitter._render_expr(condition)}:")
            branch_lines = self.render_block_fallback(
                target_index,
                indent + "    ",
                show_label=False,
                suppress_terminal_jump_to=suppress_terminal_jump_to,
            )
            if not branch_lines:
                branch_lines = [f"{indent}    pass"]
            lines.extend(branch_lines)
        return lines

    def render_block_body(self, block: BasicBlock, indent: str, show_label: bool = True) -> list[str]:
        del show_label
        emitted_lines = self.rpy_emitter._emit_statements_with_standalone_select_menus(list(block.body), self.label_map)
        return [f"{indent}{line}" if line else "" for line in emitted_lines]

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
        del show_label
        block = self.blocks[index]
        lines = self.render_block_body(block, indent, show_label=False)
        terminator = block.terminator
        if isinstance(terminator, JumpStmt):
            if suppress_terminal_jump_to is not None and terminator.target == suppress_terminal_jump_to:
                return lines
            if loop_header_label is not None and terminator.target == loop_header_label:
                lines.append(f"{indent}jump {self._map_internal_label(loop_header_label)}")
                return lines
            if loop_exit_label is not None and terminator.target == loop_exit_label:
                lines.append(f"{indent}jump {self._map_internal_label(loop_exit_label)}")
                return lines
            if switch_exit_label is not None and terminator.target == switch_exit_label:
                return lines
            if terminator.target == self.next_block_label(index):
                return lines
        if terminator is not None:
            lines.extend(self._indent_stmt_lines(terminator, indent))
        return lines

    def _indent_stmt_lines(self, stmt: Stmt, indent: str) -> list[str]:
        return [f"{indent}{line}" if line else "" for line in self.rpy_emitter._emit_stmt(stmt, self.label_map)]

    def _map_internal_label(self, label: str) -> str:
        return self.label_map.get(label, self.rpy_emitter._sanitize_label(label))


class BscPythonEmitter(RpyEmitter):
    def emit_module(self, module: Module) -> str:
        self._required_helpers.clear()
        self.user_function_names = {function.name for function in module.functions}
        if self.project_context is not None:
            for import_name in module.metadata.imports:
                imported_script = self._lookup_unique_project_script(derive_script_name(import_name))
                if imported_script is None:
                    continue
                self.user_function_names.update(
                    function.name
                    for function in imported_script.module.functions
                )
        lines: list[str] = []

        if module.metadata.header_text is not None:
            lines.append(f"# BGI_HEADER({self._render_string(module.metadata.header_text)})")
        if module.metadata.header_padding is not None:
            lines.append(f"# BGI_HEADER_PADDING({self._render_string(module.metadata.header_padding)})")
        for import_name in module.metadata.imports:
            lines.append(f"# BGI_IMPORT({self._render_string(import_name)})")
        if lines:
            lines.append("")

        init_index = len(lines)
        lines.append("init python:")
        if not module.functions:
            lines.append("    pass")
            return "\n".join(lines).rstrip() + "\n"

        for index, function in enumerate(module.functions):
            if index:
                lines.append("")
            lines.extend(self._indent_lines(self._emit_python_function(function), 1))
        helper_lines = self._python_helper_definition_lines()
        if helper_lines:
            lines[init_index + 1:init_index + 1] = [
                *self._indent_lines(helper_lines, 1),
                "",
            ]
        return "\n".join(lines).rstrip() + "\n"

    def _emit_python_function(self, function: Function) -> list[str]:
        params = function.params if len(function.params) == function.arg_count else [f"arg{index}" for index in range(function.arg_count)]
        lines = [f"def {function.name}({', '.join(params)}):"]
        array_sizes = collect_function_local_array_sizes(function)
        previous_array_sizes = self._active_array_sizes
        self._active_array_sizes = array_sizes
        try:
            structured_emitter = StructuredPythonFunctionEmitter(self, function)
            body_lines = structured_emitter.render()
            if structured_emitter.requires_dispatch:
                body_lines = PythonDispatchEmitter(self, function).render()
            body_lines = self._prune_unreachable_lines(body_lines)
        finally:
            self._active_array_sizes = previous_array_sizes
        body_lines = [
            *(f"local_{offset} = [0] * {size}" for offset, size in sorted(array_sizes.items())),
            *body_lines,
        ]
        if not body_lines:
            body_lines = ["pass"]
        lines.extend(self._indent_lines(body_lines, 1))
        return lines

    def _prune_unreachable_lines(self, lines: list[str]) -> list[str]:
        pruned: list[str] = []
        dead_block_indents: list[int] = []

        for line in lines:
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)

            while dead_block_indents and indent < dead_block_indents[-1]:
                dead_block_indents.pop()

            if dead_block_indents and indent >= dead_block_indents[-1]:
                continue

            pruned.append(line)
            if self._is_terminal_python_line(stripped):
                dead_block_indents.append(indent)

        return pruned

    def _is_terminal_python_line(self, stripped_line: str) -> bool:
        return (
            stripped_line == "return"
            or stripped_line == "break"
            or stripped_line == "continue"
            or stripped_line == "raise"
            or stripped_line.startswith("return ")
            or stripped_line.startswith("raise ")
        )

    def _emit_python_stmt(self, stmt: Stmt) -> list[str]:
        if isinstance(stmt, CommentStmt):
            return [f"# {stmt.text}"]
        if isinstance(stmt, AssignStmt):
            return [self._render_assignment_stmt(stmt.target, stmt.value, python_mode=True)]
        if isinstance(stmt, ExprStmt):
            return self._emit_python_expr_stmt(stmt.value)
        if isinstance(stmt, ReturnStmt):
            if stmt.value is None:
                return ["return"]
            return [f"return {self._render_expr(stmt.value)}"]
        if isinstance(stmt, InternalLabelCallStmt):
            return [f"# call :{stmt.registered_name} -> {stmt.target}"]
        if isinstance(stmt, RawStmt):
            return [f"# RAW: {stmt.text}"]
        if isinstance(stmt, RegisterExceptionStmt):
            return [f"# register_exception {stmt.target}"]
        if isinstance(stmt, UnregisterExceptionStmt):
            return ["# unregister_exception"]
        if isinstance(stmt, StackAllocStmt | SetBaseStmt):
            return []
        return [f"# unsupported {stmt!r}"]

    def _emit_python_expr_stmt(self, expr: Expr) -> list[str]:
        if not isinstance(expr, CallExpr):
            return [self._render_expr(expr)]
        return [self._render_python_call_stmt(expr)]

    def _render_python_call_stmt(self, call: CallExpr) -> str:
        callee = self._sanitize_callable_name(CALL_NAME_ALIASES.get(call.callee, call.callee))
        rendered_args = ", ".join(self._render_expr(arg) for arg in call.args)
        if callee in self.user_function_names:
            return f"{callee}({rendered_args})"
        if not call.args:
            return f"{callee}()"
        return f"{callee}([{rendered_args}])"


class StructuredPythonFunctionEmitter(StructuredEmitter):
    def __init__(
        self,
        emitter: BscPythonEmitter,
        function: Function,
        tail_render_cache: dict[str, list[str] | None] | None = None,
    ) -> None:
        super().__init__(function)
        self.emitter = emitter
        self.switch_temp_counter = 0
        self.requires_dispatch = False
        self._tail_render_cache = tail_render_cache if tail_render_cache is not None else {}

    def render(self) -> list[str]:
        if not self.blocks:
            return ["pass"]
        root = self.build_region_tree(0, len(self.blocks), suppress_first_label=True)
        lines = self.render_region_node(root, "")
        if not lines:
            return ["pass"]
        return lines

    def render_region_node(self, region, indent: str) -> list[str]:
        if isinstance(region, EmptyRegion):
            return []
        if isinstance(region, SequenceRegion):
            return self.render_sequence_region(region, indent)
        if isinstance(region, BlockRegion):
            return self.render_block_fallback(
                region.index,
                indent,
                show_label=False,
                suppress_terminal_jump_to=region.suppress_terminal_jump_to,
                loop_header_label=region.loop_header_label,
                loop_exit_label=region.loop_exit_label,
                switch_exit_label=region.switch_exit_label,
            )
        if isinstance(region, BlockBodyRegion):
            return self.render_block_body(self.blocks[region.index], indent, show_label=False)
        if isinstance(region, GotoRegion):
            self.requires_dispatch = True
            return [f"{indent}# goto {region.target}"]
        if isinstance(region, InlineExitRegion):
            return self.render_inline_exit_block(region.block_index, indent)
        if isinstance(region, IfRegion):
            return self.render_if_region(region, indent)
        if isinstance(region, DoWhileRegion):
            lines = [f"{indent}while True:"]
            body_lines = self.render_region_node(region.body_region, indent + "    ")
            if not body_lines:
                body_lines = [f"{indent}    pass"]
            lines.extend(body_lines)
            lines.append(f"{indent}    if {self.emitter._render_condition_expr(region.condition, negate=True)}:")
            lines.append(f"{indent}        break")
            return lines
        if isinstance(region, WhileRegion):
            lines = [f"{indent}while {self.emitter._render_expr(region.condition)}:"]
            body_lines = self.render_region_node(region.body_region, indent + "    ")
            if not body_lines:
                body_lines = [f"{indent}    pass"]
            lines.extend(body_lines)
            return lines
        if isinstance(region, InfiniteLoopRegion):
            lines = [f"{indent}while True:"]
            body_lines = self.render_region_node(region.body_region, indent + "    ")
            if not body_lines:
                body_lines = [f"{indent}    pass"]
            lines.extend(body_lines)
            return lines
        if isinstance(region, SwitchRegion):
            return self.render_switch_region(region, indent)
        if isinstance(region, SelectColorLoopRegion):
            return self.render_select_color_loop(region.header_index, region.cases, indent, show_label=False)
        if isinstance(region, PositiveCaseChainRegion):
            return self.render_positive_case_chain(
                region.header_index,
                region.cases,
                indent,
                show_label=False,
                suppress_terminal_jump_to=region.suppress_terminal_jump_to,
            )
        self.requires_dispatch = True
        return [f"{indent}# unsupported region {region!r}"]

    def render_sequence_region(self, region: SequenceRegion, indent: str) -> list[str]:
        lines: list[str] = []
        child_index = 0
        while child_index < len(region.children):
            folded = self._try_render_guarded_fallthrough(region.children, child_index, indent)
            if folded is not None:
                folded_lines, next_index = folded
                lines.extend(folded_lines)
                child_index = next_index
                continue

            lines.extend(self.render_region_node(region.children[child_index], indent))
            child_index += 1
        return lines

    def _try_render_guarded_fallthrough(
        self,
        children: list[RegionNode],
        child_index: int,
        indent: str,
    ) -> tuple[list[str], int] | None:
        child = children[child_index]
        if not isinstance(child, BlockRegion):
            return None

        block = self.blocks[child.index]
        terminator = block.terminator
        if not isinstance(terminator, BranchStmt):
            return None

        target_index = self.index_by_label.get(terminator.target)
        if target_index is None or target_index <= child.index + 1:
            return None

        slice_start = child_index + 1
        if slice_start >= len(children):
            return None

        slice_end = slice_start
        while slice_end < len(children):
            start_index = self._region_start_index(children[slice_end])
            if start_index is not None and start_index >= target_index:
                break
            slice_end += 1

        if slice_end == slice_start:
            return None

        condition = self.emitter._render_expr(terminator.condition)
        rendered_condition = condition if terminator.jump_if_false else f"not ({condition})"

        lines = self.render_block_body(block, indent, show_label=False)
        lines.append(f"{indent}if {rendered_condition}:")
        guarded_region = SequenceRegion(children=children[slice_start:slice_end])
        guarded_lines = self.render_region_node(guarded_region, indent + "    ")
        if not guarded_lines:
            guarded_lines = [f"{indent}    pass"]
        lines.extend(guarded_lines)
        return lines, slice_end

    def _region_start_index(self, region: RegionNode) -> int | None:
        if isinstance(region, EmptyRegion):
            return None
        if isinstance(region, SequenceRegion):
            for child in region.children:
                start_index = self._region_start_index(child)
                if start_index is not None:
                    return start_index
            return None
        if isinstance(region, BlockRegion | BlockBodyRegion):
            return region.index
        if isinstance(region, InlineExitRegion):
            return region.block_index
        if isinstance(region, DoWhileRegion):
            return self._region_start_index(region.body_region)
        if isinstance(
            region,
            IfRegion | WhileRegion | InfiniteLoopRegion | SwitchRegion | PositiveCaseChainRegion | SelectColorLoopRegion,
        ):
            return region.header_index
        if isinstance(region, GotoRegion):
            return self.index_by_label.get(region.target)
        return None

    def render_if_region(self, region: IfRegion, indent: str) -> list[str]:
        lines: list[str] = []
        current = region
        keyword = "if"

        while True:
            lines.append(f"{indent}{keyword} {self.emitter._render_expr(current.condition)}:")
            then_lines = self.render_region_node(current.then_region, indent + "    ")
            if not then_lines:
                then_lines = [f"{indent}    pass"]
            lines.extend(then_lines)

            if current.else_region is None:
                return lines

            normalized_else = self.normalize_statement_region(current.else_region)
            if isinstance(normalized_else, IfRegion):
                current = normalized_else
                keyword = "elif"
                continue

            lines.append(f"{indent}else:")
            else_lines = self.render_region_node(normalized_else, indent + "    ")
            if not else_lines:
                else_lines = [f"{indent}    pass"]
            lines.extend(else_lines)
            return lines

    def render_switch_region(self, region: SwitchRegion, indent: str) -> list[str]:
        subject_expr = self.emitter._simplify_expr(region.subject)
        subject_rendered = self.emitter._render_expr(subject_expr)
        lines: list[str] = []
        subject_name = subject_rendered
        if len(region.cases) > 1 and not self.emitter._is_simple_expr(subject_expr):
            self.switch_temp_counter += 1
            subject_name = f"switch_value_{region.header_index}_{self.switch_temp_counter}"
            lines.append(f"{indent}{subject_name} = {subject_rendered}")

        for case_index, case in enumerate(region.cases):
            keyword = "if" if case_index == 0 else "elif"
            lines.append(f"{indent}{keyword} {subject_name} == {self.emitter._render_case_value(case.value)}:")
            case_lines = self.render_region_node(case.body, indent + "    ")
            if not case_lines:
                case_lines = [f"{indent}    pass"]
            lines.extend(case_lines)

        if region.default_region is not None:
            lines.append(f"{indent}else:")
            default_lines = self.render_region_node(region.default_region, indent + "    ")
            if not default_lines:
                default_lines = [f"{indent}    pass"]
            lines.extend(default_lines)
        return lines

    def render_inline_exit_block(self, index: int, indent: str) -> list[str]:
        block = self.blocks[index]
        lines: list[str] = []
        for stmt in block.body:
            lines.extend(self._indent_stmt_lines(stmt, indent))
        if block.terminator is not None:
            lines.extend(self._indent_stmt_lines(block.terminator, indent))
        return lines

    def render_select_color_loop(
        self,
        index: int,
        cases: list[tuple[Expr, int]],
        indent: str,
        show_label: bool,
    ) -> list[str]:
        del show_label
        block = self.blocks[index]
        lines = [f"{indent}while True:"]
        lines.extend(self.render_block_body(block, indent + "    ", show_label=False))

        for condition, preview_index in cases:
            lines.append(f"{indent}    if {self.emitter._render_expr(condition)}:")
            preview_lines = self.render_block_fallback(
                preview_index,
                indent + "        ",
                show_label=False,
                suppress_terminal_jump_to=block.label,
            )
            if not preview_lines:
                preview_lines = [f"{indent}        pass"]
            lines.extend(preview_lines)
            lines.append(f"{indent}        continue")

        lines.append(f"{indent}    break")
        return lines

    def render_positive_case_chain(
        self,
        index: int,
        cases: list[tuple[Expr, int]],
        indent: str,
        show_label: bool,
        suppress_terminal_jump_to: str | None = None,
    ) -> list[str]:
        del show_label
        lines = self.render_block_body(self.blocks[index], indent, show_label=False)
        for case_index, (condition, target_index) in enumerate(cases):
            keyword = "if" if case_index == 0 else "elif"
            lines.append(f"{indent}{keyword} {self.emitter._render_expr(condition)}:")
            branch_lines = self.render_block_fallback(
                target_index,
                indent + "    ",
                show_label=False,
                suppress_terminal_jump_to=suppress_terminal_jump_to,
            )
            if not branch_lines:
                branch_lines = [f"{indent}    pass"]
            lines.extend(branch_lines)
        return lines

    def render_block_body(self, block: BasicBlock, indent: str, show_label: bool = True) -> list[str]:
        del show_label
        lines: list[str] = []
        for stmt in block.body:
            lines.extend(self._indent_stmt_lines(stmt, indent))
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
        del show_label
        block = self.blocks[index]
        lines = self.render_block_body(block, indent, show_label=False)
        terminator = block.terminator
        if isinstance(terminator, JumpStmt):
            if suppress_terminal_jump_to is not None and terminator.target == suppress_terminal_jump_to:
                return lines
            if loop_header_label is not None and terminator.target == loop_header_label:
                lines.append(f"{indent}continue")
                return lines
            if loop_exit_label is not None and terminator.target == loop_exit_label:
                lines.append(f"{indent}break")
                return lines
            if switch_exit_label is not None and terminator.target == switch_exit_label:
                return lines
            if terminator.target == self.next_block_label(index):
                return lines
            inline_exit_lines = self._render_inline_epilogue_jump(terminator.target, indent)
            if inline_exit_lines is not None:
                lines.extend(inline_exit_lines)
                return lines
            inline_tail_lines = self._render_inline_returning_tail(terminator.target, indent)
            if inline_tail_lines is not None:
                lines.extend(inline_tail_lines)
                return lines
            self.requires_dispatch = True
            lines.append(f"{indent}# goto {terminator.target}")
            return lines
        if isinstance(terminator, BranchStmt):
            self.requires_dispatch = True
            condition = self.emitter._render_condition_expr(terminator.condition, negate=terminator.jump_if_false)
            lines.append(f"{indent}if {condition}:")
            lines.append(f"{indent}    # goto {terminator.target}")
            return lines
        if terminator is not None:
            lines.extend(self._indent_stmt_lines(terminator, indent))
        return lines

    def _indent_stmt_lines(self, stmt: Stmt, indent: str) -> list[str]:
        return [f"{indent}{line}" if line else "" for line in self.emitter._emit_python_stmt(stmt)]

    def _render_inline_epilogue_jump(self, target: str, indent: str) -> list[str] | None:
        target_index = self.index_by_label.get(target)
        if target_index is None:
            return None

        block = self.blocks[target_index]
        if block.successor_labels:
            return None
        if not isinstance(block.terminator, ReturnStmt):
            return None
        if any(not isinstance(stmt, UnregisterExceptionStmt | SetBaseStmt) for stmt in block.body):
            return None

        lines: list[str] = []
        for stmt in block.body:
            lines.extend(self._indent_stmt_lines(stmt, indent))
        lines.extend(self._indent_stmt_lines(block.terminator, indent))
        return lines

    def _render_inline_returning_tail(self, target: str, indent: str) -> list[str] | None:
        if target in self._tail_render_cache:
            cached = self._tail_render_cache[target]
            if cached is None:
                return None
            return [f"{indent}{line}" if line else "" for line in cached]

        target_index = self.index_by_label.get(target)
        if target_index is None:
            self._tail_render_cache[target] = None
            return None

        # Nested tail emitters can encounter the same target again through a
        # backward edge. Mark it before descending and share the cache so the
        # recursive attempt falls back to dispatch instead of recursing forever.
        self._tail_render_cache[target] = None
        tail_emitter = StructuredPythonFunctionEmitter(self.emitter, self.function, self._tail_render_cache)
        root = tail_emitter.build_region_tree(target_index, len(self.blocks), suppress_first_label=True)
        tail_lines = tail_emitter.render_region_node(root, "")
        if tail_emitter.requires_dispatch:
            self._tail_render_cache[target] = None
            return None

        last_code_line = next((line.strip() for line in reversed(tail_lines) if line.strip()), "")
        if last_code_line != "return":
            self._tail_render_cache[target] = None
            return None

        self._tail_render_cache[target] = tail_lines
        return [f"{indent}{line}" if line else "" for line in tail_lines]


class PythonDispatchEmitter:
    def __init__(self, emitter: BscPythonEmitter, function: Function) -> None:
        self.emitter = emitter
        self.function = function
        self.blocks = function.blocks

    def render(self) -> list[str]:
        if not self.blocks:
            return ["pass"]

        lines = [f"pc = {self.emitter._render_string(self.blocks[0].label)}", "while True:"]
        for index, block in enumerate(self.blocks):
            keyword = "if" if index == 0 else "elif"
            lines.append(f"    {keyword} pc == {self.emitter._render_string(block.label)}:")
            block_lines = self._emit_block(index, block)
            if not block_lines:
                block_lines = ["pass"]
            lines.extend(self.emitter._indent_lines(block_lines, 2))
        lines.append("    else:")
        lines.append("        return")
        return lines

    def _emit_block(self, index: int, block: BasicBlock) -> list[str]:
        lines: list[str] = []
        for stmt in block.body:
            lines.extend(self.emitter._emit_python_stmt(stmt))

        terminator = block.terminator
        next_label = self.blocks[index + 1].label if index + 1 < len(self.blocks) else None
        if terminator is None:
            if next_label is None:
                lines.append("return")
            else:
                lines.extend(self._emit_pc_transition(next_label))
            return lines

        if isinstance(terminator, ReturnStmt):
            if terminator.value is None:
                lines.append("return")
            else:
                lines.append(f"return {self.emitter._render_expr(terminator.value)}")
            return lines

        if isinstance(terminator, JumpStmt):
            lines.extend(self._emit_pc_transition(terminator.target))
            return lines

        if isinstance(terminator, BranchStmt):
            condition = self.emitter._render_condition_expr(terminator.condition, negate=terminator.jump_if_false)
            branch_target = terminator.target
            fallthrough_target = next_label
            if terminator.jump_if_false:
                lines.append(f"if {condition}:")
                lines.extend(self.emitter._indent_lines(self._emit_pc_transition(branch_target), 1))
                lines.extend(self._emit_pc_transition(fallthrough_target))
            else:
                lines.append(f"if {condition}:")
                lines.extend(self.emitter._indent_lines(self._emit_pc_transition(branch_target), 1))
                lines.extend(self._emit_pc_transition(fallthrough_target))
            return lines

        lines.append(f"# unsupported terminator {terminator!r}")
        lines.append("return")
        return lines

    def _emit_pc_transition(self, target: str | None) -> list[str]:
        if target is None:
            return ["return"]
        return [f"pc = {self.emitter._render_string(target)}", "continue"]
