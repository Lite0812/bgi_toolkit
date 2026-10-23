from __future__ import annotations

import argparse
from pathlib import Path

from .rpy_emitter import RpyEmitOptions
from .pipeline import (
    build_project_rpy_context,
    decompile_file,
    decompile_files,
    derive_ir_output_path,
    iter_input_files,
    derive_output_path_for_format,
    write_definition_bundle,
    write_ir_result,
    write_result,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Decompile BGI BSD/BSC/BSB assembly text into C-like output")
    parser.add_argument("target", help="Input .bsd file, .bsd.txt file, or a directory to scan recursively")
    parser.add_argument("-o", "--output", help="Output path for single-file mode")
    parser.add_argument("--output-dir", help="Output directory for generated files and definitions bundle")
    parser.add_argument("--ir-dir", help="Output directory for generated IR text files")
    parser.add_argument("--hint", choices=["auto", "bsc", "bs5", "bsb", "bss"], default="auto")
    parser.add_argument("--format", dest="output_format", choices=["c", "rpy"], default="c")
    parser.add_argument(
        "--rpy-strip-double-zero-prefix",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When writing RPY, collapse leading 00 in script-derived names such as 001_xxx -> 01_xxx.",
    )
    parser.add_argument(
        "--rpy-strip-three-digit-prefix",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="When writing RPY, remove leading three-digit prefix and underscore such as 001_xxx -> xxx.",
    )
    parser.add_argument(
        "--rpy-lowercase-labels",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="When writing RPY, lowercase all label declarations and call/jump targets.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target_path = Path(args.target)

    inputs = list(iter_input_files(args.target))
    if not inputs:
        parser.error("No .bsd or .bsd.txt inputs found.")

    if args.output and args.output_dir:
        parser.error("--output and --output-dir cannot be used together.")

    if args.output and len(inputs) != 1:
        parser.error("--output can only be used with a single input file.")

    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    ir_dir = None
    if args.ir_dir:
        ir_dir = Path(args.ir_dir)
        ir_dir.mkdir(parents=True, exist_ok=True)

    override = None if args.hint == "auto" else args.hint
    rpy_emit_options = RpyEmitOptions(
        strip_double_zero_prefix=args.rpy_strip_double_zero_prefix,
        strip_three_digit_prefix=args.rpy_strip_three_digit_prefix,
        lowercase_labels=args.rpy_lowercase_labels,
    )
    if len(inputs) == 1:
        results = [decompile_file(inputs[0], override)]
    else:
        results = decompile_files(inputs, override)

    project_context = (
        build_project_rpy_context(results, rpy_emit_options=rpy_emit_options) if args.output_format == "rpy" else None
    )

    for source_path, result in zip(inputs, results):
        destination_override = args.output if len(inputs) == 1 else None
        relative_source_path = source_path
        if target_path.is_dir():
            relative_source_path = source_path.relative_to(target_path)
        if output_dir is not None:
            destination_override = output_dir / derive_output_path_for_format(
                relative_source_path,
                result.detected_type,
                args.output_format,
                rpy_emit_options,
            )
        destination = write_result(
            result,
            destination_override,
            output_format=args.output_format,
            project_context=project_context,
            rpy_emit_options=rpy_emit_options,
        )
        print(f"{source_path} -> {destination}")
        if ir_dir is not None:
            ir_base_name = derive_output_path_for_format(relative_source_path, result.detected_type, "c")
            ir_destination = ir_dir / derive_ir_output_path(ir_base_name)
            ir_written = write_ir_result(result, ir_destination)
            print(f"{source_path} -> {ir_written}")

    definitions_target = output_dir / "bsd2c_defs.json" if output_dir is not None else None
    definitions_path = write_definition_bundle(results, definitions_target)
    print(f"definitions -> {definitions_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
