from __future__ import annotations

import argparse
from pathlib import Path
from ..bp_engine import add_engine_arguments

from .pipeline import (
    decompile_file,
    decompile_target,
    derive_ir_output_path,
    iter_input_files,
    write_ir_result,
    write_result,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 BGI .bpd VM 汇编反编译为可追踪的 C 风格代码")
    parser.add_argument("target", help="输入 .bpd 文件或包含 .bpd 的目录")
    parser.add_argument("-o", "--output", help="单文件 C 输出路径")
    parser.add_argument("--output-dir", help="批量模式的 C 输出目录")
    parser.add_argument("--ir", action="store_true", help="单文件模式同时写出 .ir.txt")
    parser.add_argument("--ir-dir", help="批量模式的 IR 输出目录")
    parser.add_argument("--no-recursive", action="store_true", help="目录模式不扫描子目录")
    add_engine_arguments(parser)
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        help="批量反编译工作进程数，0 表示自动（默认最多 8）",
    )
    parser.add_argument(
        "--no-source-comments",
        action="store_true",
        help="C 输出不附加原始 BPD 行号注释",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = Path(args.target)
    recursive = not args.no_recursive
    inputs = iter_input_files(target, recursive=recursive)
    if not inputs:
        parser.error(f"没有找到 .bpd 输入: {target}")

    if target.is_file():
        if args.output_dir:
            parser.error("单文件模式请使用 --output，不要使用 --output-dir")
        result = decompile_file(
            target,
            include_source_comments=not args.no_source_comments,
            engine=args.bp_engine, games_path=args.bp_games, executable=args.bp_exe,
        )
        if result.assembly.engine_detection:
            print(result.assembly.engine_detection.describe())
        output_path = write_result(result, args.output)
        print(f"{target} -> {output_path}")
        if args.ir or args.ir_dir:
            ir_path = Path(args.ir_dir) / derive_ir_output_path(target).name if args.ir_dir else None
            ir_path = write_ir_result(result, ir_path)
            print(f"IR -> {ir_path}")
        stats = result.module.stats
        print(
            f"完成: {stats.instruction_count} 条指令, {result.label_count} 个标签, "
            f"{stats.lifted_count} 条已提升, {stats.opaque_count} 条 VM opcode 调用"
        )
        if result.module.diagnostics:
            print(f"警告: {len(result.module.diagnostics)} 条解析诊断")
        return 0

    if args.output:
        parser.error("目录模式请使用 --output-dir，不要使用 --output")
    if args.jobs < 0:
        parser.error("--jobs 不能为负数")
    output_dir = Path(args.output_dir) if args.output_dir else target.with_name(target.name + "_c")
    written = decompile_target(
        target,
        output_dir,
        ir_dir=args.ir_dir,
        recursive=recursive,
        progress=print,
        include_source_comments=not args.no_source_comments,
        workers=None if args.jobs == 0 else args.jobs,
        engine=args.bp_engine, games_path=args.bp_games, executable=args.bp_exe,
    )
    instruction_count = sum(item.module.stats.instruction_count for item, _, _ in written)
    opaque_count = sum(item.module.stats.opaque_count for item, _, _ in written)
    diagnostics = sum(len(item.module.diagnostics) for item, _, _ in written)
    print(
        f"完成: {len(written)} 个文件, {instruction_count} 条指令, "
        f"{opaque_count} 条 VM opcode 调用, {diagnostics} 条解析诊断"
    )
    print(f"C 输出目录: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
