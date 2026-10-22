"""PNG conversion with editable, loss-preserving JSON header sidecars."""
from __future__ import annotations
from .i18n import tr

import json
import struct
from pathlib import Path

from PIL import Image

from .resource_metadata import byte_values, wrapper_metadata, wrapper_header
from .native_codec import transform
from .resource_codec import ResourceError, crypt, encode_dsc, unwrap_bse, wrap_bse


def image_kind(data: bytes) -> str | None:
    if data.startswith(b'CompressedBG___'):
        return 'cbg'
    if data.startswith(b'DSC FORMAT 1.00\0'):
        return 'dsc'
    if data.startswith(b'BSE 1.'):
        return 'bse'
    if len(data) >= 16:
        width, height, bpp, flag = struct.unpack_from('<4H', data)
        if 0 < width <= 32767 and 0 < height <= 32767 and bpp in (8, 24, 32) and flag in (0, 1):
            return 'bgi'
    return None


def _raw_pixels(data: bytes, width: int, height: int, bpp: int, flag: int) -> bytes:
    count = width * height * (bpp // 8)
    if len(data) < 16 + count:
        raise ResourceError(tr('BGI 像素数据不完整'))
    raw = data[16:16 + count]
    if not flag:
        return raw
    # Old BGI images store channel-major, differential, serpentine scanlines.
    output = bytearray(count)
    channels = bpp // 8
    pos = 0
    for channel in range(channels):
        value = 0
        for y in range(height):
            for x in (range(width) if not y % 2 else range(width - 1, -1, -1)):
                value = (value + raw[pos]) & 255
                output[(y * width + x) * channels + channel] = value
                pos += 1
    return bytes(output)


def decode_image(data: bytes) -> tuple[Image.Image, dict]:
    wrappers = []
    for _ in range(4):
        kind = image_kind(data)
        if kind == 'bse':
            wrappers.append(wrapper_metadata(data, 'bse'))
            data = unwrap_bse(data)
        elif kind == 'dsc':
            wrappers.append(wrapper_metadata(data, 'dsc'))
            data = transform(2, data)
        else:
            break
    else:
        raise ResourceError(tr('图片封装层数过多'))
    kind = image_kind(data)
    if kind == 'cbg':
        if len(data) < 48:
            raise ResourceError(tr('CBG 文件头不完整'))
        width, height, bpp = *struct.unpack_from('<HH', data, 16), struct.unpack_from('<I', data, 20)[0]
        header = data[:48]
        raw = transform(0, data)
        fields = [24]
        origin_offset = 28
        trailer = b''
    elif kind == 'bgi':
        width, height, bpp, flag = struct.unpack_from('<4H', data)
        header = data[:16]
        raw = _raw_pixels(data, width, height, bpp, flag)
        fields = [8]
        origin_offset = 12
        trailer = data[16 + width * height * (bpp // 8):]
    else:
        raise ResourceError(tr('解压后的资源不是受支持的 BGI 图片'))
    if bpp == 8:
        image = Image.frombytes('L', (width, height), raw).convert('RGBA')
    elif bpp == 16:
        image = Image.frombytes('RGB', (width, height), raw, 'raw', 'BGR;16').convert('RGBA')
    elif bpp == 24:
        image = Image.frombytes('RGB', (width, height), raw, 'raw', 'BGR').convert('RGBA')
    else:
        image = Image.frombytes('RGBA', (width, height), raw, 'raw', 'BGRA')
    meta = {
        'schema': 'bgi-image-metadata', 'schema_version': 2, 'format': kind,
        'width': width, 'height': height, 'bpp': bpp,
        'origin': {'x': struct.unpack_from('<H', header, origin_offset)[0],
                   'y': struct.unpack_from('<H', header, origin_offset + 2)[0],
                   'reference': 'top-left'},
        'header_fields': [{'offset': offset, 'type': 'int32',
            'name': 'reserved_word', 'value': struct.unpack_from('<i', header, offset)[0]}
            for offset in fields],
        'header_note': tr('origin 是相对于图片左上角的基点；origin 与 header_fields 可编辑。未知字段保持原值；长度、校验和等编码字段自动重算。'),
        'wrappers': wrappers, 'trailing_bytes': list(trailer),
    }
    if kind == 'cbg':
        meta['cbg_version'] = struct.unpack_from('<H', header, 46)[0]
        meta['compression'] = dict(intermediate_size=struct.unpack_from('<I',header,32)[0],
            seed=struct.unpack_from('<I',header,36)[0], table_size=struct.unpack_from('<I',header,40)[0],
            checksum_sum=header[44], checksum_xor=header[45])
        if meta['cbg_version'] == 2:
            table = crypt(data[48:48+meta['compression']['table_size']], meta['compression']['seed'])
            meta['quantization'] = {'luminance':list(table[:64]), 'chrominance':list(table[64:128])}
    else:
        meta['differential_encoding'] = bool(struct.unpack_from('<H', header, 6)[0])
    return image, meta


def export_image(source: Path, output: Path) -> tuple[Path, Path]:
    data = source.read_bytes()
    image, meta = decode_image(data)
    meta.update(source_name=source.name)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format='PNG')
    sidecar = Path(str(output) + '.json')
    sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output, sidecar


def encode_image(source: Path, output: Path, *, metadata: Path | None = None, output_format='original', quality=None, log=print) -> Path:
    sidecar = metadata or Path(str(source) + '.json')
    meta = json.loads(sidecar.read_text(encoding='utf-8-sig')) if sidecar.is_file() else None
    if not sidecar.is_file():
        log(tr('提示：未找到图片元信息 JSON：{0}；使用{1}，基点为 (0, 0)，未知头字段置零。', sidecar, tr('无压缩 BGI') if output_format == 'bgi' else 'CBG V1'))
    if meta and (meta.get('schema') != 'bgi-image-metadata' or meta.get('schema_version') not in (1, 2)):
        raise ResourceError(tr('不支持的图片元信息 JSON'))
    if meta and meta.get('schema_version') == 1:
        # Read legacy sidecars without emitting binary dumps in new exports.
        legacy = bytes.fromhex(meta.get('header_hex', ''))
        if legacy:
            if meta['format'] == 'cbg':
                meta.setdefault('compression', {'seed': struct.unpack_from('<I', legacy, 36)[0]})
            else:
                meta.setdefault('differential_encoding', bool(struct.unpack_from('<H',legacy,6)[0]))
        if 'trailer_hex' in meta:
            meta.setdefault('trailing_bytes', list(bytes.fromhex(meta['trailer_hex'])))
        for wrapper in meta.get('wrappers', []):
            if 'header_hex' in wrapper:
                wrapper.update(wrapper_metadata(bytes.fromhex(wrapper['header_hex']), wrapper['format']))
    with Image.open(source) as loaded:
        image = loaded.convert('RGBA')
    kind = meta['format'] if meta and output_format == 'original' else ('bgi' if output_format == 'bgi' else 'cbg')
    bpp = meta['bpp'] if meta else (32 if image.getextrema()[3] != (255, 255) else 24)
    if bpp not in (8, 24, 32):
        raise ResourceError(tr('16 位图片可导出；重新编码请将 JSON 中的 bpp 改为 24 或 32'))
    raw = image.convert('L').tobytes() if bpp == 8 else image.convert('RGB').tobytes('raw', 'BGR') if bpp == 24 else image.tobytes('raw', 'BGRA')
    version = 2 if output_format == 'cbg2' else meta.get('cbg_version', 1) if meta and output_format == 'original' else 1
    header = bytearray(48 if kind == 'cbg' else 16)
    if meta and meta['format'] == kind:
        if kind == 'cbg':
            struct.pack_into('<I', header, 36, meta.get('compression', {}).get('seed', 0))
        else:
            struct.pack_into('<H', header, 6, int(bool(meta.get('differential_encoding', False))))
    if meta and meta['format'] == kind:
        allowed = {24} if kind == 'cbg' else {8}
        for field in meta.get('header_fields', []):
            if field['offset'] not in allowed or field['type'] != 'int32':
                raise ResourceError(tr('元信息字段位置或类型不受支持'))
            struct.pack_into('<i', header, field['offset'], field['value'])
    if meta and 'origin' in meta:
        origin = meta['origin']
        if any(type(origin.get(axis)) is not int or not 0 <= origin[axis] <= 65535 for axis in ('x', 'y')):
            raise ResourceError(tr('图片基点 x、y 必须是 0 到 65535 的整数'))
        struct.pack_into('<HH', header, 28 if kind == 'cbg' else 12, origin['x'], origin['y'])
    width, height = image.size
    if kind == 'cbg':
        if version == 2:
            from .dct_codec import quantization_tables
            header.extend(quantization_tables(meta.get('quantization') if meta else None, quality))
        data = transform(3 if version == 2 else 1, raw, width=width, height=height, bpp=bpp, header=bytes(header))
    else:
        flag = struct.unpack_from('<H', header, 6)[0]
        if flag:
            channels = bpp // 8
            encoded = bytearray()
            for channel in range(channels):
                previous = 0
                for y in range(height):
                    for x in (range(width) if not y % 2 else range(width - 1, -1, -1)):
                        value = raw[(y * width + x) * channels + channel]
                        encoded.append((value - previous) & 255)
                        previous = value
            raw = bytes(encoded)
        struct.pack_into('<4H', header, 0, width, height, bpp, flag)
        data = bytes(header) + raw + (byte_values(meta.get('trailing_bytes', []), tr('图片尾部字节')) if meta else b'')
    if meta and output_format == 'original':
        for wrapper in reversed(meta.get('wrappers', [])):
            if wrapper['format'] == 'dsc':
                data = encode_dsc(data, wrapper_header(wrapper))
            elif wrapper['format'] == 'bse':
                data = wrap_bse(data, wrapper_header(wrapper))
            else:
                raise ResourceError(tr('不支持的图片封装类型'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return output
