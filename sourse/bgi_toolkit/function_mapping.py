from .i18n import tr
#!/usr/bin/env python3
import csv
import json
import re
import os
import tempfile
from dataclasses import dataclass
from collections import Counter
from pathlib import Path


MAPPING_FILE = "bss_mapping.json"
OUTPUT_FILE = "script_functions_mapping.csv"

DIRECT_REGISTRATION_RE = re.compile(
    r"\bglobal_(?:i32_)?([0-9A-Fa-f]{8})\s*=\s*"
    r"(?:\([^()]+\)\s*)*&([A-Za-z_][A-Za-z0-9_]*)"
)
INDEXED_REGISTRATION_RE = re.compile(
    r"\*\s*\(\s*int32_t\s*\*\s*\)\s*\(\s*"
    r"0x([0-9A-Fa-f]+)\s*\+\s*"
    r"\(\s*0x([0-9A-Fa-f]+)\s*<<\s*0x2\s*\)\s*\)\s*"
    r"=\s*&([A-Za-z_][A-Za-z0-9_]*)"
)
NAMED_INDEXED_REGISTRATION_RE = re.compile(
    r"\bglobal_(?:i32|fn)_array_([0-9A-Fa-f]{8})"
    r"\s*\[\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*\]\s*=\s*"
    r"(?:\([^()]+\)\s*)*&([A-Za-z_][A-Za-z0-9_]*)"
)
BULK_REGISTRATION_RE = re.compile(
    r"\bmemcpy\(\s*global_(?:i32|fn)_array_([0-9A-Fa-f]{8})"
    r"(?:\s*\[\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*\])?\s*,\s*"
    r"\(BPDEntryPoint\[\]\)\s*\{(.*?)\}\s*,"
)
LEGACY_COPYSTACK_RE = re.compile(r"\bcopystack\((.*?)\)\s*;")
HANDLER_VALUE_RE = re.compile(
    r"(?:\(BPDEntryPoint\)\s*)?&([A-Za-z_][A-Za-z0-9_]*)"
)
LOOP_BOUND_RE = re.compile(
    r"\bwhile\s*\([^\r\n)]*<\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*\)"
)
HANDLER_TEMP_RE = re.compile(
    r"\s*(?:(?:BPDEntryPoint|intptr_t|int32_t)\s+)?"
    r"(stored_L[0-9]+|v[0-9]+)\s*=\s*(?:\([^()]+\)\s*)*"
    r"&([A-Za-z_][A-Za-z0-9_]*)\s*;"
    r"(?:\s*(?://[^\n]*|/\*.*?\*/))*\s*"
)
HANDLER_TEMP_USE_RE = re.compile(
    r"(?<![=!<>])=\s*(?:\([^()]+\)\s*)*"
    r"(?P<temporary>stored_L[0-9]+|v[0-9]+)\s*;"
    r"(?:\s*(?://[^\n]*|/\*.*?\*/))*\s*$"
)


def parse_number(text):
    return int(text, 0)


def split_c_arguments(text):
    arguments = []
    start = 0
    depth = 0
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            arguments.append(text[start:index].strip())
            start = index + 1
        index += 1
    arguments.append(text[start:].strip())
    return arguments


def default_mapping_path():
    return Path(__file__).resolve().parent / 'asdis' / 'resources' / MAPPING_FILE


def load_mapping(script_directory=None):
    path = Path(script_directory) if script_directory is not None else default_mapping_path()
    if path.is_dir():
        path = path / MAPPING_FILE
    if not path.is_file():
        raise RuntimeError(f"Mapping file not found: {path}")

    with path.open("r", encoding="utf-8-sig") as stream:
        raw_mapping = json.load(stream)

    if not isinstance(raw_mapping, dict):
        raise RuntimeError(f'Mapping JSON must be an object: {path}')
    mapping = {}
    for key, value in raw_mapping.items():
        match = re.fullmatch(r"f_([0-9A-Fa-f]+)", key)
        if match:
            mapping[int(match.group(1), 16)] = str(value)

    if not mapping:
        raise RuntimeError(f"No f_HEX entries found in {path}")
    return mapping


class MappingCancelled(RuntimeError):
    pass


def check_cancelled(cancelled):
    if cancelled():
        raise MappingCancelled(tr('已取消函数映射生成'))


def load_sources(directory, *, log=lambda _: None, cancelled=lambda: False):
    directory = Path(directory)
    root = directory if directory.is_dir() else directory.parent
    paths = ([directory] if directory.is_file() and directory.suffix.lower() == '.c' else
             sorted((path for path in directory.rglob('*') if path.is_file() and path.suffix.lower() == '.c'),
                    key=lambda path: path.relative_to(root).as_posix().lower()))
    if not paths:
        raise RuntimeError(tr('没有找到 C 文件：{0}', directory))
    sources = []
    for index, path in enumerate(paths, 1):
        check_cancelled(cancelled)
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        relative = path.relative_to(root).as_posix()
        sources.append((relative, text.splitlines()))
        log(tr('读取 C 文件 [{0}/{1}]：{2}', index, len(paths), relative))
    return sources


def _registration_lines(lines):
    """Resolve an immediately preceding handler spill, without crossing statements.

    The emitter can write ``stored_L13 = &handler; global = stored_L13;``
    on separate lines. Retain line numbering and require a simple RHS; branches,
    reassignments, function boundaries, and other statements invalidate the spill.
    """
    pending = None
    for original in lines:
        stripped = original.strip()
        if not stripped or stripped.startswith('//') or (stripped.startswith('/*') and stripped.endswith('*/')):
            yield original
            continue
        line = original
        if pending and stripped.startswith(('global_', '*')):
            use = HANDLER_TEMP_USE_RE.search(line)
            if use and use.group('temporary') == pending[0]:
                start, end = use.span('temporary')
                line = line[:start] + '&' + pending[1] + line[end:]
        definition = HANDLER_TEMP_RE.fullmatch(original)
        pending = definition.groups() if definition else None
        yield line


def collect_registrations(sources, *, cancelled=lambda: False):
    direct = []
    indexed = []
    for relative_path, lines in sources:
        for line_number, line in enumerate(_registration_lines(lines), 1):
            if line_number % 256 == 1:
                check_cancelled(cancelled)
            for direct_match in DIRECT_REGISTRATION_RE.finditer(line):
                direct.append(
                    (
                        int(direct_match.group(1), 16),
                        direct_match.group(2),
                        relative_path,
                        line_number,
                    )
                )

            for indexed_match in INDEXED_REGISTRATION_RE.finditer(line):
                indexed.append(
                    (
                        int(indexed_match.group(1), 16),
                        int(indexed_match.group(2), 16),
                        indexed_match.group(3),
                        relative_path,
                        line_number,
                    )
                )

            for indexed_match in NAMED_INDEXED_REGISTRATION_RE.finditer(line):
                indexed.append(
                    (
                        int(indexed_match.group(1), 16),
                        parse_number(indexed_match.group(2)),
                        indexed_match.group(3),
                        relative_path,
                        line_number,
                    )
                )

            for bulk_match in BULK_REGISTRATION_RE.finditer(line):
                base = int(bulk_match.group(1), 16)
                start = parse_number(bulk_match.group(2) or "0")
                for offset, value in enumerate(split_c_arguments(bulk_match.group(3))):
                    handler = HANDLER_VALUE_RE.fullmatch(value)
                    if handler is not None:
                        direct.append(
                            (
                                base + (start + offset) * 4,
                                handler.group(1),
                                relative_path,
                                line_number,
                            )
                        )

            # Backward compatibility lets the mapper consume output produced
            # before copystack was lowered to memcpy/typed assignments.
            for copy_match in LEGACY_COPYSTACK_RE.finditer(line):
                arguments = split_c_arguments(copy_match.group(1))
                if len(arguments) < 4:
                    continue
                try:
                    base = parse_number(arguments[0])
                    width = parse_number(arguments[-2]) & 3
                    count = parse_number(arguments[-1])
                except ValueError:
                    continue
                values = arguments[1:-2]
                if width != 2 or count != len(values):
                    continue
                for offset, value in enumerate(values):
                    handler = HANDLER_VALUE_RE.fullmatch(value)
                    if handler is not None:
                        direct.append(
                            (
                                base + offset * 4,
                                handler.group(1),
                                relative_path,
                                line_number,
                            )
                        )
    return direct, indexed


def detect_dispatch_base(mapping, direct, indexed):
    if not direct and not indexed:
        raise RuntimeError("No opcode handler registrations found")

    scores = Counter()
    opcodes = set(mapping)
    for address, _handler, _file, _line in direct:
        for opcode in opcodes:
            candidate = address - opcode * 4
            # BP global memory is byte-addressed.  Entries have a 4-byte
            # stride, but the table itself need not be 4-byte aligned.
            if candidate >= 0:
                scores[candidate] += 1

    # Named array writes also carry an explicit table base and opcode.  Fold
    # them into the same mapping-aware score instead of selecting the most
    # frequently written array outright: scripts often initialize a small,
    # unrelated callback table with more constant-index writes than the real
    # opcode table, whose bulk default fill uses a dynamic loop index.
    for base, opcode, _handler, _file, _line in indexed:
        if opcode in opcodes:
            scores[base] += 1

    if not scores:
        raise RuntimeError("Could not infer the opcode dispatch table base")

    base, score = scores.most_common(1)[0]
    if score < 3:
        raise RuntimeError("Opcode dispatch table base confidence is too low")
    return base


def detect_dispatch_slots(base, sources, mapping, direct, indexed):
    base_re = re.compile(
        rf"\b(?:0x0*{base:x}|global_(?:i32|fn)_array_0*{base:x})\b",
        re.IGNORECASE,
    )
    candidates = Counter()

    for _relative_path, lines in sources:
        for index, line in enumerate(lines):
            if not base_re.search(line):
                continue

            context = lines[max(0, index - 2) : min(len(lines), index + 9)]
            if not any("&bpd_" in context_line for context_line in context):
                continue

            for context_line in context:
                match = LOOP_BOUND_RE.search(context_line)
                if match:
                    value = parse_number(match.group(1))
                    if 256 <= value <= 65536:
                        candidates[value] += 1

    if candidates:
        return candidates.most_common(1)[0][0]

    mapped_opcodes = set(mapping)
    high_opcode_found = any(
        (address - base) % 4 == 0
        and (address - base) // 4 in mapped_opcodes
        and (address - base) // 4 >= 1024
        for address, _handler, _file, _line in direct
        if address >= base
    ) or any(
        indexed_base == base and opcode in mapped_opcodes and opcode >= 1024
        for indexed_base, opcode, _handler, _file, _line in indexed
    )
    return 1280 if high_opcode_found else 1024


def build_runtime_map(base, slots, direct, indexed):
    runtime = {}
    limit = base + slots * 4

    for address, handler, source_file, source_line in direct:
        if base <= address < limit and (address - base) % 4 == 0:
            opcode = (address - base) // 4
            runtime[opcode] = (handler, source_file, source_line)

    for indexed_base, opcode, handler, source_file, source_line in indexed:
        if indexed_base == base and 0 <= opcode < slots:
            runtime[opcode] = (handler, source_file, source_line)

    if not runtime:
        raise RuntimeError("No registrations fall inside the detected dispatch table")
    return runtime


def write_mapping(mapping, runtime, output_path=None):
    output_path = Path(output_path) if output_path is not None else Path.cwd() / OUTPUT_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_opcodes = sorted(set(mapping) | set(runtime))

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                dir=output_path.parent, prefix=".function-mapping-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(
                [
                    "Opcode",
                    "OpcodeHex",
                    "FunctionName",
                    "Status",
                    "Handler",
                    "SourceFile",
                    "SourceLine",
                ]
            )
            for opcode in all_opcodes:
                has_mapping = opcode in mapping
                has_runtime = opcode in runtime
                if has_mapping and has_runtime:
                    status = "mapped-runtime"
                elif has_mapping:
                    status = "mapped-only"
                else:
                    status = "runtime-unmapped"
    
                handler, source_file, source_line = runtime.get(opcode, ("", "", ""))
                writer.writerow(
                    [
                        f"f_{opcode:03x}",
                        f"0x{opcode:03X}",
                        mapping.get(opcode, ""),
                        status,
                        handler,
                        source_file,
                        source_line,
                    ]
                )
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output_path



@dataclass(frozen=True)
class MappingResult:
    output_path: Path
    dispatch_base: int
    slots: int
    source_count: int
    mapped_runtime: int
    runtime_unmapped: int
    mapped_only: int


def generate_function_mapping(target, output_path=None, mapping_path=None, *, log=print, cancelled=lambda: False, mapping=None):
    target = Path(target).resolve()
    if not target.is_dir() and not (target.is_file() and target.suffix.lower() == '.c'):
        raise RuntimeError(tr('请选择已反编译的 C 文件或目录：{0}', target))
    check_cancelled(cancelled)
    mapping = load_mapping(mapping_path) if mapping is None else mapping
    root = target if target.is_dir() else target.parent
    output = Path(output_path).resolve() if output_path else root / OUTPUT_FILE
    sources = load_sources(target, log=log, cancelled=cancelled)
    protected = {(root / relative).resolve() for relative, _ in sources}
    protected.add(Path(mapping_path).resolve() if mapping_path else default_mapping_path())
    if output in protected:
        raise RuntimeError(tr('CSV 输出不能覆盖输入 C 文件或映射 JSON'))
    direct, indexed = collect_registrations(sources, cancelled=cancelled)
    log(tr('发现注册：直接赋值 {0} 条，索引赋值 {1} 条', len(direct), len(indexed)))
    check_cancelled(cancelled)
    base = detect_dispatch_base(mapping, direct, indexed)
    slots = detect_dispatch_slots(base, sources, mapping, direct, indexed)
    runtime = build_runtime_map(base, slots, direct, indexed)
    check_cancelled(cancelled)
    written = write_mapping(mapping, runtime, output)
    result = MappingResult(written, base, slots, len(sources),
        len(set(mapping) & set(runtime)), len(set(runtime) - set(mapping)), len(set(mapping) - set(runtime)))
    log(tr('分发表基址：0x{0:X}，槽位数：{1}', base, slots))
    log(tr('已匹配运行时：{0}；运行时未命名：{1}；仅映射表中存在：{2}', result.mapped_runtime, result.runtime_unmapped, result.mapped_only))
    log(tr('CSV 输出：{0}', written))
    return result
