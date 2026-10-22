from .i18n import tr
import sys
import os
import struct
import io
import math
import argparse
import subprocess
import shutil
import time
import glob
import threading
import queue
import ctypes
import platform
import multiprocessing as mp
import types
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, wait, FIRST_COMPLETED, ALL_COMPLETED

try:
    import winreg
    HAS_WINREG = True
except ImportError: 
    HAS_WINREG = False

from PIL import Image

# NumPy is needed by the frame buffers; Numba is prepared off the GUI thread.
import importlib.util
from functools import wraps

_numba_lock = threading.RLock()
_numba_kernels = []
_numba_ready = False
_numba_module = None
_numba_error = None


class _DeferredNumba:
    def __getattr__(self, name):
        prepare_numba()
        if _numba_module is None:
            raise AttributeError(name)
        return getattr(_numba_module, name)


def prepare_numba():
    """Import Numba and register dispatchers without compiling frame signatures."""
    global _numba_ready, _numba_module, _numba_error
    with _numba_lock:
        if _numba_ready:
            return _numba_module
        try:
            import numba as module
            _numba_module = module
        except Exception as error:
            _numba_error = error
        for record in _numba_kernels:
            function, options = record['function'], record['options']
            record['compiled'] = _numba_module.jit(**options)(function) if _numba_module else function
            owner = sys.modules.get(function.__module__)
            if owner is not None and getattr(owner, function.__name__, None) is record['deferred']:
                setattr(owner, function.__name__, record['compiled'])
        _numba_ready = True
        return _numba_module


def jit(**options):
    def decorate(function):
        if _numba_ready:
            return _numba_module.jit(**options)(function) if _numba_module else function
        record = {'function': function, 'options': options}
        @wraps(function)
        def deferred(*args, **kwargs):
            prepare_numba()
            return record['compiled'](*args, **kwargs)
        record['deferred'] = deferred
        with _numba_lock:
            if _numba_ready:
                return _numba_module.jit(**options)(function) if _numba_module else function
            _numba_kernels.append(record)
        return deferred
    return decorate


try:
    import numpy as np
    HAS_OPTIMIZATION = importlib.util.find_spec('numba') is not None
    numba = _DeferredNumba() if HAS_OPTIMIZATION else None
except ImportError:
    np = None
    numba = None
    HAS_OPTIMIZATION = False

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False

# ==========================================
# C 解码库加载
# ==========================================
_decoder_lib = None

def _load_decoder_lib():
    """加载 C 编写的 BM 解码动态库，返回库对象，失败返回 None"""
    global _decoder_lib
    if _decoder_lib is not None:
        return _decoder_lib

    # 根据操作系统确定库文件名
    system = platform.system()
    if system == 'Windows':
        lib_name = 'bm_decoder.dll'
    elif system == 'Darwin':
        lib_name = 'libbm_decoder.dylib'
    else:  # Linux
        lib_name = 'libbm_decoder.so'

    # 构造绝对路径（脚本所在目录）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    lib_path = os.path.join(script_dir, "native", lib_name)

    try:
        # 检查文件是否存在
        if not os.path.isfile(lib_path):
            print(tr('警告: 未找到解码库文件 {0}', lib_path))
            return None

        # 尝试加载
        lib = ctypes.CDLL(lib_path)
        # 设置函数原型
        lib.decode_bm_frame.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte),  # frame_data
            ctypes.c_int,                     # data_len
            ctypes.c_int, ctypes.c_int,       # width, height
            ctypes.c_int,                     # bpp (24 or 32)
            ctypes.POINTER(ctypes.c_ubyte),   # quant_tables (128 bytes)
            ctypes.POINTER(ctypes.c_ubyte)    # output buffer (width*height*4)
        ]
        lib.decode_bm_frame.restype = ctypes.c_int  # 0 成功，负值失败
        try:
            lib.decode_bm_frame_rgba.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.c_int,
                ctypes.c_int, ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte)
            ]
            lib.decode_bm_frame_rgba.restype = ctypes.c_int
        except Exception:
            pass
        try:
            lib.bm_decoder_set_threads.argtypes = [ctypes.c_int]
            lib.bm_decoder_set_threads.restype = None
        except Exception:
            pass
        _decoder_lib = lib
        print(tr('成功加载 C 解码库: {0}', lib_path))
    except Exception as e:
        print(tr('警告: 无法加载 C 解码库 {0}，将使用 Python 解码（速度较慢）。错误: {1}', lib_path, e))
        _decoder_lib = None
    return _decoder_lib

def _normalize_parallel_count(value):
    max_threads = max(1, os.cpu_count() or 1)
    if value is None:
        return max_threads
    try:
        v = int(value)
    except Exception:
        return max_threads
    if v <= 0:
        return max_threads
    return min(v, max_threads)

def _set_c_decoder_threads(count):
    lib = _load_decoder_lib()
    if lib is None:
        return
    if not hasattr(lib, "bm_decoder_set_threads"):
        return
    try:
        lib.bm_decoder_set_threads(int(count))
    except Exception:
        pass

def _swap_rb_bytes(data: bytes) -> bytes:
    """快速交换 RGBA 数据的 R 和 B 通道（BGRA ↔ RGBA）"""
    if HAS_OPTIMIZATION and np is not None:
        # 创建可写的副本（np.frombuffer 只读，故需 .copy()）
        arr = np.frombuffer(data, dtype=np.uint8).reshape(-1, 4).copy()
        # 向量化交换 R 和 B 列
        arr[:, [0, 2]] = arr[:, [2, 0]]
        return arr.tobytes()
    else:
        # 回退：使用 memoryview 和 bytearray 进行逐像素交换
        buf = bytearray(data)
        mv = memoryview(buf)
        for i in range(0, len(buf), 4):
            mv[i], mv[i+2] = mv[i+2], mv[i]
        return bytes(buf)

@lru_cache(maxsize=64)
def _quant_tables_to_ctypes(quant_tables: bytes):
    if len(quant_tables) != 128:
        raise ValueError("quant_tables length mismatch")
    return (ctypes.c_ubyte * 128).from_buffer_copy(quant_tables)

def decode_frame_c(frame_data, width, height, bpp, quant_tables, prev_bytes=None):
    """
    使用 C 库解码一帧 BM 数据。
    返回 RGBA 字节数据（bytes），失败时抛出异常。
    """
    lib = _load_decoder_lib()
    if lib is None:
        raise RuntimeError(tr('C 解码库未加载'))

    out_size = width * height * 4
    out_buf = (ctypes.c_ubyte * out_size)()
    use_rgba_fn = hasattr(lib, "decode_bm_frame_rgba")
    if prev_bytes is not None:
        if len(prev_bytes) != out_size:
            raise ValueError("prev_bytes length mismatch")
        if use_rgba_fn:
            ctypes.memmove(out_buf, prev_bytes, out_size)
        else:
            prev_bgra = _swap_rb_bytes(prev_bytes)
            ctypes.memmove(out_buf, prev_bgra, out_size)

    # 将 frame_data 转换为 ctypes 数组
    frame_data_len = len(frame_data)
    if frame_data_len <= 0:
        raise ValueError("empty frame_data")
    frame_data_arr = (ctypes.c_ubyte * frame_data_len).from_buffer_copy(frame_data)

    # 将 quant_tables 转换为 ctypes 数组（固定 128 字节）
    quant_arr = _quant_tables_to_ctypes(quant_tables)

    if use_rgba_fn:
        ret = lib.decode_bm_frame_rgba(
            frame_data_arr, frame_data_len,
            width, height, bpp,
            quant_arr, out_buf
        )
    else:
        ret = lib.decode_bm_frame(
            frame_data_arr, frame_data_len,
            width, height, bpp,
            quant_arr, out_buf
        )
    if ret != 0:
        raise RuntimeError(tr('C 解码失败，错误码 {0}', ret))
    if use_rgba_fn:
        return bytes(out_buf)
    return _swap_rb_bytes(bytes(out_buf))


# ==========================================
# 常量定义
# ==========================================

DCT_Table = [
    1.00000000, 1.38703990, 1.30656302, 1.17587554, 1.00000000, 0.78569496, 0.54119611, 0.27589938,
    1.38703990, 1.92387950, 1.81225491, 1.63098633, 1.38703990, 1.08979023, 0.75066054, 0.38268343,
    1.30656302, 1.81225491, 1.70710683, 1.53635550, 1.30656302, 1.02655995, 0.70710677, 0.36047992,
    1.17587554, 1.63098633, 1.53635550, 1.38268340, 1.17587554, 0.92387950, 0.63637930, 0.32442334,
    1.00000000, 1.38703990, 1.30656302, 1.17587554, 1.00000000, 0.78569496, 0.54119611, 0.27589938,
    0.78569496, 1.08979023, 1.02655995, 0.92387950, 0.78569496, 0.61731654, 0.42521504, 0.21677275,
    0.54119611, 0.75066054, 0.70710677, 0.63637930, 0.54119611, 0.42521504, 0.29289323, 0.14931567,
    0.27589938, 0.38268343, 0.36047992, 0.32442334, 0.27589938, 0.21677275, 0.14931567, 0.07612047,
]

block_fill_order = [
    0,  1,  8,  16, 9,  2,  3,  10, 17, 24, 32, 25, 18, 11, 4,  5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,  7,  14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
]

# ==========================================
# 优化逻辑 (Numba/Numpy) - 保留作为回退
# ==========================================

if HAS_OPTIMIZATION:
    DCT_TABLE_NP = np.array(DCT_Table, dtype=np.float64)
    BLOCK_FILL_ORDER_NP = np.array(block_fill_order, dtype=np.int32)

    @jit(nopython=True, fastmath=True)
    def float_to_short_jit(f):
        return 128.0 + f * 0.125

    @jit(nopython=True, fastmath=True)
    def float_to_byte_jit(f):
        if f >= 255: return 255
        if f <= 0: return 0
        return int(f)

    @jit(nopython=True, fastmath=True)
    def decode_dct_jit(channel, data, src, dct_matrix_row, tmp, y_cb_cr_block):
        for i in range(8):
            v1 = data[src + i] * dct_matrix_row[i]
            v2 = data[src + 8 + i] * dct_matrix_row[8 + i]
            v3 = data[src + 16 + i] * dct_matrix_row[16 + i]
            v4 = data[src + 24 + i] * dct_matrix_row[24 + i]
            v5 = data[src + 32 + i] * dct_matrix_row[32 + i]
            v6 = data[src + 40 + i] * dct_matrix_row[40 + i]
            v7 = data[src + 48 + i] * dct_matrix_row[48 + i]
            v8 = data[src + 56 + i] * dct_matrix_row[56 + i]
            
            v10 = v1 + v5
            v11 = v1 - v5
            v12 = v3 + v7
            v13 = (v3 - v7) * 1.414213562 - v12
            v1 = v10 + v12
            v7 = v10 - v12
            v3 = v11 + v13
            v5 = v11 - v13
            v14 = v2 + v8
            v15 = v2 - v8
            v16 = v6 + v4
            v17 = v6 - v4
            v8 = v14 + v16
            v11 = (v14 - v16) * 1.414213562
            v9 = (v17 + v15) * 1.847759065
            v10 = 1.082392200 * v15 - v9
            v13 = -2.613125930 * v17 + v9
            v6 = v13 - v8
            v4 = v11 - v6
            v2 = v10 + v4
            
            tmp[0, i] = v1 + v8
            tmp[1, i] = v3 + v6
            tmp[2, i] = v5 + v4
            tmp[3, i] = v7 - v2
            tmp[4, i] = v7 + v2
            tmp[5, i] = v5 - v4
            tmp[6, i] = v3 - v6
            tmp[7, i] = v1 - v8
            
        dst_idx = 0
        for i in range(8):
            v10 = tmp[i, 0] + tmp[i, 4]
            v11 = tmp[i, 0] - tmp[i, 4]
            v12 = tmp[i, 2] + tmp[i, 6]
            v13 = tmp[i, 2] - tmp[i, 6]
            v14 = tmp[i, 1] + tmp[i, 7]
            v15 = tmp[i, 1] - tmp[i, 7]
            v16 = tmp[i, 5] + tmp[i, 3]
            v17 = tmp[i, 5] - tmp[i, 3]
            
            v13 = 1.414213562 * v13 - v12
            v1 = v10 + v12
            v7 = v10 - v12
            v3 = v11 + v13
            v5 = v11 - v13
            v8 = v14 + v16
            v11 = (v14 - v16) * 1.414213562
            v9 = (v17 + v15) * 1.847759065
            v10 = v9 - v15 * 1.082392200
            v13 = v9 - v17 * 2.613125930
            v6 = v13 - v8
            v4 = v11 - v6
            v2 = v10 - v4
            
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v1 + v8)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v3 + v6)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v5 + v4)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v7 + v2)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v7 - v2)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v5 - v4)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v3 - v6)
            dst_idx += 1
            y_cb_cr_block[dst_idx, channel] = float_to_short_jit(v1 - v8)
            dst_idx += 1

    @jit(nopython=True, fastmath=True)
    def ycbcr_to_rgb_jit(y_cb_cr_block, width, output, dst_offset, write_alpha):
        for j in range(64):
            cy = y_cb_cr_block[j, 0]
            cb = y_cb_cr_block[j, 1]
            cr = y_cb_cr_block[j, 2]
            
            r = cy + 1.402 * cr - 178.956
            g = cy - 0.34414 * cb - 0.71414 * cr + 135.95984
            b = cy + 1.772 * cb - 226.316
            
            y = j >> 3
            x = j & 7
            p = (y * width + x) * 4
            
            idx = dst_offset + p
            output[idx]   = float_to_byte_jit(b)
            output[idx+1] = float_to_byte_jit(g)
            output[idx+2] = float_to_byte_jit(r)
            if write_alpha:
                output[idx+3] = 255


# ==========================================
# 核心类（Python 解码器，保留作为回退）
# ==========================================

class MsbBitStream:
    def __init__(self, stream):
        self.stream = stream
        self.bit_buffer = 0
        self.bit_count = 0

    def get_bits(self, count):
        result = 0
        while count > 0:
            if self.bit_count == 0:
                byte = self.stream.read(1)
                if not byte:
                    raise EOFError("End of stream in get_bits")
                self.bit_buffer = byte[0]
                self.bit_count = 8
            
            bits_to_read = min(count, self.bit_count)
            shift = self.bit_count - bits_to_read
            mask = (1 << bits_to_read) - 1
            chunk = (self.bit_buffer >> shift) & mask
            
            result = (result << bits_to_read) | chunk
            self.bit_count -= bits_to_read
            count -= bits_to_read
        return result

    def get_next_bit(self):
        return self.get_bits(1)

class HuffmanNode:
    def __init__(self, weight=0, valid=True, is_parent=False, left=-1, right=-1):
        self.valid = valid
        self.weight = weight
        self.is_parent = is_parent
        self.left = left
        self.right = right

class HuffmanTree:
    def __init__(self, weights, v2=False):
        self.nodes = []
        root_weight = 0
        for w in weights:
            node = HuffmanNode(weight=w, valid=(w != 0))
            self.nodes.append(node)
            root_weight += w
            
        while True:
            child_indices = [-1, -1]
            combined_weight = 0
            
            for i in range(2):
                min_weight = 0xFFFFFFFF
                idx = -1
                n = 0
                
                if v2:
                    while n < len(self.nodes):
                        if self.nodes[n].valid:
                            min_weight = self.nodes[n].weight
                            idx = n
                            n += 1
                            break
                        n += 1
                    n = max(n, i + 1)
                
                while n < len(self.nodes):
                    if self.nodes[n].valid and self.nodes[n].weight < min_weight:
                        min_weight = self.nodes[n].weight
                        idx = n
                    n += 1
                
                child_indices[i] = idx
                if idx == -1:
                    continue
                
                self.nodes[idx].valid = False
                combined_weight += self.nodes[idx].weight
            
            parent = HuffmanNode(weight=combined_weight, valid=True, is_parent=True,
                                 left=child_indices[0], right=child_indices[1])
            self.nodes.append(parent)
            
            if combined_weight >= root_weight:
                break
                
    def decode_token(self, bit_stream):
        idx = len(self.nodes) - 1
        while self.nodes[idx].is_parent:
            bit = bit_stream.get_next_bit()
            if bit == 0:
                idx = self.nodes[idx].left
            else:
                idx = self.nodes[idx].right
        return idx

def read_integer(stream):
    v = 0
    code_length = 0
    while True:
        byte = stream.read(1)
        if not byte:
            return -1
        code = byte[0]
        if code_length >= 32:
            return -1
        v |= (code & 0x7f) << code_length
        code_length += 7
        if (code & 0x80) == 0:
            break
    return v

def read_weight_table(stream, length):
    weights = []
    for _ in range(length):
        w = read_integer(stream)
        if w == -1:
            raise ValueError("Invalid weight table")
        weights.append(w)
    return weights

class ParallelCbgDecoder:
    def __init__(self, width, height, bpp, quant_matrix):
        self.width = (width + 7) & -8
        self.height = (height + 7) & -8
        self.bpp = bpp
        
        if HAS_OPTIMIZATION:
            self.output = np.zeros(self.width * self.height * 4, dtype=np.uint8)
            self.dct_tables = np.zeros((2, 64), dtype=np.float64)
            for i in range(128):
                val = quant_matrix[i]
                self.dct_tables[i >> 6, i & 0x3F] = val * DCT_TABLE_NP[i & 0x3F]
            self.y_cb_cr_block = np.zeros((64, 3), dtype=np.float64)
            self.tmp = np.zeros((8, 8), dtype=np.float64)
        else:
            self.output = bytearray(self.width * self.height * 4)
            self.dct = [[0.0]*64 for _ in range(2)]
            for i in range(128):
                val = quant_matrix[i]
                self.dct[i >> 6][i & 0x3F] = val * DCT_Table[i & 0x3F]
            self.y_cb_cr_block = [[0]*3 for _ in range(64)]
            self.tmp = [[0.0]*8 for _ in range(8)]
            
        self.has_alpha = False
        self.write_alpha = True
        self.tree1 = None
        self.tree2 = None
        self.input_data = None

    def unpack_block(self, offset, length, dst, mask=None):
        if offset + length > len(self.input_data):
            return
            
        stream = io.BytesIO(self.input_data[offset : offset+length])
        reader = MsbBitStream(stream)
        
        try:
            block_size = read_integer(stream)
        except:
            return
            
        if block_size == -1:
            return

        if HAS_OPTIMIZATION:
            color_data = np.zeros(block_size, dtype=np.int32)
        else:
            color_data = [0] * block_size
            
        acc = 0
        
        for i in range(0, block_size, 64):
            if stream.tell() >= length: break
            try:
                count = self.tree1.decode_token(reader)
            except EOFError:
                break
                
            if count != 0:
                v = reader.get_bits(count)
                if (v >> (count - 1)) == 0:
                    v = (-1 << count | v) + 1
                acc += v
            if i < len(color_data):
                color_data[i] = acc
            
        if (reader.bit_count & 7) != 0:
            reader.get_bits(reader.bit_count & 7)
            
        for i in range(0, block_size, 64):
            if stream.tell() >= length: break
            index = 1
            while index < 64 and stream.tell() < length:
                try:
                    code = self.tree2.decode_token(reader)
                except EOFError:
                    break
                    
                if code == 0:
                    break
                if code == 0xF:
                    index += 0x10
                    continue
                
                index += code & 0xF
                if index >= 64:
                    break
                    
                code >>= 4
                v = reader.get_bits(code)
                if code != 0 and (v >> (code - 1)) == 0:
                    v = (-1 << code | v) + 1
                
                pos = i + block_fill_order[index]
                if pos < len(color_data):
                    color_data[pos] = v
                index += 1
        
        if self.bpp == 8:
            pass
        else:
            if mask:
                self.decode_rgb_masked(color_data, dst, mask)
            else:
                self.decode_rgb(color_data, dst)

    def decode_rgb(self, data, dst):
        block_count = self.width // 8
        for i in range(block_count):
            src = i * 64
            skip_block = False
            
            for channel in range(3):
                if not self.decode_dct(channel, data, src):
                    skip_block = True
                src += self.width * 8
            
            if skip_block:
                dst += 32
                continue

            self.convert_to_rgb(dst)
            dst += 32

    def decode_rgb_masked(self, data, dst, mask):
        block_count = self.width // 8
        mask_bits = []
        for i in range(block_count):
            b = mask[i >> 3]
            mask_bits.append((b >> (i & 7)) & 1)
            
        present_count = sum(mask_bits)
        if present_count == 0:
            return
            
        plane_size = present_count * 64
        present_index = 0
        
        for i in range(block_count):
            if not mask_bits[i]:
                dst += 32
                continue
                
            src_base = present_index * 64
            skip_block = False
            for channel in range(3):
                src = src_base + channel * plane_size
                if not self.decode_dct(channel, data, src):
                    skip_block = True
                    
            if skip_block:
                dst += 32
                present_index += 1
                continue

            self.convert_to_rgb(dst)
            dst += 32
            present_index += 1

    def decode_dct(self, channel, data, src):
        if src + 63 >= len(data):
            return False
            
        d_idx = 1 if channel > 0 else 0
        
        if HAS_OPTIMIZATION:
            decode_dct_jit(channel, data, src, self.dct_tables[d_idx], self.tmp, self.y_cb_cr_block)
            return True
        else:
            # 回退到慢速 Python 实现
            for i in range(8):
                v1 = data[src + i] * self.dct[d_idx][i]
                v2 = data[src + 8 + i] * self.dct[d_idx][8 + i]
                v3 = data[src + 16 + i] * self.dct[d_idx][16 + i]
                v4 = data[src + 24 + i] * self.dct[d_idx][24 + i]
                v5 = data[src + 32 + i] * self.dct[d_idx][32 + i]
                v6 = data[src + 40 + i] * self.dct[d_idx][40 + i]
                v7 = data[src + 48 + i] * self.dct[d_idx][48 + i]
                v8 = data[src + 56 + i] * self.dct[d_idx][56 + i]
                
                v10 = v1 + v5
                v11 = v1 - v5
                v12 = v3 + v7
                v13 = (v3 - v7) * 1.414213562 - v12
                v1 = v10 + v12
                v7 = v10 - v12
                v3 = v11 + v13
                v5 = v11 - v13
                v14 = v2 + v8
                v15 = v2 - v8
                v16 = v6 + v4
                v17 = v6 - v4
                v8 = v14 + v16
                v11 = (v14 - v16) * 1.414213562
                v9 = (v17 + v15) * 1.847759065
                v10 = 1.082392200 * v15 - v9
                v13 = -2.613125930 * v17 + v9
                v6 = v13 - v8
                v4 = v11 - v6
                v2 = v10 + v4
                
                self.tmp[0][i] = v1 + v8
                self.tmp[1][i] = v3 + v6
                self.tmp[2][i] = v5 + v4
                self.tmp[3][i] = v7 - v2
                self.tmp[4][i] = v7 + v2
                self.tmp[5][i] = v5 - v4
                self.tmp[6][i] = v3 - v6
                self.tmp[7][i] = v1 - v8
            
            dst_idx = 0
            def f2s(f):
                a = 128 + (int(f) >> 3)
                if a <= 0: return 0
                if a <= 255: return a
                if a < 384: return 255
                return 0

            for i in range(8):
                v10 = self.tmp[i][0] + self.tmp[i][4]
                v11 = self.tmp[i][0] - self.tmp[i][4]
                v12 = self.tmp[i][2] + self.tmp[i][6]
                v13 = self.tmp[i][2] - self.tmp[i][6]
                v14 = self.tmp[i][1] + self.tmp[i][7]
                v15 = self.tmp[i][1] - self.tmp[i][7]
                v16 = self.tmp[i][5] + self.tmp[i][3]
                v17 = self.tmp[i][5] - self.tmp[i][3]
                
                v13 = 1.414213562 * v13 - v12
                v1 = v10 + v12
                v7 = v10 - v12
                v3 = v11 + v13
                v5 = v11 - v13
                v8 = v14 + v16
                v11 = (v14 - v16) * 1.414213562
                v9 = (v17 + v15) * 1.847759065
                v10 = v9 - v15 * 1.082392200
                v13 = v9 - v17 * 2.613125930
                v6 = v13 - v8
                v4 = v11 - v6
                v2 = v10 - v4
                
                self.y_cb_cr_block[dst_idx][channel] = f2s(v1 + v8)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v3 + v6)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v5 + v4)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v7 + v2)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v7 - v2)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v5 - v4)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v3 - v6)
                dst_idx += 1
                self.y_cb_cr_block[dst_idx][channel] = f2s(v1 - v8)
                dst_idx += 1
            return True

    def convert_to_rgb(self, dst):
        if HAS_OPTIMIZATION:
            ycbcr_to_rgb_jit(self.y_cb_cr_block, self.width, self.output, dst, self.write_alpha)
        else:
            def f2b(f):
                if f >= 255: return 255
                if f <= 0: return 0
                return int(f)

            for j in range(64):
                cy = self.y_cb_cr_block[j][0]
                cb = self.y_cb_cr_block[j][1]
                cr = self.y_cb_cr_block[j][2]
                
                r = cy + 1.402 * cr - 178.956
                g = cy - 0.34414 * cb - 0.71414 * cr + 135.95984
                b = cy + 1.772 * cb - 226.316
                
                y = j >> 3
                x = j & 7
                p = (y * self.width + x) * 4
                
                if dst + p + 2 < len(self.output):
                    self.output[dst+p]   = f2b(b)
                    self.output[dst+p+1] = f2b(g)
                    self.output[dst+p+2] = f2b(r)
                    if self.write_alpha:
                        self.output[dst+p+3] = 255

    def unpack_alpha(self, offset):
        if offset < 0: return
        if HAS_OPTIMIZATION:
            if offset + 4 > len(self.input_data): return
            data_view = memoryview(self.input_data)
            stream = io.BytesIO(data_view[offset:])
        else:
            if offset + 4 > len(self.input_data): return
            stream = io.BytesIO(self.input_data[offset:])

        header_bytes = stream.read(4)
        if len(header_bytes) < 4: return
        header = struct.unpack('<i', header_bytes)[0]
        if header != 1: return
        
        dst = 3
        ctl = 1 << 1
        width = self.width
        out_len = len(self.output)
        
        out = self.output
        
        while dst < out_len:
            ctl >>= 1
            if ctl == 1:
                b = stream.read(1)
                if not b: return
                ctl = b[0] | 0x100
            
            if ctl & 1:
                v_bytes = stream.read(2)
                if len(v_bytes) < 2: return
                v = struct.unpack('<H', v_bytes)[0]
                x = v & 0x3F
                if x > 0x1F: x |= -0x40
                y = (v >> 6) & 7
                if y != 0: y |= -8
                count = ((v >> 9) & 0x7F) + 3
                
                src = dst + (x + y * width) * 4
                if src < 0 or src >= dst: return
                
                for _ in range(count):
                    if dst >= out_len or src >= out_len: return
                    out[dst] = out[src]
                    src += 4
                    dst += 4
            else:
                b = stream.read(1)
                if not b: return
                out[dst] = b[0]
                dst += 4
        self.has_alpha = True


# ==========================================
# 辅助函数
# ==========================================

def read_frame_offsets(file_obj):
    file_obj.seek(0xC0)
    frame_offsets = []
    while True:
        pos = file_obj.tell()
        if len(frame_offsets) > 0 and pos >= frame_offsets[0]:
            break
        chunk = file_obj.read(4)
        if len(chunk) < 4:
            break
        offset = struct.unpack('<I', chunk)[0]
        if offset < pos:
            break
        frame_offsets.append(offset)
    return frame_offsets

def iter_frame_bytes(file_obj, frame_offsets, file_size, limit=None):
    total = len(frame_offsets)
    if limit is None:
        count = total
    else:
        count = min(int(limit), total)
    current_pos = file_obj.tell()
    for i in range(count):
        offset = frame_offsets[i]
        if i + 1 < total:
            length = frame_offsets[i + 1] - offset
        else:
            length = file_size - offset
        if length <= 0:
            yield i, offset, length, b""
            continue
        if current_pos != offset:
            file_obj.seek(offset)
        data = file_obj.read(length)
        current_pos = file_obj.tell()
        yield i, offset, length, data

def get_frame_count(filepath):
    try:
        with open(filepath, 'rb', buffering=1024 * 1024) as f:
            magic = f.read(16)
            if b'BF_Movie' not in magic:
                return 0
            return len(read_frame_offsets(f))
    except Exception:
        return 0

def decode_frame(decoder, data, width, height):
    """Python 解码函数（保留作为回退）"""
    stream = io.BytesIO(data)
    weights1 = read_weight_table(stream, 0x10)
    decoder.tree1 = HuffmanTree(weights1, v2=True)
    weights2 = read_weight_table(stream, 0xB0)
    decoder.tree2 = HuffmanTree(weights2, v2=True)

    y_blocks = ((height + 7) & -8) // 8
    num_offsets = y_blocks + 1
    offset_table_pos = stream.tell()
    
    raw_offsets = []
    for _ in range(num_offsets):
        read_bytes = stream.read(4)
        if len(read_bytes) < 4: break
        raw_offsets.append(struct.unpack('<I', read_bytes)[0])

    if len(raw_offsets) < num_offsets: return

    compressed_data = stream.read()

    def build_offsets(table_len, data):
        input_base = offset_table_pos + table_len * 4
        offsets = [v - input_base for v in raw_offsets[:table_len]]
        return offsets, data

    def offsets_valid(offsets, data_len):
        if not offsets: return False
        if any(o < 0 for o in offsets): return False
        if any(offsets[i] > offsets[i + 1] for i in range(len(offsets) - 1)): return False
        return offsets[-1] <= data_len

    offsets, data = build_offsets(num_offsets, compressed_data)
    if not offsets_valid(offsets, len(data)) and len(raw_offsets) >= num_offsets:
        alt_data = struct.pack('<I', raw_offsets[-1]) + compressed_data
        alt_offsets, _ = build_offsets(y_blocks, alt_data)
        if offsets_valid(alt_offsets, len(alt_data)):
            offsets = alt_offsets
            compressed_data = alt_data
            
    decoder.input_data = compressed_data
    decoder.write_alpha = True
    alpha_header = None
    
    if decoder.bpp == 32 and len(offsets) > y_blocks:
        alpha_offset = offsets[y_blocks]
        if 0 <= alpha_offset + 4 <= len(compressed_data):
            alpha_header = struct.unpack('<i', compressed_data[alpha_offset:alpha_offset + 4])[0]
            if alpha_header in (1, 2):
                decoder.has_alpha = True
    
    if alpha_header == 2:
        decoder.write_alpha = False

    pad_skip = ((decoder.width >> 3) + 7) >> 3

    for j in range(y_blocks):
        if j >= len(offsets): break
        mask = None
        if 0 <= offsets[j] and offsets[j] + pad_skip <= len(compressed_data):
            mask = compressed_data[offsets[j] : offsets[j] + pad_skip]
        
        block_offset = offsets[j] + pad_skip
        # Start-only tables end at the frame boundary; an explicit next
        # offset still keeps the final color strip separate from Alpha.
        next_offset = offsets[j + 1] if j + 1 < len(offsets) else len(compressed_data)

        if block_offset < 0 or block_offset >= len(compressed_data): continue
        block_len = next_offset - block_offset
        if block_len <= 0: continue
        
        dst_addr = j * decoder.width * 32
        decoder.unpack_block(block_offset, block_len, dst_addr, mask)

    if alpha_header == 1:
        decoder.unpack_alpha(offsets[y_blocks])

def build_image(decoder, width, height):
    if HAS_OPTIMIZATION:
        arr = decoder.output.reshape((decoder.height, decoder.width, 4))
        cropped = arr[:height, :width, :]
        rgba = cropped[..., [2, 1, 0, 3]].copy()
        img = Image.fromarray(rgba, 'RGBA')
        return img
    else:
        img = Image.frombuffer('RGBA', (decoder.width, decoder.height), decoder.output, 'raw', 'BGRA', 0, 1)
        img = img.crop((0, 0, width, height))
        return img


# Shared by the embedded BM page and headless image/movie services.
__all__ = [name for name in globals() if not name.startswith("__")]
