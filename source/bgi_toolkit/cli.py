from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from . import __version__
from .asdis.service import AsdisOptions
from .bp_engine import PROFILES, add_engine_arguments, detect_engine
from .gdb_tool import (
    GdbFormatError,
    _default_decode_output,
    _default_encode_output,
    _load_json,
    _pretty_json,
    _summary,
    decode_gdb,
    encode_gdb,
)
from .models import Operation, TaskOptions, TaskRequest, TextFormat
from .runner import ToolkitRunner


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", type=Path, help="input file or directory")
    parser.add_argument("-o", "--output", type=Path, help="output file or output root")
    parser.add_argument("--encoding", default="cp932", help="output/main encoding")
    parser.add_argument("--fallback-encoding", default="gbk")
    parser.add_argument("--source-encoding", help="source decoding encoding")
    parser.add_argument("--source-fallback-encoding", help="source fallback encoding")
    parser.add_argument(
        "--script-version",
        choices=["auto", "v0", "v1"],
        default="auto",
    )
    parser.add_argument("--exact", action="store_true", help="preserve exact string data when supported")
    parser.add_argument(
        "--f07e-mode",
        choices=["auto", "offset", "string"],
        default="auto",
        help="v1 f_07e operand: auto-detect, legacy code offset, or function-name string",
    )
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--keep-intermediate", action="store_true")
    add_engine_arguments(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bgi-toolkit",
        description="Unified BGI assembler, text tool, and BSD/BPD decompiler",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="detect supported input types")
    inspect_parser.add_argument("target", type=Path)
    inspect_parser.add_argument("--no-recursive", action="store_true")

    engine_parser = subparsers.add_parser('bp-engine', help='识别 BP 引擎版本或列出配置')
    engine_parser.add_argument('target', nargs='?', type=Path)
    engine_parser.add_argument('--list', action='store_true', help='列出内置 BP 引擎配置')
    engine_parser.add_argument('--json', action='store_true', help='输出识别依据与指针布局 JSON')
    add_engine_arguments(engine_parser)

    disasm_parser = subparsers.add_parser("disasm", help="convert story/BP binaries to BSD/BPD")
    _add_common_options(disasm_parser)

    asm_parser = subparsers.add_parser("asm", help="assemble BSD/BPD files")
    _add_common_options(asm_parser)

    decompile_parser = subparsers.add_parser("decompile", help="decompile binary or BSD/BPD input")
    _add_common_options(decompile_parser)
    decompile_parser.add_argument("--format", choices=["c", "rpy"], default="c")
    decompile_parser.add_argument("--ir", action="store_true", help="also write textual IR")
    decompile_parser.add_argument(
        "--hint",
        choices=["auto", "bsc", "bs5", "bsb", "bss"],
        default="auto",
    )
    decompile_parser.add_argument("--no-source-comments", action="store_true")

    text_parser = subparsers.add_parser("text", help="export or import translatable text")
    text_subparsers = text_parser.add_subparsers(dest="text_command", required=True)

    export_parser = text_subparsers.add_parser("export", help="export JSON or TXT")
    _add_common_options(export_parser)
    export_parser.add_argument("--format", choices=["json", "txt"], default="json")
    export_parser.add_argument(
        "--user-function",
        action="append",
        default=[],
        help='system/user text function or rule, e.g. "f_321:1=name 2=message 3=ignore"; repeatable',
    )

    import_parser = text_subparsers.add_parser("import", help="import JSON or TXT")
    _add_common_options(import_parser)
    import_parser.add_argument("translation", type=Path, help="translation file or directory")
    import_parser.add_argument("--format", choices=["json", "txt"], default="json")
    import_parser.add_argument(
        "--user-function",
        action="append",
        default=[],
        help='system/user text function or rule, e.g. "f_321:1=name 2=message 3=ignore"; repeatable',
    )

    gdb_parser = subparsers.add_parser("gdb", help="convert BURIKO GDB saves to/from JSON")
    gdb_subparsers = gdb_parser.add_subparsers(dest="gdb_command", required=True)
    for command, help_text in (
        ("decode", "convert GDB to semantic JSON"),
        ("encode", "rebuild GDB from semantic JSON"),
    ):
        command_parser = gdb_subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("input", type=Path)
        command_parser.add_argument("output", type=Path, nargs="?")
    gdb_inspect_parser = gdb_subparsers.add_parser("inspect", help="show a concise GDB summary")
    gdb_inspect_parser.add_argument("input", type=Path)
    gdb_verify_parser = gdb_subparsers.add_parser("verify", help="require a byte-identical GDB round trip")
    gdb_verify_parser.add_argument("input", type=Path)

    db_parser = subparsers.add_parser("db", help="ScenarioDB / StringsDB JSON 转换与无损验证")
    db_commands = db_parser.add_subparsers(dest="db_command", required=True)
    from .db_text_tool import add_conversion_arguments
    for operation in ("export", "build", "check"):
        add_conversion_arguments(db_commands.add_parser(operation), operation)

    bsi_parser = subparsers.add_parser('bsi', help='BP2 BSI 字符串表与 JSON 互转及无损验证')
    bsi_commands = bsi_parser.add_subparsers(dest='bsi_command', required=True)
    from .bsi_tool import add_conversion_arguments as add_bsi_arguments
    for operation in ('export', 'build', 'check'):
        add_bsi_arguments(bsi_commands.add_parser(operation), operation)

    audio_parser = subparsers.add_parser('audio', help='BGI BW 原音频导出与常见音频格式转换')
    audio_commands = audio_parser.add_subparsers(dest='audio_command', required=True)
    for operation in ('export', 'convert', 'info'):
        command = audio_commands.add_parser(operation)
        command.add_argument('input', type=Path)
        command.add_argument('--no-recursive', action='store_true')
        if operation != 'info':
            command.add_argument('-o', '--output', type=Path)
            command.add_argument('--force', action='store_true')
            command.add_argument('--no-metadata', action='store_true')
        if operation == 'convert':
            command.add_argument('--format', choices=['wav','flac','ogg','mp3','aac','m4a','aiff','bw','bw_pcm'], default='wav')
            command.add_argument('--metadata', type=Path, help='BW 头信息 JSON；默认读取输入音频旁的同名 JSON')
            command.add_argument('--ffmpeg', default='ffmpeg')
            command.add_argument('--sample-rate', type=int)
            command.add_argument('--channels', type=int, choices=[1,2])
            command.add_argument('--bitrate', type=int, default=192, help='MP3 / OGG 码率，kbps')
            command.add_argument('--wav-bits', type=int, choices=[16,24,32], default=16)

    for kind in ('sdc', 'dsc'):
        sdc_parser = subparsers.add_parser(kind, help=f'{kind.upper()} FORMAT 1.00 解压与压缩')
        sdc_commands = sdc_parser.add_subparsers(dest='sdc_command', required=True)
        for operation in ('decompress', 'compress'):
            command = sdc_commands.add_parser(operation)
            command.add_argument('input', type=Path)
            command.add_argument('-o', '--output', type=Path)
            command.add_argument('--force', action='store_true')
            command.add_argument('--no-recursive', action='store_true')
            if operation == 'decompress':
                command.add_argument('--no-metadata', action='store_true')
            else:
                command.add_argument('--metadata', type=Path, help='封装信息 JSON；默认读取输入文件旁的同名 JSON')
                command.add_argument('--seed', type=lambda value: int(value, 0), help='SDC seed，支持十进制与 0x 十六进制')

    subparsers.add_parser("gui", help="start the PyQt6 GUI")
    for group, operations in (
        ('image', ('export', 'encode')),
        ('archive', ('list', 'unpack', 'pack')),
        ('bm', ('info', 'export', 'encode')),
    ):
        resource_parser = subparsers.add_parser(group, help=f'{group} resource tools')
        commands = resource_parser.add_subparsers(dest='asset_operation', required=True)
        for operation in operations:
            command = commands.add_parser(operation)
            command.add_argument('input', type=Path)
            command.add_argument('-o', '--output', type=Path)
            if group == 'image' and operation == 'encode':
                command.add_argument('--metadata', type=Path, help='PNG sidecar JSON (default: input.png.json)')
                command.add_argument('--format', choices=['original', 'cbg', 'cbg2', 'bgi'], default='original')
                command.add_argument('--quality', type=int, help='CBG V2 质量 1～100；不指定时沿用 JSON 量化表，新图默认 90')
            if group == 'archive' and operation == 'pack':
                command.add_argument('--archive-version', type=int, choices=[1, 2])
                command.add_argument('--import-images', action='store_true', help='apply .bgi-images PNG + JSON to a new archive')
            if group == 'archive' and operation == 'unpack':
                command.add_argument('--images', action='store_true', help='also export PNG and header JSON into .bgi-images')
            if group == 'bm' and operation == 'encode':
                command.add_argument('--fps', type=int, help='图片序列帧率；无 JSON 时默认 30，视频始终跟随原帧率')
                command.add_argument('--bpp', type=int, choices=[24,32])
                command.add_argument('--quality', type=int, help='DCT 质量 1～100；无 JSON 时默认 90')
                command.add_argument('--metadata', type=Path, help='可选 movie.json')
                command.add_argument('--ffmpeg', default='ffmpeg')
                command.add_argument('--force', action='store_true')
                command.add_argument('--limit', type=int, help='图片序列最多编码的帧数；视频始终保留全部帧')
            if group == 'bm' and operation == 'export':
                command.add_argument('--backend', choices=['auto', 'c', 'python'], default='auto')
                command.add_argument('--limit', type=int, help='export only the first N frames')
    return parser


def _split_user_functions(values: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for value in values:
        names.extend(item.strip() for item in re.split(r"[,;\r\n]+", value) if item.strip())
    return tuple(dict.fromkeys(names))


def _make_options(args: argparse.Namespace) -> TaskOptions:
    return TaskOptions(
        asdis=AsdisOptions(
            encoding=args.encoding,
            fallback_encoding=args.fallback_encoding,
            source_encoding=args.source_encoding,
            source_fallback_encoding=args.source_fallback_encoding,
            script_version=args.script_version,
            exact=args.exact,
            f07e_mode=args.f07e_mode,
            bp_engine=args.bp_engine,
            bp_games=args.bp_games,
            bp_executable=args.bp_exe,
        ),
        recursive=not args.no_recursive,
        keep_intermediate=args.keep_intermediate,
        emit_ir=getattr(args, "ir", False),
        include_source_comments=not getattr(args, "no_source_comments", False),
        output_format=getattr(args, "format", "c"),
        bsd_hint=getattr(args, "hint", "auto"),
        user_function_names=_split_user_functions(getattr(args, "user_function", [])),
    )


def _make_request(args: argparse.Namespace) -> TaskRequest:
    if args.command == "inspect":
        return TaskRequest(
            Operation.INSPECT,
            args.target,
            options=TaskOptions(recursive=not args.no_recursive),
        )
    operation = {
        "disasm": Operation.DISASSEMBLE,
        "asm": Operation.ASSEMBLE,
        "decompile": Operation.DECOMPILE,
    }.get(args.command)
    if args.command == "text":
        operation = (
            Operation.TEXT_EXPORT
            if args.text_command == "export"
            else Operation.TEXT_IMPORT
        )
    if operation is None:
        raise ValueError(f"Unsupported command: {args.command}")
    return TaskRequest(
        operation=operation,
        input_path=args.target,
        output_path=args.output,
        translation_path=getattr(args, "translation", None),
        text_format=TextFormat(getattr(args, "format", "json"))
        if args.command == "text"
        else TextFormat.JSON,
        options=_make_options(args),
    )


def _print_result(result) -> None:  # type: ignore[no-untyped-def]
    for path, kind in result.inspected:
        print(f"{kind.value}\t{path}")
    seen: set[tuple[str, Path]] = set()
    for artifact in result.artifacts:
        key = (artifact.kind, artifact.path)
        if key in seen:
            continue
        seen.add(key)
        print(f"{artifact.kind}: {artifact.path}")
    for diagnostic in result.diagnostics:
        prefix = diagnostic.level.upper()
        location = f" [{diagnostic.source_path}]" if diagnostic.source_path else ""
        print(f"{prefix}{location}: {diagnostic.message}", file=sys.stderr)


def _run_gdb(args: argparse.Namespace) -> int:
    try:
        if args.gdb_command == "encode":
            document = _load_json(args.input)
            encoded = encode_gdb(document)
            output = args.output or _default_encode_output(args.input)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(encoded)
            print(f"encoded {args.input} -> {output} ({len(encoded)} bytes)")
            return 0

        source = args.input.read_bytes()
        document = decode_gdb(source)
        if args.gdb_command == "decode":
            output = args.output or _default_decode_output(args.input)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(_pretty_json(document) + "\n", encoding="utf-8", newline="\n")
            print(f"decoded {args.input} -> {output}")
            print(_summary(document, len(source)))
            return 0

        if args.gdb_command == "inspect":
            print(_summary(document, len(source)))
            return 0

        rebuilt = encode_gdb(document)
        if rebuilt != source:
            difference = next(
                (index for index, pair in enumerate(zip(source, rebuilt)) if pair[0] != pair[1]),
                min(len(source), len(rebuilt)),
            )
            raise GdbFormatError(
                "round trip is not byte-identical: "
                f"original={len(source)} bytes rebuilt={len(rebuilt)} bytes "
                f"first_difference=0x{difference:x}"
            )
        print(f"OK: byte-identical round trip ({len(source)} bytes)")
        return 0
    except (GdbFormatError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'bp-engine':
        if args.list:
            for name, profile in PROFILES.items():
                print(f'{name}\tgeneration={profile.generation}\toffset_bits={profile.tag_shift}')
            return 0
        if args.target is None or not args.target.exists():
            parser.error('请指定存在的 BP / BPD 文件或游戏目录，或使用 --list')
        try:
            result = detect_engine(args.target, args.bp_engine, games_path=args.bp_games, executable=args.bp_exe)
        except (OSError, ValueError) as error:
            print(f'error: {error}', file=sys.stderr)
            return 2
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) if args.json else result.describe())
        return 0
    if args.command == "gui":
        try:
            from .gui.app import main as gui_main
        except ImportError as exc:
            parser.error(f"PyQt6 GUI is unavailable: {exc}")
        return gui_main([])

    if args.command == 'audio':
        from .audio_tool import AudioOptions, run_audio_task
        try:
            options = AudioOptions(format=getattr(args,'format','original'), ffmpeg=getattr(args,'ffmpeg','ffmpeg'),
                sample_rate=getattr(args,'sample_rate',None), channels=getattr(args,'channels',None),
                bitrate=getattr(args,'bitrate',192), wav_bits=getattr(args,'wav_bits',16), recursive=not args.no_recursive,
                force=getattr(args,'force',False), metadata=not getattr(args,'no_metadata',False),
                metadata_path=getattr(args,'metadata',None))
            result = run_audio_task(args.input, getattr(args,'output',None), options=options, inspect=args.audio_command=='info')
            return 1 if result.errors or result.cancelled else 0
        except (OSError, ValueError) as error:
            print(f'error: {error}', file=sys.stderr)
            return 2

    if args.command in ('sdc', 'dsc'):
        from .sdc_tool import run_sdc_task
        try:
            result = run_sdc_task(args.sdc_command, args.input, args.output,
                metadata_path=getattr(args,'metadata',None), metadata=not getattr(args,'no_metadata',False),
                seed=getattr(args,'seed',None), force=args.force, recursive=not args.no_recursive,
                output_format=args.command, format_filter=args.command)
            return 1 if result.errors or result.cancelled else 0
        except (OSError, ValueError) as error:
            print(f'error: {error}', file=sys.stderr)
            return 2

    if args.command == 'bsi':
        from .bsi_tool import run_bsi_task
        try:
            result = run_bsi_task(args.bsi_command, args.input, getattr(args, 'output', None),
                force=getattr(args, 'force', False), recursive=not args.no_recursive,
                require_complete=getattr(args, 'require_complete', False))
            return 1 if result.errors or result.cancelled else 0
        except (OSError, ValueError, UnicodeError) as error:
            print(f'error: {error}', file=sys.stderr)
            return 2

    if args.command == "db":
        from .db_text_tool import run_db_task
        try:
            result = run_db_task(args.db_command, args.input, getattr(args, "output", None),
                kind=getattr(args, "kind", None), force=getattr(args, "force", False), recursive=not args.no_recursive)
            return 1 if result.errors or result.cancelled else 0
        except (OSError, ValueError, UnicodeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    if args.command == "gdb":
        return _run_gdb(args)
    if args.command in {'image', 'archive', 'bm'}:
        try:
            from .asset_service import run_asset_task
            options = {'metadata': getattr(args, 'metadata', None),
                       'format': getattr(args, 'format', 'original'),
                       'version': getattr(args, 'archive_version', None),
                       'convert_images': getattr(args, 'images', False),
                       'import_images': getattr(args, 'import_images', False),
                       'backend': getattr(args, 'backend', 'auto'),
                       'limit': getattr(args, 'limit', None),
                       'fps': getattr(args, 'fps', None), 'bpp': getattr(args, 'bpp', None),
                       'quality': getattr(args, 'quality', None), 'ffmpeg': getattr(args, 'ffmpeg', 'ffmpeg'),
                       'force': getattr(args, 'force', False)}
            operation = f'{args.command}-{args.asset_operation}'
            for output in run_asset_task(operation, [args.input], args.output, options=options):
                print(f'output: {output}')
            return 0
        except (OSError, ValueError, ImportError) as error:
            print(f'error: {error}', file=sys.stderr)
            return 2

    request = _make_request(args)
    runner = ToolkitRunner(progress=print)
    result = runner.execute(request)
    _print_result(result)
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
