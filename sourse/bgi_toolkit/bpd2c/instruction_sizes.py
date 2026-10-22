from __future__ import annotations

import re

from .opcode_encoding_data import FIXED_INSTRUCTION_SIZES, VARIABLE_OPCODES
from ..bp_engine import engine_scope, get_profile


GENERIC_GROUP_OPCODE_RE = re.compile(
    r"^(?:sys0|sys1|sys2|grp1|grp2|grp3|snd1|usr1|usr2|usr3|usr4|userscript)_[0-9A-Fa-f]{2}$"
)


def _parse_int(text: str) -> int:
    return int(text.strip(), 0)


def _sleb_size(value: int) -> int:
    size = 0
    while True:
        byte = value & 0x7F
        value >>= 7
        size += 1
        if (value == 0 and byte & 0x40 == 0) or (value == -1 and byte & 0x40 != 0):
            return size


def _typed_sleb_size(type_text: str, value_text: str) -> int:
    return _sleb_size((_parse_int(value_text) << 2) | (_parse_int(type_text) & 3))


def _list_values(text: str) -> list[int]:
    token = text.strip()
    if not token.startswith("[") or not token.endswith("]"):
        return []
    body = token[1:-1].strip()
    return [] if not body else [_parse_int(item) for item in body.split(",")]


def instruction_size(opcode: str, args: list[str], engine=None) -> int | None:
    if engine is not None:
        with engine_scope(engine):
            return instruction_size(opcode, args)
    if opcode == 'store_keep':
        return 2 if get_profile().historical else None
    if opcode == 'jc_legacy':
        return 2 if not get_profile().compact else None
    if opcode == 'debug_hook':
        return 1 if not get_profile().compact else None
    if get_profile().historical:
        from ..bp_engine import historical_names
        if opcode in historical_names().values():
            return 2
    if opcode == "store_inline":
        from ..asdis.bp.bpop import _inline_payload
        try:
            return 2 + len(_inline_payload(args))
        except ValueError:
            return None
    fixed = FIXED_INSTRUCTION_SIZES.get(opcode)
    if fixed is not None:
        return fixed
    if GENERIC_GROUP_OPCODE_RE.fullmatch(opcode):
        return 2
    op = VARIABLE_OPCODES.get(opcode)
    if op is None:
        return None

    try:
        if op == 0x03:
            mode = _parse_int(args[0]) & 0xFF
            signed_mode = mode if mode < 0x80 else mode - 0x100
            if signed_mode >= 0:
                values = _list_values(args[1])
                return 2 + sum(_sleb_size(value) for value in values)
            width = mode & 3
            return 2 + (1, 2, 4, 8)[width]
        if op == 0x0D:
            return 1 + _typed_sleb_size(args[0], args[1])
        if op == 0x0E:
            return 3 + _typed_sleb_size(args[1], args[2])
        if op == 0x15:
            return 2 if len(args) == 1 else 4
        if op in {0x12, 0x1F, 0x2C, 0x2D, 0x2E, 0x2F}:
            return 1 + _sleb_size(_parse_int(args[0]))
        if op in {0x1A, 0x1B, 0xD5, 0xD8}:
            return 3 + _sleb_size(_parse_int(args[1]))
        if op == 0x36:
            return 2 + _sleb_size(_parse_int(args[1]))
        if op == 0x37:
            return 4 + _sleb_size(_parse_int(args[2]))
        if op == 0x3B:
            return 4
        if op == 0x3F:
            return 2 + _sleb_size(_parse_int(args[1]))
        if op == 0xD6:
            return 3 + _typed_sleb_size(args[1], args[2])
        if op == 0xD7:
            return 5 + _typed_sleb_size(args[2], args[3])
        if op == 0xDA:
            return 4 + _sleb_size(_parse_int(args[2]))
        if op == 0xDB:
            return 4 + _typed_sleb_size(args[2], args[3]) + _sleb_size(_parse_int(args[4]))
        if op == 0xDC:
            return 6 + _sleb_size(_parse_int(args[3]))
        if op == 0xDD:
            return 6 + _typed_sleb_size(args[3], args[4]) + _sleb_size(_parse_int(args[5]))
        if op == 0xDE:
            return 5
        if op == 0xDF:
            return 5 + _typed_sleb_size(args[2], args[3])
        if op == 0xEA:
            return 5 + _sleb_size(_parse_int(args[2]))
        if op == 0xEC:
            return 1 + _sleb_size(_parse_int(args[0]))
        if op == 0xED:
            return 3 + _sleb_size(_parse_int(args[1]))
        if op in {0xE2, 0xE3}:
            return 2 + len(_list_values(args[0])) * 2
        if op in {0xE4, 0xE5, 0xE6, 0xE7}:
            return 3 + _typed_sleb_size(args[1], args[2])
        if op == 0xF0:
            return 7 + _sleb_size(_parse_int(args[2]))
        if op == 0xF1:
            return 5 + _sleb_size(_parse_int(args[2]))
        if op == 0xF2:
            return 7 + _sleb_size(_parse_int(args[3]))
    except (IndexError, ValueError):
        return None
    return None
