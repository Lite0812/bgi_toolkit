"""ctypes bindings for the bundled x64 resource accelerator."""
from __future__ import annotations
from .i18n import tr

import ctypes
from functools import lru_cache
from pathlib import Path

from .resource_codec import ResourceError


@lru_cache(maxsize=1)
def library():
    path = Path(__file__).with_name('native') / 'bgi_resources.dll'
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as error:
        raise ResourceError(tr('无法加载图片加速库 {0}；请使用 Windows x64，或运行 native/build_resources.bat 重建：{1}', path, error)) from error
    ptr = ctypes.POINTER(ctypes.c_ubyte)
    lib.bgi_resource_transform.argtypes = [ctypes.c_int, ptr, ctypes.c_size_t,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ptr, ctypes.c_size_t,
        ctypes.POINTER(ptr), ctypes.POINTER(ctypes.c_size_t), ctypes.c_char_p, ctypes.c_size_t]
    lib.bgi_resource_transform.restype = ctypes.c_int
    lib.bgi_resource_free.argtypes = [ctypes.c_void_p]
    lib.bgi_resource_free.restype = None
    return lib


def transform(operation: int, data: bytes, *, width=0, height=0, bpp=0, header=b'') -> bytes:
    lib = library()
    src = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    hdr = (ctypes.c_ubyte * len(header)).from_buffer_copy(header) if header else None
    output = ctypes.POINTER(ctypes.c_ubyte)()
    size = ctypes.c_size_t()
    error = ctypes.create_string_buffer(512)
    result = lib.bgi_resource_transform(operation, src, len(data), width, height, bpp,
        hdr, len(header), ctypes.byref(output), ctypes.byref(size), error, len(error))
    if result:
        raise ResourceError(error.value.decode('utf-8', errors='replace'))
    try:
        return ctypes.string_at(output, size.value)
    finally:
        lib.bgi_resource_free(output)
