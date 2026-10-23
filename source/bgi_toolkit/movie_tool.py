"""Headless BF_Movie information and sequential PNG export using the fixed codec."""
from __future__ import annotations
from .i18n import tr

import json
import struct
from pathlib import Path

from .resource_codec import ResourceError


def movie_info(source: Path) -> dict:
    with source.open('rb') as stream:
        header = stream.read(192)
        if len(header) != 192 or not header.startswith(b'BF_Movie'):
            raise ResourceError(tr('不是完整的 BF_Movie 文件'))
        width, height, bpp = struct.unpack_from('<III', header, 20)
        fps, declared_count = struct.unpack_from('<II', header, 36)
        first = stream.read(4)
        if len(first) != 4:
            raise ResourceError(tr('BM 帧偏移表为空'))
        first_offset = struct.unpack('<I', first)[0]
        count, remainder = divmod(first_offset - 192, 4)
        if remainder or count <= 0 or first_offset > source.stat().st_size:
            raise ResourceError(tr('BM 帧偏移表无效'))
        stream.seek(192)
        raw = stream.read(count * 4)
        offsets = list(struct.unpack(f'<{count}I', raw))
    if (not width or not height or width * height > 134217728 or bpp not in (24, 32) or
        any(a >= b for a, b in zip(offsets, offsets[1:])) or offsets[-1] >= source.stat().st_size):
        raise ResourceError(tr('BM 图像尺寸、位深或帧偏移无效'))
    return {'schema': 'bgi-movie-metadata', 'schema_version': 1, 'source_name': source.name,
        'width': width, 'height': height, 'bpp': bpp, 'fps': fps or 30,
        'format_version': struct.unpack_from('<I',header,16)[0],
        'surface_format': struct.unpack_from('<I',header,32)[0],
        'frame_count': count, 'declared_frame_count': declared_count,
        'reserved_header_fields': [{'offset':offset, 'value':struct.unpack_from('<I',header,offset)[0]}
            for offset in range(44,64,4)],
        'quantization_table': list(header[64:192]),
        'frame_offsets': offsets}


def export_movie(source: Path, output: Path, *, backend='auto', limit: int | None = None,
                 log=lambda message: None, cancelled=lambda: False) -> Path:
    from PIL import Image
    from . import bm_codec as codec
    info = movie_info(source)
    width, height, bpp = info['width'], info['height'], info['bpp']
    quant = bytes(info['quantization_table'])
    native = backend != 'python' and codec._load_decoder_lib() is not None
    if backend == 'c' and not native:
        raise ResourceError(tr('BM C 解码库不可用'))
    decoder = None if native else codec.ParallelCbgDecoder(width, height, bpp, quant)
    output.mkdir(parents=True, exist_ok=True)
    previous = None
    count = min(limit, info['frame_count']) if limit is not None else info['frame_count']
    if count <= 0:
        raise ResourceError(tr('导出帧数必须大于 0'))
    with source.open('rb') as stream:
        for index, _, _, data in codec.iter_frame_bytes(stream, info['frame_offsets'], source.stat().st_size, count):
            if cancelled():
                raise ResourceError(tr('任务已取消'))
            if native:
                previous = codec.decode_frame_c(data, width, height, bpp, quant, previous)
                image = Image.frombytes('RGBA', (width, height), previous)
            else:
                codec.decode_frame(decoder, data, width, height)
                image = codec.build_image(decoder, width, height)
            if bpp == 24:
                image.putalpha(255)
            image.save(output / f'frame_{index:04d}.png')
            log(f'[{index + 1}/{count}] {source.name}')
    info['exported_frames'] = count
    info['decoder'] = 'c' if native else 'python'
    (output / 'movie.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output
