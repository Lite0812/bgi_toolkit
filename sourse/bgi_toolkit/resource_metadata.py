"""Named resource header fields; JSON stores numbers rather than binary dumps."""
from __future__ import annotations
from .i18n import tr
import struct
from .resource_codec import ResourceError


def byte_values(value, label=tr('保留字节')):
    if not isinstance(value, list) or any(type(n) is not int or not 0 <= n <= 255 for n in value):
        raise ResourceError(tr('{0}必须是 0～255 的整数数组', label))
    return bytes(value)


def wrapper_metadata(data, kind):
    if kind == 'dsc':
        seed, size, tokens, reserved = struct.unpack_from('<4I', data, 16)
        return dict(format='dsc', seed=seed, unpacked_size=size, token_count=tokens, reserved=reserved)
    if kind == 'bse':
        version, reserved, seed = struct.unpack_from('<HHI', data, 8)
        return dict(format='bse', version=version, reserved=reserved, seed=seed)
    raise ResourceError(tr('未知封装类型：{0}', kind))


def wrapper_header(meta):
    kind = meta['format']
    if kind == 'dsc':
        return b'DSC FORMAT 1.00\0' + struct.pack('<4I', meta.get('seed',0),
            meta.get('unpacked_size',0), meta.get('token_count',0), meta.get('reserved',0))
    if kind == 'bse':
        return b'BSE 1.0\0' + struct.pack('<HHI', meta.get('version',256), meta.get('reserved',0), meta.get('seed',0))
    raise ResourceError(tr('未知封装类型：{0}', kind))


def bw_header_metadata(header):
    words = struct.unpack_from('<14I', header, 8)
    return dict(header_size=len(header), data_size=words[0], frames=words[1], sample_rate=words[2],
        channels=words[3], loop_flag=words[4], loop_start_frame=words[5],
        loop_adpcm_states=list(struct.unpack_from('<4i',header,32)), codec=words[10], reserved=words[11],
        loop_bit_offset=words[12], step_parameter=words[13], extended_header_bytes=list(header[64:]))


def build_bw_header(meta):
    h = meta or {}
    extra = byte_values(h.get('extended_header_bytes',[]), tr('BW 扩展头'))
    if len(extra) > 1024 * 1024 - 64:
        raise ResourceError(tr('BW 扩展头过长'))
    header = bytearray(64) + extra
    struct.pack_into('<I4s6I4i4I',header,0,len(header),b'bw  ', h.get('data_size',0),h.get('frames',0),
        h.get('sample_rate',44100),h.get('channels',2),h.get('loop_flag',0),h.get('loop_start_frame',0),
        *h.get('loop_adpcm_states',[0,0,0,0]),h.get('codec',3),h.get('reserved',0),
        h.get('loop_bit_offset',0),h.get('step_parameter',0))
    return header
