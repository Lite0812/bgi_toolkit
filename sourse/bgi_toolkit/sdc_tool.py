"""Standalone SDC / DSC decompression and recompression with sidecar metadata."""
from __future__ import annotations
from .i18n import tr

from dataclasses import dataclass, field
import json
from pathlib import Path
import struct

from .db_text_tool import output_file
from .gdb_tool import MAX_UNPACKED_SIZE, SDC_MAGIC, _decode_sdc, _encode_sdc
from .resource_codec import ResourceError, decode_dsc_stream, encode_dsc
from .resource_metadata import byte_values


@dataclass
class SdcResult:
    outputs: list[Path] = field(default_factory=list)
    succeeded: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


DSC_MAGIC = b'DSC FORMAT 1.00\0'


def default_sdc_output(source: Path, operation: str, output_format='original', *, metadata_path=None) -> Path:
    kind = output_format
    if operation == 'compress' and kind == 'original':
        sidecar = Path(metadata_path) if metadata_path else Path(str(source) + '.json')
        try:
            kind = _metadata(sidecar).get('format', 'sdc') if sidecar.is_file() else 'sdc'
        except (OSError, ValueError):
            kind = 'sdc'  # Naming is a hint; processing reports invalid metadata.
    if source.is_dir():
        return source.with_name(source.name + ('_' + kind if operation == 'compress' else '_' + (kind if kind in ('sdc', 'dsc') else 'sdc') + '_unpacked'))
    return source.with_name(source.name + ('.' + kind if operation == 'compress' else '.unpacked'))


def _metadata(path):
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(document, dict) or document.get('schema') not in ('bgi-sdc-metadata', 'bgi-dsc-metadata') or document.get('version') != 1:
        raise ResourceError(tr('不是支持的 SDC 元信息 JSON：{0}', path))
    document.setdefault('format', 'dsc' if document['schema'] == 'bgi-dsc-metadata' else 'sdc')
    if document.get('format', 'sdc') not in ('sdc', 'dsc'):
        raise ResourceError(tr('元信息中的压缩格式无效'))
    if 'trailing_data_hex' in document and 'trailing_bytes' not in document:
        document['trailing_bytes'] = list(bytes.fromhex(document['trailing_data_hex']))
    return document


def run_sdc_task(operation, source, output=None, *, metadata_path=None, metadata=True,
                 seed=None, force=False, recursive=True, output_format='original', format_filter=None, log=print, cancelled=lambda:False):
    if operation not in ('decompress', 'compress'):
        raise ResourceError(tr('未知的 SDC 操作'))
    source = Path(source)
    if not source.exists():
        raise ResourceError(tr('输入不存在：{0}', source))
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xffffffff):
        raise ResourceError(tr('SDC seed 必须是 0 到 4294967295 的整数'))
    if output_format not in ('original', 'sdc', 'dsc'):
        raise ResourceError(tr('压缩格式必须为 original、sdc 或 dsc'))
    if format_filter not in (None, 'sdc', 'dsc'):
        raise ResourceError(tr('压缩格式筛选无效'))
    batch = source.is_dir()
    if batch and metadata_path:
        raise ResourceError(tr('指定元信息 JSON 时请选择单个文件；目录压缩会读取每个文件的同名 JSON'))
    target = Path(output) if output else default_sdc_output(source, operation,
        format_filter if operation == 'decompress' and format_filter else output_format, metadata_path=metadata_path)
    if batch and target.resolve() == source.resolve():
        raise ResourceError(tr('请为目录批处理选择独立的输出目录'))
    files = [source]
    if batch:
        files = []
        iterator = source.rglob('*') if recursive else source.glob('*')
        for path in sorted(iterator):
            if (not path.is_file() or any(part.startswith('.bgi-') for part in path.relative_to(source).parts)
                    or path.resolve().is_relative_to(target.resolve()) or path.suffix.lower() == '.json'):
                continue
            with path.open('rb') as stream:
                magic = stream.read(16)
                kind = 'sdc' if magic == SDC_MAGIC else 'dsc' if magic == DSC_MAGIC else None
                packed = kind is not None
            if operation == 'decompress' and packed and (format_filter is None or kind == format_filter):
                files.append(path)
            elif operation == 'compress' and not packed:
                files.append(path)
    if not files:
        raise ResourceError(tr('没有找到可处理的 {0} 文件', format_filter.upper() if format_filter else 'SDC / DSC') if operation == 'decompress' else tr('没有找到可压缩的文件'))
    inputs = {p.resolve() for p in files}
    result = SdcResult()
    for index, path in enumerate(files, 1):
        if cancelled():
            result.cancelled = True
            break
        try:
            destination = target / path.relative_to(source) if batch else target / default_sdc_output(path, operation, output_format, metadata_path=metadata_path).name if target.is_dir() else target
            if destination.resolve() in inputs:
                raise ResourceError(tr('输出不能覆盖本次输入文件'))
            sidecar = Path(str(destination) + '.json')
            if not force and (destination.exists() or (operation == 'decompress' and metadata and sidecar.exists())):
                raise ResourceError(tr('输出已存在：{0}；勾选允许覆盖或使用 --force', destination))
            log(f'[{index}/{len(files)}] {path}')
            if operation == 'decompress':
                original = path.read_bytes()
                detected = 'dsc' if original.startswith(DSC_MAGIC) else 'sdc' if original.startswith(SDC_MAGIC) else None
                if detected and format_filter and detected != format_filter:
                    raise ResourceError(tr('这是 {0} 文件，请使用 {0} 工具。', detected.upper()))
                if original.startswith(DSC_MAGIC):
                    payload, end = decode_dsc_stream(original)
                    original_seed = struct.unpack_from('<I', original, 16)[0]
                    trailing, kind = original[end:], 'dsc'
                elif original.startswith(SDC_MAGIC):
                    payload, original_seed, trailing = _decode_sdc(original)
                    kind = 'sdc'
                else:
                    raise ResourceError(tr('无法识别压缩格式；支持 SDC FORMAT 1.00 和 DSC FORMAT 1.00'))
                log(tr('检测到 {0} FORMAT 1.00', kind.upper()))
                if cancelled():
                    result.cancelled = True
                    break
                if metadata:
                    document = dict(schema='bgi-' + kind + '-metadata', version=1, format=kind, source_name=path.name,
                        seed=original_seed, packed_size=len(original)-32-len(trailing),
                        unpacked_size=len(payload), trailing_bytes=list(trailing))
                    if kind == 'dsc':
                        document['header_bytes'] = list(original[:32])
                    output_file(sidecar, (json.dumps(document,ensure_ascii=False,indent=2)+'\n').encode('utf-8'), force=force)
                output_file(destination, payload, force=force)
            else:
                if path.stat().st_size > MAX_UNPACKED_SIZE:
                    raise ResourceError(tr('SDC 输入超过 512 MiB 上限'))
                payload = path.read_bytes()
                json_path = Path(metadata_path) if metadata_path else Path(str(path) + '.json')
                if not json_path.is_file():
                    log(tr('提示：未找到封装信息 JSON：{0}；未指定 seed 时使用 0，不添加尾部数据。', json_path))
                document = _metadata(json_path) if json_path.is_file() else {}
                original_seed = seed if seed is not None else document.get('seed', 0)
                if type(original_seed) is not int or not 0 <= original_seed <= 0xffffffff:
                    raise ResourceError(tr('元信息中的 SDC seed 无效'))
                trailing = byte_values(document.get('trailing_bytes', []), tr('SDC 尾部字节'))
                kind = document.get('format', 'sdc') if output_format == 'original' else output_format
                if kind == 'dsc':
                    header = bytearray(byte_values(document['header_bytes'], tr('DSC 原始头'))
                        if document.get('format') == 'dsc' and 'header_bytes' in document else DSC_MAGIC + bytes(16))
                    if len(header) != 32 or not header.startswith(DSC_MAGIC):
                        raise ResourceError(tr('DSC 原始头必须为 32 字节并包含正确的格式标记'))
                    struct.pack_into('<I', header, 16, original_seed)
                    packed = encode_dsc(payload, bytes(header)) + trailing
                else:
                    packed = _encode_sdc(payload, original_seed, trailing)
                if cancelled():
                    result.cancelled = True
                    break
                output_file(destination, packed, force=force)
            result.outputs.append(destination)
            result.succeeded += 1
            log(tr('完成：{0}', destination))
        except (ValueError, OSError) as error:
            result.errors.append(f'{path}：{error}')
            log(tr('失败 · {0}：{1}', path, error))
    log(tr('完成 {0} 个 · 失败 {1} 个', result.succeeded, len(result.errors)) + (tr(' · 已取消') if result.cancelled else ''))
    return result
