"""BP2 BSI string tables: ordered UTF-8 blocks of key/value strings."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import struct

from .db_text_tool import output_file
from .i18n import tr


class BsiFormatError(ValueError):
    """Invalid BSI structure or JSON string table."""


def decode(data: bytes) -> dict[str, dict[str, str]]:
    offset = 0

    def u32():
        nonlocal offset
        if offset + 4 > len(data):
            raise BsiFormatError(tr('偏移 {0:#x} 处读 u32 越界', offset))
        value = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        return value

    def cstr():
        nonlocal offset
        end = data.find(b'\0', offset)
        if end < 0:
            raise BsiFormatError(tr('偏移 {0:#x} 起的字符串没有 NUL 结尾', offset))
        try:
            value = data[offset:end].decode('utf-8')
        except UnicodeDecodeError as error:
            raise BsiFormatError(tr('偏移 {0:#x} 的字符串不是合法 UTF-8', offset)) from error
        offset = end + 1
        return value

    block_count = u32()
    if block_count > 0x10000 or block_count > (len(data) - offset) // 5:
        raise BsiFormatError(tr('BSI 块数不合理：{0}', block_count))
    blocks = {}
    for _ in range(block_count):
        name = cstr()
        if name in blocks:
            raise BsiFormatError(tr('BSI 块名重复：{0}', name))
        item_count = u32()
        if item_count > 0x100000 or item_count > (len(data) - offset) // 2:
            raise BsiFormatError(tr('BSI 块 {0} 的条目数不合理：{1}', name, item_count))
        items = {}
        for _ in range(item_count):
            key, value = cstr(), cstr()
            if key in items:
                raise BsiFormatError(tr('BSI 块 {0} 中的 key 重复：{1}', name, key))
            items[key] = value
        blocks[name] = items
    if offset != len(data):
        raise BsiFormatError(tr('BSI 尾部有 {0} 字节多余数据', len(data) - offset))
    return blocks


def build(blocks: dict[str, dict[str, str]], *, require_complete=False) -> bytes:
    if not isinstance(blocks, dict):
        raise BsiFormatError(tr('BSI JSON 顶层必须是对象'))
    if len(blocks) > 0x10000:
        raise BsiFormatError(tr('BSI 块数不合理：{0}', len(blocks)))

    def cstr(value):
        if not isinstance(value, str):
            raise BsiFormatError(tr('BSI 块名、key 和 value 必须是字符串'))
        if '\0' in value:
            raise BsiFormatError(tr('BSI 字符串不能包含 NUL'))
        return value.encode('utf-8') + b'\0'

    chunks = [struct.pack('<I', len(blocks))]
    for name, items in blocks.items():
        chunks.append(cstr(name))
        if not isinstance(items, dict):
            raise BsiFormatError(tr('BSI 块 {0} 必须是对象', name))
        if len(items) > 0x100000:
            raise BsiFormatError(tr('BSI 块 {0} 的条目数不合理：{1}', name, len(items)))
        chunks.append(struct.pack('<I', len(items)))
        for key, value in items.items():
            chunks.append(cstr(key))
            if require_complete and value == '':
                raise BsiFormatError(tr('存在空译文：{0}/{1}', name, key))
            chunks.append(cstr(value))
    return b''.join(chunks)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BsiFormatError(tr('BSI JSON 中的名称或 key 重复：{0}', key))
        result[key] = value
    return result


def default_output(source: Path, operation: str) -> Path | None:
    if operation == 'check':
        return None
    if source.is_dir():
        return source.with_name(source.name + ('_bsi_json' if operation == 'export' else '_bsi_rebuilt'))
    if operation == 'export':
        return source.with_name(source.name + '.json')
    target = source.with_suffix('') if source.suffix.lower() == '.json' else source
    return target if target.suffix.lower() in ('._bsi', '.bsi') else target.with_name(target.name + '._bsi')


@dataclass
class BsiTaskResult:
    outputs: list[Path] = field(default_factory=list)
    succeeded: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def run_bsi_task(operation, source, output=None, *, force=False, recursive=True,
                 require_complete=False, log=print, cancelled=lambda: False) -> BsiTaskResult:
    if operation not in ('export', 'build', 'check'):
        raise BsiFormatError(tr('不支持的 BSI 操作：{0}', operation))
    source = Path(source)
    batch = source.is_dir()
    if source.is_file():
        files = [source]
    elif batch:
        paths = source.rglob('*') if recursive else source.glob('*')
        # Only BSI-named JSON files are selected in batch mode to avoid
        # rebuilding unrelated JSON resources or legacy metadata sidecars.
        files = sorted(path for path in paths if path.is_file()
            and not any(part.startswith('.bgi-') for part in path.relative_to(source).parts)
            and (path.name.lower().endswith(('._bsi.json', '.bsi.json')) if operation == 'build'
                 else path.suffix.lower() in ('._bsi', '.bsi')))
    else:
        raise BsiFormatError(tr('输入文件或目录不存在：{0}', source))
    if not files:
        raise BsiFormatError(tr('输入目录没有找到 BSI 文件（重建时需使用 ._bsi.json 或 .bsi.json）'))
    target = Path(output) if output is not None else default_output(source, operation)
    if batch and target is not None and target.is_file():
        raise BsiFormatError(tr('目录批处理的输出路径必须是目录'))
    result = BsiTaskResult()
    for index, path in enumerate(files, 1):
        if cancelled():
            result.cancelled = True
            break
        try:
            log(f'[{index}/{len(files)}] {path}')
            if operation == 'build':
                blocks = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_object)
                data = build(blocks, require_complete=require_complete)
                if decode(data) != blocks:
                    raise BsiFormatError(tr('JSON 重建后回读不一致，已停止'))
                contents = data
            else:
                data = path.read_bytes()
                blocks = decode(data)
                if build(blocks) != data:
                    raise BsiFormatError(tr('BSI 重建结果与原文件不一致'))
                contents = (json.dumps(blocks, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            log(tr('BSI · {0} 个块 · {1} 条目 · {2} 字节', len(blocks), sum(len(v) for v in blocks.values()), len(data)))
            if operation == 'check':
                log(tr('验证通过：二进制 → JSON → 二进制逐字节一致'))
            else:
                if batch:
                    relative = path.relative_to(source)
                    name = relative.name + '.json' if operation == 'export' else relative.stem
                    destination = target / relative.with_name(name)
                else:
                    destination = target / default_output(path, operation).name if target.is_dir() else target
                if destination.resolve() == path.resolve():
                    raise BsiFormatError(tr('输出路径不能与输入文件相同'))
                output_file(destination, contents, force=force)
                result.outputs.append(destination)
                log(tr('已写入 {0}', destination))
            result.succeeded += 1
        except (OSError, ValueError, UnicodeError) as error:
            message = f'{path}：{error}'
            result.errors.append(message)
            log(tr('失败 · {0}', message))
    log(tr('完成 {0} 个文件 · 失败 {1} 个', result.succeeded, len(result.errors))
        + (tr(' · 已取消') if result.cancelled else ''))
    return result


def add_conversion_arguments(parser, operation):
    parser.add_argument('input', type=Path, help=tr('输入文件或目录'))
    if operation != 'check':
        parser.add_argument('-o', '--output', type=Path)
        parser.add_argument('--force', action='store_true', help=tr('允许覆盖已有输出文件'))
    if operation == 'build':
        parser.add_argument('--require-complete', action='store_true', help=tr('存在空译文时报错'))
    parser.add_argument('--no-recursive', action='store_true', help=tr('目录只处理当前层'))
