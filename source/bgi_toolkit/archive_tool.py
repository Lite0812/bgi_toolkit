"""Streaming PackFile/ARC20 extraction and packing, retaining index metadata."""
from __future__ import annotations
from .i18n import tr

import json
import os
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .resource_metadata import byte_values, wrapper_metadata, wrapper_header
from .resource_codec import ResourceError, decode_dsc, encode_dsc, unwrap_bse, wrap_bse

MANIFEST = '.bgi-archive.json'
PACKED_DATA = '.bgi-archive-data'


@dataclass(frozen=True)
class ArcEntry:
    name: str
    offset: int
    size: int
    index_metadata: dict


def safe_path(name: str) -> Path:
    path = PureWindowsPath(name)
    if path.is_absolute() or path.drive or any(part in ('..', '') or ':' in part for part in path.parts):
        raise ResourceError(tr('无效的封包路径：{0}', name))
    if not path.parts:
        raise ResourceError(tr('封包文件名为空'))
    return Path(*path.parts)


def read_archive(source: Path) -> tuple[int, list[ArcEntry]]:
    size = source.stat().st_size
    with source.open('rb') as stream:
        header = stream.read(16)
        if len(header) != 16:
            raise ResourceError(tr('封包头不完整'))
        version = 2 if header[:12] == b'BURIKO ARC20' else 1 if header[:12] == b'PackFile    ' else 0
        if not version:
            raise ResourceError(tr('不是 PackFile / BURIKO ARC20 封包'))
        count = struct.unpack_from('<I', header, 12)[0]
        entry_size, name_size = (128, 96) if version == 2 else (32, 16)
        base = 16 + count * entry_size
        if base > size:
            raise ResourceError(tr('封包索引不完整'))
        entries = []
        for _ in range(count):
            record = stream.read(entry_size)
            name = record[:name_size].split(b'\0', 1)[0].decode('cp932')
            safe_path(name)
            offset, length = struct.unpack_from('<II', record, name_size)
            if base + offset + length > size:
                raise ResourceError(tr('封包条目越界：{0}', name))
            encoded_name = name.encode('cp932')
            index_metadata = dict(filename_padding=list(record[len(encoded_name):name_size]),
                reserved_words=list(struct.unpack_from(f'<{(entry_size-name_size-8)//4}I', record, name_size+8)))
            entries.append(ArcEntry(name, base + offset, length, index_metadata))
    return version, entries


def _path_key(path: Path) -> str:
    return '/'.join(part.rstrip(' .').casefold() for part in path.parts)


def _extraction_paths(entries):
    originals = [safe_path(entry.name) for entry in entries]
    reserved = {_path_key(path) for path in originals}
    directories = {_path_key(parent) for path in originals for parent in path.parents if parent != Path('.')}
    used = {MANIFEST.casefold(), PACKED_DATA.casefold(), '.bgi-images'}
    result = []
    for original in originals:
        path = original
        if path.parts[0].casefold() in used & {MANIFEST.casefold(), PACKED_DATA.casefold(), '.bgi-images'}:
            path = Path(path.parts[0] + '.__resource', *path.parts[1:])
        candidate, number = path, 2
        while (_path_key(candidate) in used or _path_key(candidate) in directories
               or (candidate != original and _path_key(candidate) in reserved)):
            candidate = path.with_name(f'{path.stem}__{number}{path.suffix}')
            number += 1
        used.add(_path_key(candidate))
        result.append(candidate)
    return result


def _checked_path(root: Path, relative: str | Path) -> Path:
    path = root / safe_path(str(relative))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ResourceError(tr('封包路径超出目录：{0}', relative))
    return path


def _dsc_decoder():
    # Use the bundled DLL on Windows; the Python codec keeps CLI extraction portable.
    from .native_codec import library, transform
    try:
        library()
    except ResourceError:
        return decode_dsc
    return lambda data: transform(2, data)


def extract_archive(source: Path, output: Path, *, log=lambda message: None, cancelled=lambda: False) -> list[Path]:
    version, entries = read_archive(source)
    if output.resolve() == source.parent.resolve():
        raise ResourceError(tr('解包输出请选择独立目录'))
    output.mkdir(parents=True, exist_ok=True)
    paths = _extraction_paths(entries)
    files, records, dsc = [], [], None
    with source.open('rb') as stream:
        for index, (entry, relative) in enumerate(zip(entries, paths), 1):
            if cancelled():
                raise ResourceError(tr('任务已取消'))
            path = _checked_path(output, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {'name': entry.name, 'index_metadata': entry.index_metadata}
            if relative != safe_path(entry.name):
                record['path'] = relative.as_posix()
                log(tr('同名或保留路径：{0} → {1}', entry.name, relative.as_posix()))
            stream.seek(entry.offset)
            prefix = stream.read(min(entry.size, 80))
            wrapper = None
            # Match GARbro ArcOpener/Arc2Opener.OpenEntry: one container layer only.
            if entry.size > 0x220 and prefix.startswith(b'DSC FORMAT 1.00\0'):
                wrapper = 'dsc'
            elif (version == 2 and entry.size >= 0x50 and prefix.startswith(b'BSE 1.')
                  and struct.unpack_from('<H', prefix, 8)[0] in (0x100, 0x101)):
                wrapper = 'bse'
            decoded = None
            if wrapper:
                stream.seek(entry.offset)
                packed = stream.read(entry.size)
                try:
                    if wrapper == 'dsc':
                        dsc = dsc or _dsc_decoder()
                        decoded = dsc(packed)
                    else:
                        decoded = unwrap_bse(packed)
                except (ResourceError, ValueError) as error:
                    # GARbro returns original bytes when DSC decompression fails.
                    log(tr('解压失败，保留原始资源 · {0}：{1}', entry.name, error))
                    record['decode_error'] = str(error)
                else:
                    record.update(wrapper=wrapper, wrapper_metadata=wrapper_metadata(packed, wrapper))
            if decoded is not None:
                path.write_bytes(decoded)
            else:
                stream.seek(entry.offset)
                remaining = entry.size
                with path.open('wb') as target:
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ResourceError(tr('封包数据提前结束'))
                        target.write(chunk)
                        remaining -= len(chunk)
            files.append(path)
            records.append(record)
            log(f'[{index}/{len(entries)}] {relative.as_posix()}' + (tr(' · {0} 已展开', wrapper.upper()) if decoded is not None else ''))
    manifest = {'schema': 'bgi-archive', 'version': version, 'source_name': source.name, 'entries': records}
    _checked_path(output, MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return files


def pack_archive(source: Path, output: Path, *, version: int | None = None,
                 log=lambda message: None, cancelled=lambda: False, import_images=False) -> Path:
    if not source.is_dir():
        raise ResourceError(tr('封包输入必须是目录'))
    if output.resolve().is_relative_to(source.resolve()):
        raise ResourceError(tr('封包输出必须位于输入目录之外'))
    path = _checked_path(source, MANIFEST)
    manifest = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    if not path.is_file():
        log(tr('提示：未找到封包清单 JSON；按当前目录文件名与排序创建封包，默认使用 ARC20，索引保留字段置零。'))
    version = version or manifest.get('version', 2)
    if version not in (1, 2):
        raise ResourceError(tr('封包版本必须是 1 或 2'))
    records = []
    indexed = set()
    for entry in manifest.get('entries', []):
        relative = entry.get('path', entry['name'])
        if _checked_path(source, relative).is_file():
            records.append(entry)
            indexed.add(_path_key(safe_path(relative)))
    for file in sorted(source.rglob('*')):
        relative = file.relative_to(source)
        if (file.is_file() and not relative.parts[0].startswith('.bgi-')
                and not file.name.endswith('.png.json') and _path_key(relative) not in indexed):
            records.append({'name': relative.as_posix()})
    entry_size, name_size = (128, 96) if version == 2 else (32, 16)
    with tempfile.TemporaryDirectory(prefix='bgi-archive-') as folder:
        scratch = Path(folder)
        overrides = {}
        image_root = source / '.bgi-images'
        if import_images and image_root.is_dir():
            from .image_tool import encode_image
            lookup = {safe_path(row.get('path', row['name'])) for row in records}
            for png in sorted(image_root.rglob('*.png')):
                if cancelled():
                    raise ResourceError(tr('任务已取消'))
                relative = png.relative_to(image_root).with_suffix('')
                if relative not in lookup:
                    raise ResourceError(tr('PNG 没有对应的原始封包条目：{0}', png))
                target = scratch / 'images' / relative
                encode_image(png, target, log=log)
                overrides[relative] = target
                log(tr('图片回写：{0}', relative))
        items = []
        for i, row in enumerate(records):
            if cancelled():
                raise ResourceError(tr('任务已取消'))
            relative = safe_path(row.get('path', row['name']))
            file = overrides.get(relative, _checked_path(source, relative))
            wrapper = row.get('wrapper')
            if wrapper:
                data = file.read_bytes()
                metadata = row.get('wrapper_metadata')
                if metadata is None:
                    log(tr('提示：{0} 缺少封装元信息；使用默认的 {1} 头重新编码。', relative, wrapper.upper()))
                    metadata = {'format': wrapper}
                header = wrapper_header(metadata)
                file = scratch / f'{i:06d}.{wrapper}'
                if wrapper == 'dsc':
                    file.write_bytes(encode_dsc(data, header))
                elif wrapper == 'bse':
                    file.write_bytes(wrap_bse(data, header))
                else:
                    raise ResourceError(tr('未知的封装类型：{0}', wrapper))
            items.append((row['name'], file, row.get('index_metadata', {})))
        return _pack_files(output, items, entry_size, name_size, version, log, cancelled)


def _pack_files(output, items, entry_size, name_size, version, log, cancelled):
    index = bytearray()
    offset = 0
    for name, file, metadata in items:
        encoded = name.encode('cp932')
        if len(encoded) > name_size:
            raise ResourceError(tr('封包文件名过长：{0}', name))
        record = bytearray(entry_size)
        record[:len(encoded)] = encoded
        padding = byte_values(metadata.get('filename_padding', []), tr('文件名填充'))
        if len(padding) == name_size-len(encoded):
            record[len(encoded):name_size] = padding
        words = metadata.get('reserved_words', [])
        if len(words) == (entry_size-name_size-8)//4:
            struct.pack_into(f'<{len(words)}I', record, name_size+8, *words)
        length = file.stat().st_size
        if offset + length > 0xffffffff:
            raise ResourceError(tr('封包超出 32 位偏移范围'))
        struct.pack_into('<II', record, name_size, offset, length)
        index.extend(record)
        offset += length
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=output.name + '.', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(handle, 'wb') as target:
            target.write((b'BURIKO ARC20' if version == 2 else b'PackFile    ') + struct.pack('<I', len(items)))
            target.write(index)
            for i, (name, file, _) in enumerate(items, 1):
                if cancelled():
                    raise ResourceError(tr('任务已取消'))
                with file.open('rb') as item:
                    shutil.copyfileobj(item, target, 1024 * 1024)
                log(f'[{i}/{len(items)}] {name}')
        os.replace(temporary, output)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return output
