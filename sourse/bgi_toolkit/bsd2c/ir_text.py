from __future__ import annotations

from .ir import BasicBlock, Function, Module


def format_basic_block_ir(block: BasicBlock, indent: str = "    ") -> list[str]:
    lines = [f"{indent}block {block.id} {block.label}:"]
    if block.body:
        lines.append(f"{indent}  body:")
        for stmt in block.body:
            lines.append(f"{indent}    {stmt!r}")
    else:
        lines.append(f"{indent}  body: []")
    lines.append(f"{indent}  terminator: {block.terminator!r}")
    lines.append(f"{indent}  successors: {block.successor_labels!r}")
    return lines


def format_function_ir(function: Function) -> list[str]:
    lines = [
        f"function {function.name}:",
        f"  return_type: {function.return_type}",
        f"  arg_count: {function.arg_count}",
        f"  params: {function.params!r}",
        f"  param_types: {function.param_types!r}",
        "  blocks:",
    ]
    for block in function.blocks:
        lines.extend(format_basic_block_ir(block, indent="    "))
    return lines


def format_module_ir(module: Module) -> str:
    lines = [
        f"source_type: {module.source_type}",
        f"header_text: {module.metadata.header_text!r}",
        f"header_padding: {module.metadata.header_padding!r}",
        f"imports: {module.metadata.imports!r}",
        f"exports: {module.metadata.exports!r}",
        f"defines: {module.metadata.defines!r}",
        f"function_count: {len(module.functions)}",
    ]
    for function in module.functions:
        lines.append("")
        lines.extend(format_function_ir(function))
    return "\n".join(lines) + "\n"