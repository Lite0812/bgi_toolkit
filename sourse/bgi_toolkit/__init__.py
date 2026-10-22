"""Unified BGI script toolkit."""

__version__ = "0.1.0"

from .gdb_tool import GdbFormatError, decode_gdb, encode_gdb

__all__ = ["GdbFormatError", "decode_gdb", "encode_gdb", "__version__"]
