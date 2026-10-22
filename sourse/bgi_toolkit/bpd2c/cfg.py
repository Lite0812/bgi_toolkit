from __future__ import annotations

from dataclasses import dataclass, field
import re

from .ir import (
    BasicBlock,
    DirectLabelTarget,
    Expression,
    StackValue,
    ValueType,
    merge_value_types,
)


CONTROL_KINDS = {"branch", "jump", "jump_dynamic", "return"}
TERMINAL_KINDS = {"jump", "jump_dynamic", "return"}
STACK_PUSH_RE = re.compile(r"^vm_push\(vm, (?P<value>.*)\);$")


@dataclass(slots=True)
class StackSSAResult:
    diagnostics: tuple[str, ...]
    incoming: dict[int, tuple[StackValue, ...]]
    outgoing: dict[int, tuple[StackValue, ...]]
    phis: dict[int, tuple[StackValue, ...]]


@dataclass(slots=True)
class FunctionOwnership:
    roots: tuple[str, ...]
    reachable_by_root: dict[str, frozenset[int]]
    owners_by_block: dict[int, frozenset[str]]
    shared_blocks: frozenset[int]


@dataclass(slots=True)
class ControlFlowGraph:
    blocks: list[BasicBlock]
    index_by_label: dict[str, int]
    successors: dict[int, tuple[int, ...]]
    predecessors: dict[int, set[int]]
    reachable: set[int]
    reverse_postorder: tuple[int, ...]
    exits: set[int]
    dominators: dict[int, set[int]]
    immediate_dominators: dict[int, int | None]
    postdominators: dict[int, set[int]]
    immediate_postdominators: dict[int, int | None]
    strongly_connected_components: tuple[frozenset[int], ...]
    back_edges: tuple[tuple[int, int], ...]
    natural_loops: dict[tuple[int, int], frozenset[int]]
    invalid_targets: tuple[tuple[int, str], ...] = ()

    @classmethod
    def build(
        cls,
        blocks: list[BasicBlock],
        *,
        rebuild_successors: bool = False,
    ) -> ControlFlowGraph:
        if rebuild_successors:
            cls.assign_successors(blocks)
        index_by_label = {block.label: index for index, block in enumerate(blocks)}
        for block in blocks:
            for statement in block.statements:
                if statement.target is not None and statement.control_target != DirectLabelTarget(
                    statement.target
                ):
                    statement.set_target(statement.target)
        successors: dict[int, tuple[int, ...]] = {}
        predecessors: dict[int, set[int]] = {index: set() for index in range(len(blocks))}
        invalid_targets: list[tuple[int, str]] = []
        for source, block in enumerate(blocks):
            targets: list[int] = []
            for label in block.successors:
                target = index_by_label.get(label)
                if target is None:
                    invalid_targets.append((source, label))
                    continue
                if target not in targets:
                    targets.append(target)
                    predecessors[target].add(source)
            successors[source] = tuple(targets)

        reachable, reverse_postorder = cls._reachable_rpo(successors, len(blocks))
        exits = {
            index for index in reachable if not [item for item in successors[index] if item in reachable]
        }
        dominators = cls._dominators(reachable, predecessors, 0 if blocks else None)
        immediate_dominators = cls._immediate_relations(dominators, 0 if blocks else None)
        postdominators = cls._postdominators(reachable, successors, exits)
        immediate_postdominators = cls._immediate_relations(postdominators, None)
        components = cls._tarjan(successors, reachable)
        back_edges = tuple(
            (source, target)
            for source in sorted(reachable)
            for target in successors[source]
            if target in dominators.get(source, set())
        )
        natural_loops = {
            edge: frozenset(cls._natural_loop(edge[0], edge[1], predecessors))
            for edge in back_edges
        }
        return cls(
            blocks=blocks,
            index_by_label=index_by_label,
            successors=successors,
            predecessors=predecessors,
            reachable=reachable,
            reverse_postorder=reverse_postorder,
            exits=exits,
            dominators=dominators,
            immediate_dominators=immediate_dominators,
            postdominators=postdominators,
            immediate_postdominators=immediate_postdominators,
            strongly_connected_components=components,
            back_edges=back_edges,
            natural_loops=natural_loops,
            invalid_targets=tuple(invalid_targets),
        )

    @staticmethod
    def assign_successors(blocks: list[BasicBlock]) -> None:
        labels = {block.label for block in blocks}
        for index, block in enumerate(blocks):
            successors: list[str] = []
            for statement in block.statements:
                if statement.kind == "branch" and statement.target in labels:
                    if statement.target not in successors:
                        successors.append(statement.target)
            last = block.statements[-1] if block.statements else None
            if last is not None and last.kind == "jump" and last.target in labels:
                block.successors = [last.target]
                continue
            if last is None or last.kind not in TERMINAL_KINDS:
                if index + 1 < len(blocks):
                    fallthrough = blocks[index + 1].label
                    if fallthrough not in successors:
                        successors.append(fallthrough)
            block.successors = successors

    @staticmethod
    def _reachable_rpo(
        successors: dict[int, tuple[int, ...]],
        block_count: int,
    ) -> tuple[set[int], tuple[int, ...]]:
        if block_count == 0:
            return set(), ()
        visited: set[int] = set()
        postorder: list[int] = []
        pending: list[tuple[int, bool]] = [(0, False)]
        while pending:
            index, expanded = pending.pop()
            if expanded:
                postorder.append(index)
                continue
            if index in visited:
                continue
            visited.add(index)
            pending.append((index, True))
            for successor in reversed(successors.get(index, ())):
                if successor not in visited:
                    pending.append((successor, False))
        return visited, tuple(reversed(postorder))

    @staticmethod
    def _dominators(
        reachable: set[int],
        predecessors: dict[int, set[int]],
        entry: int | None,
    ) -> dict[int, set[int]]:
        if entry is None or entry not in reachable:
            return {}
        result = {
            index: ({entry} if index == entry else set(reachable))
            for index in reachable
        }
        changed = True
        while changed:
            changed = False
            for index in sorted(reachable):
                if index == entry:
                    continue
                incoming = [item for item in predecessors[index] if item in reachable]
                common = set(reachable) if not incoming else set(result[incoming[0]])
                for predecessor in incoming[1:]:
                    common.intersection_update(result[predecessor])
                updated = {index} | common
                if updated != result[index]:
                    result[index] = updated
                    changed = True
        return result

    @staticmethod
    def _postdominators(
        reachable: set[int],
        successors: dict[int, tuple[int, ...]],
        exits: set[int],
    ) -> dict[int, set[int]]:
        if not reachable:
            return {}
        virtual_exit = max(reachable) + 1
        universe = set(reachable) | {virtual_exit}
        result: dict[int, set[int]] = {
            index: ({virtual_exit} if index == virtual_exit else set(universe))
            for index in universe
        }
        changed = True
        while changed:
            changed = False
            for index in sorted(reachable, reverse=True):
                outgoing = [item for item in successors[index] if item in reachable]
                if not outgoing or index in exits:
                    outgoing = [virtual_exit]
                common = set(result[outgoing[0]])
                for successor in outgoing[1:]:
                    common.intersection_update(result[successor])
                updated = {index} | common
                if updated != result[index]:
                    result[index] = updated
                    changed = True
        return {
            index: {item for item in values if item != virtual_exit}
            for index, values in result.items()
            if index != virtual_exit
        }

    @staticmethod
    def _immediate_relations(
        relations: dict[int, set[int]],
        root: int | None,
    ) -> dict[int, int | None]:
        result: dict[int, int | None] = {}
        for index, values in relations.items():
            if root is not None and index == root:
                result[index] = None
                continue
            strict = values - {index}
            result[index] = max(strict, key=lambda item: len(relations.get(item, ())), default=None)
        return result

    @staticmethod
    def _tarjan(
        successors: dict[int, tuple[int, ...]],
        reachable: set[int],
    ) -> tuple[frozenset[int], ...]:
        next_index = 0
        indices: dict[int, int] = {}
        lowlinks: dict[int, int] = {}
        stack: list[int] = []
        on_stack: set[int] = set()
        components: list[frozenset[int]] = []

        def visit(node: int) -> None:
            nonlocal next_index
            indices[node] = next_index
            lowlinks[node] = next_index
            next_index += 1
            stack.append(node)
            on_stack.add(node)
            for successor in successors[node]:
                if successor not in reachable:
                    continue
                if successor not in indices:
                    visit(successor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[successor])
                elif successor in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[successor])
            if lowlinks[node] != indices[node]:
                return
            component: set[int] = set()
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            components.append(frozenset(component))

        for node in sorted(reachable):
            if node not in indices:
                visit(node)
        return tuple(components)

    @staticmethod
    def _natural_loop(
        source: int,
        header: int,
        predecessors: dict[int, set[int]],
    ) -> set[int]:
        if source == header:
            return {header}
        loop = {header, source}
        pending = [source]
        while pending:
            node = pending.pop()
            for predecessor in predecessors[node]:
                if predecessor in loop:
                    continue
                loop.add(predecessor)
                pending.append(predecessor)
        return loop

    def dominates(self, dominator: int, node: int) -> bool:
        return dominator in self.dominators.get(node, set())

    def postdominates(self, postdominator: int, node: int) -> bool:
        return postdominator in self.postdominators.get(node, set())

    def natural_loop_for_header(self, header: int) -> frozenset[int]:
        nodes: set[int] = set()
        for (_, target), loop in self.natural_loops.items():
            if target == header:
                nodes.update(loop)
        return frozenset(nodes)


def analyze_stack_ssa(blocks: list[BasicBlock]) -> StackSSAResult:
    """Build fixed-point stack states and stable phi values for CFG joins.

    This pass models the explicit edge protocol left by the instruction
    lifter. Function-entry underflow is represented as an external value;
    incompatible predecessor heights are retained as a diagnostic instead of
    inventing a value.
    """
    cfg = ControlFlowGraph.build(blocks)
    next_value_id = 1
    push_values: dict[int, StackValue] = {}
    external_values: dict[tuple[int, int], StackValue] = {}
    phi_values: dict[tuple[int, int], StackValue] = {}
    diagnostics: set[str] = set()
    incoming: dict[int, tuple[StackValue, ...]] = {}
    outgoing: dict[int, tuple[StackValue, ...]] = {}

    def allocate(expression: Expression, sources: tuple[str, ...]) -> StackValue:
        nonlocal next_value_id
        value = StackValue(next_value_id, expression, expression.value_type, sources)
        expression.value_id = next_value_id
        next_value_id += 1
        return value

    def merged_stack(index: int) -> tuple[StackValue, ...] | None:
        predecessors = cfg.predecessors[index]
        known = [outgoing[item] for item in predecessors if item in outgoing]
        if predecessors and len(known) != len(predecessors):
            return None
        if not known:
            return ()
        heights = {len(stack) for stack in known}
        height = min(heights)
        if len(heights) != 1:
            diagnostics.add(
                f"{blocks[index].label}: incompatible stack heights "
                + ", ".join(str(item) for item in sorted(heights))
            )
        result: list[StackValue] = []
        for slot in range(height):
            values = [stack[slot] for stack in known]
            if all(value.value_id == values[0].value_id for value in values[1:]):
                result.append(values[0])
                continue
            key = (index, slot)
            value_type = merge_value_types([value.value_type for value in values])
            phi = phi_values.get(key)
            if phi is None:
                expression = Expression(
                    "stack_phi(" + ", ".join(value.expression.text for value in values) + ")",
                    value_type=value_type,
                )
                phi = allocate(
                    expression,
                    tuple(blocks[item].label for item in sorted(predecessors)),
                )
                phi_values[key] = phi
            else:
                phi.value_type = value_type
                phi.expression.value_type = value_type
            result.append(phi)
        return tuple(result)

    pending = list(cfg.reverse_postorder)
    pending.extend(index for index in range(len(blocks)) if index not in cfg.reachable)
    iterations = 0
    iteration_limit = max(32, len(blocks) * 16)
    while pending and iterations < iteration_limit:
        iterations += 1
        index = pending.pop(0)
        merged = merged_stack(index)
        if merged is None:
            pending.append(index)
            continue
        stack = list(merged)
        external_index = 0
        for statement in blocks[index].statements:
            text = statement.condition or statement.text
            pop_count = text.count("vm_pop(vm)")
            for _ in range(pop_count):
                if stack:
                    stack.pop()
                    continue
                key = (index, external_index)
                if key not in external_values:
                    external_values[key] = allocate(
                        Expression(f"vm_arg(vm, {external_index})"),
                        (blocks[index].label,),
                    )
                external_index += 1
            pushed = STACK_PUSH_RE.fullmatch(statement.text.strip())
            if pushed is None:
                continue
            value = push_values.get(id(statement))
            if value is None:
                value = allocate(
                    Expression(pushed.group("value"), source_lines=statement.source_lines),
                    (blocks[index].label,),
                )
                push_values[id(statement)] = value
            stack.append(value)
        next_out = tuple(stack)
        if incoming.get(index) == merged and outgoing.get(index) == next_out:
            continue
        incoming[index] = merged
        outgoing[index] = next_out
        for successor in cfg.successors[index]:
            if successor not in pending:
                pending.append(successor)

    if pending:
        diagnostics.add("stack SSA did not converge")

    phis: dict[int, tuple[StackValue, ...]] = {}
    for index, block in enumerate(blocks):
        block.stack_in = incoming.get(index, ())
        block.stack_out = outgoing.get(index, ())
        block_phis = tuple(
            value for (block_index, _), value in phi_values.items() if block_index == index
        )
        block.stack_phis = block_phis
        phis[index] = block_phis
    return StackSSAResult(tuple(sorted(diagnostics)), incoming, outgoing, phis)


def compute_function_ownership(
    blocks: list[BasicBlock],
    root_labels: list[str] | tuple[str, ...],
) -> FunctionOwnership:
    """Assign CFG-reachable blocks to roots and identify explicitly shared tails."""
    cfg = ControlFlowGraph.build(blocks)
    roots = tuple(label for label in root_labels if label in cfg.index_by_label)
    reachable_by_root: dict[str, frozenset[int]] = {}
    owners: dict[int, set[str]] = {index: set() for index in range(len(blocks))}
    for label in roots:
        root = cfg.index_by_label[label]
        visited: set[int] = set()
        pending = [root]
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            pending.extend(
                successor
                for successor in cfg.successors[index]
                if successor not in visited
            )
        reachable_by_root[label] = frozenset(visited)
        for index in visited:
            owners[index].add(label)
    owners_by_block = {
        index: frozenset(labels)
        for index, labels in owners.items()
    }
    return FunctionOwnership(
        roots,
        reachable_by_root,
        owners_by_block,
        frozenset(index for index, labels in owners.items() if len(labels) > 1),
    )
