"""BGI compression primitives, following GARbro's MIT-licensed format readers.

See THIRD_PARTY_NOTICES.md for attribution. No game executable is required.
"""
from __future__ import annotations
from .i18n import tr

import heapq
import struct
from collections import Counter


class ResourceError(ValueError):
    pass


class Bits:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def get(self, count: int = 1) -> int:
        value = 0
        for _ in range(count):
            if self.pos >= len(self.data) * 8:
                raise ResourceError(tr('压缩位流提前结束'))
            value = (value << 1) | ((self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value


def varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if pos >= len(data):
            raise ResourceError(tr('变长整数提前结束'))
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ResourceError(tr('变长整数溢出'))


def put_varint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def keys(seed: int, magic: int = 0):
    while True:
        v0 = 20021 * (seed & 65535)
        v1 = ((magic | (seed >> 16)) * 20021 + seed * 346 + (v0 >> 16)) & 65535
        seed = ((v1 << 16) + (v0 & 65535) + 1) & 0xffffffff
        yield v1 & 255


def crypt(data: bytes, seed: int, *, encode=False, magic=0) -> bytes:
    gen = keys(seed, magic)
    return bytes((byte + next(gen) if encode else byte - next(gen)) & 255 for byte in data)


def weighted_tree(weights: list[int]):
    heap = [(weight, index, index) for index, weight in enumerate(weights) if weight]
    heapq.heapify(heap)
    if not heap:
        raise ResourceError(tr('Huffman 权重表为空'))
    nodes = [None] * len(weights)
    while len(heap) > 1:
        a, _, left = heapq.heappop(heap)
        b, _, right = heapq.heappop(heap)
        index = len(nodes)
        nodes.append((left, right))
        heapq.heappush(heap, (a + b, index, index))
    root = heap[0][2]
    # GARbro creates a parent even for a single nonzero symbol.
    if nodes[root] is None:
        nodes.append((root, -1))
        root = len(nodes) - 1
    return nodes, root


def huffman_decode(data: bytes, weights: list[int], count: int) -> bytes:
    nodes, root = weighted_tree(weights)
    bits = Bits(data)
    output = bytearray(count)
    for i in range(count):
        node = root
        while nodes[node] is not None:
            node = nodes[node][bits.get()]
            if node < 0:
                raise ResourceError(tr('无效 Huffman 分支'))
        output[i] = node
    return bytes(output)


def huffman_encode(data: bytes) -> tuple[bytes, bytes]:
    histogram = Counter(data)
    weights = [histogram[i] for i in range(256)]
    nodes, root = weighted_tree(weights)
    codes = {}
    def visit(node, code, length):
        if node < 0:
            return
        if nodes[node] is None:
            codes[node] = (code, length)
        else:
            visit(nodes[node][0], code << 1, length + 1)
            visit(nodes[node][1], (code << 1) | 1, length + 1)
    visit(root, 0, 0)
    output = bytearray()
    cache = count = 0
    for byte in data:
        code, length = codes[byte]
        cache = (cache << length) | code
        count += length
        while count >= 8:
            count -= 8
            output.append((cache >> count) & 255)
        cache &= (1 << count) - 1
    if count:
        output.append((cache << (8 - count)) & 255)
    return b''.join(put_varint(weight) for weight in weights), bytes(output)


def unwrap_bse(data: bytes, *, _encode=False) -> bytes:
    if not data.startswith(b'BSE 1.'):
        return data
    if len(data) < 80:
        raise ResourceError(tr('BSE 头不完整'))
    version = struct.unpack_from('<H', data, 8)[0]
    if version not in (256, 257):
        raise ResourceError(tr('不支持的 BSE 版本：{0}', version))
    key = struct.unpack_from('<I', data, 12)[0]
    def next_key():
        nonlocal key
        signed = key if key < 0x80000000 else key - 0x100000000
        a, shift, b, c, xor = (257, 8, 97, 23, 0xa6cd9b75) if version == 256 else (127, 7, 83, 53, 0xb97a7e5c)
        product = (signed * a) & 0xffffffff
        product = product if product < 0x80000000 else product - 0x100000000
        value = (((product >> shift) + signed * b + c) & 0xffffffff) ^ xor
        key = ((value >> 16) | (value << 16)) & 0xffffffff
        return key
    header = bytearray(data[16:80])
    done = set()
    for _ in range(64):
        dst = next_key() & 63
        while dst in done:
            dst = (dst + 1) & 63
        shift = next_key() & 7
        right = not (next_key() & 1)
        key_byte = next_key()
        if _encode:
            symbol = header[dst]
            rotated = ((symbol << shift) | (symbol >> (8-shift))) if right else ((symbol >> shift) | (symbol << (8-shift)))
            header[dst] = (rotated + key_byte) & 255
        else:
            symbol = (header[dst] - key_byte) & 255
            header[dst] = ((symbol >> shift) | (symbol << (8 - shift))) & 255 if right else ((symbol << shift) | (symbol >> (8 - shift))) & 255
        done.add(dst)
    return (data[:16] if _encode else b'') + bytes(header) + data[80:]


def wrap_bse(data: bytes, header: bytes) -> bytes:
    if len(header) != 16 or not header.startswith(b'BSE 1.'):
        raise ResourceError(tr('BSE 封装头无效'))
    if len(data) < 64:
        raise ResourceError(tr('BSE 封装的数据不能少于 64 字节'))
    return unwrap_bse(header + data, _encode=True)


def decode_dsc(data: bytes) -> bytes:
    return decode_dsc_stream(data)[0]


def decode_dsc_stream(data: bytes) -> tuple[bytes, int]:
    """Decode one DSC stream and return its byte end, excluding any trailer."""
    if not data.startswith(b'DSC FORMAT 1.00\0') or len(data) < 544:
        raise ResourceError(tr('不是完整的 DSC FORMAT 1.00 文件'))
    seed, size, token_count = struct.unpack_from('<III', data, 16)
    if size > 512 * 1024 * 1024:
        raise ResourceError(tr('DSC 解码尺寸过大'))
    depths = crypt(data[32:544], seed, magic=struct.unpack_from('<H', data)[0] << 16)
    symbols = sorted((depth, symbol) for symbol, depth in enumerate(depths) if depth)
    table = {}
    code = previous_depth = 0
    for depth, symbol in symbols:
        if depth > 32:
            raise ResourceError(tr('DSC Huffman 深度无效'))
        code <<= depth - previous_depth
        table[depth, code] = symbol
        code += 1
        previous_depth = depth
    bits = Bits(data[544:])
    output = bytearray()
    for _ in range(token_count):
        code = 0
        for depth in range(1, 33):
            code = (code << 1) | bits.get()
            symbol = table.get((depth, code))
            if symbol is not None:
                break
        else:
            raise ResourceError(tr('DSC Huffman 符号无效'))
        if symbol < 256:
            output.append(symbol)
        else:
            distance = bits.get(12) + 2
            count = (symbol & 255) + 2
            if distance > len(output):
                raise ResourceError(tr('DSC 回溯越界'))
            for _ in range(count):
                output.append(output[-distance])
        if len(output) > size:
            raise ResourceError(tr('DSC 输出超出声明大小'))
    if len(output) != size:
        raise ResourceError(tr('DSC 输出长度与文件头不符'))
    return bytes(output), 544 + (bits.pos + 7) // 8


def encode_dsc(data: bytes, header: bytes | None = None) -> bytes:
    """Write a valid literal Huffman stream while retaining opaque header bytes."""
    result = bytearray(header or (b'DSC FORMAT 1.00\0' + bytes(16)))
    if len(result) != 32:
        raise ResourceError(tr('DSC 头长度必须为 32 字节'))
    struct.pack_into('<II', result, 20, len(data), len(data))
    seed = struct.unpack_from('<I', result, 16)[0]
    depths = bytes([8]) * 256 + bytes(256)
    return bytes(result) + crypt(depths, seed, encode=True, magic=struct.unpack_from('<H', result)[0] << 16) + data
