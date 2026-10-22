from __future__ import annotations

from .ir import Module


def format_module_ir(module: Module) -> str:
    stats = module.stats
    lines = [
        f"source: {module.source_path}",
        f"bp_engine: {module.engine.name} (generation {module.engine.generation})",
        f"blocks: {len(module.blocks)}",
        f"instructions: {stats.instruction_count}",
        f"lifted: {stats.lifted_count}",
        f"opaque: {stats.opaque_count}",
        f"stack_sync: {stats.stack_sync_count}",
        f"diagnostics: {len(module.diagnostics)}",
        f"stack_diagnostics: {len(module.stack_diagnostics)}",
    ]
    if module.cfg is not None:
        lines.extend(
            [
                f"cfg_rpo: {','.join(str(index) for index in module.cfg.reverse_postorder)}",
                f"cfg_back_edges: {len(module.cfg.back_edges)}",
                f"cfg_sccs: {len(module.cfg.strongly_connected_components)}",
            ]
        )
    for diagnostic in module.diagnostics:
        lines.append(f"  ! {diagnostic}")

    for block in module.blocks:
        lines.extend(["", f"block {block.label} (source L{block.source_line}):"])
        if block.stack_in or block.stack_out or block.stack_phis:
            lines.append(
                "  stack_ssa: "
                f"in={[value.value_id for value in block.stack_in]} "
                f"out={[value.value_id for value in block.stack_out]} "
                f"phi={[value.value_id for value in block.stack_phis]}"
            )
        for statement in block.statements:
            source = ",".join(str(line) for line in statement.source_lines) or "-"
            target = f" -> {statement.control_target}" if statement.control_target is not None else ""
            lines.append(
                f"  [{statement.kind}/{statement.node_kind.value} "
                f"effects={int(statement.effects)} L{source}]{target} {statement.text}"
            )
        lines.append(f"  successors: {', '.join(block.successors) if block.successors else '-'}")
    return "\n".join(lines) + "\n"
