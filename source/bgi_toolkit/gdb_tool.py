#!/usr/bin/env python3
"""Convert legacy and BURIKO GDB 3.00 saves to editable semantic JSON and back."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


SDC_MAGIC = b"SDC FORMAT 1.00\0"
GDB_MAGIC = b"BURIKO GDB 3.00\0"
SCHEMA = "ethornell-gdb-json-v1"
LEGACY_SCHEMA = "ethornell-gdb-legacy-json-v1"
FIXED_REGION_LIMIT = 1024
DATA_REGION_LIMIT = 1024 * 1024
MAX_STRING_COUNT = 1 << 20
MAX_NAMED_BIT_ARRAYS = 2048
MAX_UNPACKED_SIZE = 512 * 1024 * 1024


class GdbFormatError(ValueError):
    """Raised when an SDC, GDB, or semantic JSON document is invalid."""


class _Reader:
    def __init__(self, data: bytes, label: str = "GDB payload") -> None:
        self.data = data
        self.position = 0
        self.label = label

    def read(self, size: int) -> bytes:
        end = self.position + size
        if size < 0 or end > len(self.data):
            raise GdbFormatError(
                f"{self.label} truncated at 0x{self.position:x} "
                f"while reading {size} bytes"
            )
        result = self.data[self.position:end]
        self.position = end
        return result

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def c_string(self) -> bytes:
        end = self.data.find(b"\0", self.position)
        if end < 0:
            raise GdbFormatError(
                f"unterminated GDB string at 0x{self.position:x}"
            )
        result = self.data[self.position:end]
        self.position = end + 1
        return result


class _SdcRandom:
    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF

    def next(self) -> int:
        low_product = (20021 * (self.state & 0xFFFF)) & 0xFFFFFFFF
        high = (
            (low_product >> 16)
            + 346 * self.state
            + 20021 * (self.state >> 16)
        ) & 0xFFFFFFFF
        self.state = ((low_product & 0xFFFF) + 1 + (high << 16)) & 0xFFFFFFFF
        return high & 0x7FFF


def _checksum(data: bytes) -> tuple[int, int]:
    total = 0
    xor = 0
    for value in data:
        total = (total + value) & 0xFFFF
        xor ^= value
    return total, xor


def _unpack_sdc(packed: bytes, output_size: int) -> bytes:
    output = bytearray()
    source = 0
    while source < len(packed):
        control = packed[source]
        source += 1
        if control & 0x80 == 0:
            count = control + 1
            end = source + count
            if end > len(packed):
                raise GdbFormatError("SDC literal extends past packed stream")
            if len(output) + count > output_size:
                raise GdbFormatError("SDC literal extends past declared output size")
            output.extend(packed[source:end])
            source = end
            continue

        if source >= len(packed):
            raise GdbFormatError("SDC back-reference misses its offset byte")
        offset_low = packed[source]
        source += 1
        count = ((control >> 3) & 0x0F) + 2
        offset = ((control & 7) << 8) + offset_low + 2
        if offset > len(output) or len(output) + count > output_size:
            raise GdbFormatError(
                "invalid SDC back-reference "
                f"offset={offset} count={count} output={len(output)}"
            )
        for _ in range(count):
            output.append(output[-offset])

    if len(output) != output_size:
        raise GdbFormatError(
            f"SDC output size mismatch: got {len(output)}, expected {output_size}"
        )
    return bytes(output)


def _decode_sdc(data: bytes) -> tuple[bytes, int, bytes]:
    if len(data) < 32 or data[:16] != SDC_MAGIC:
        raise GdbFormatError("missing SDC FORMAT 1.00 header")
    seed, packed_size, output_size, expected_sum, expected_xor = struct.unpack_from(
        "<IIIHH", data, 16
    )
    if output_size > MAX_UNPACKED_SIZE:
        raise GdbFormatError(
            f"refusing to unpack {output_size} bytes (limit {MAX_UNPACKED_SIZE})"
        )
    packed_end = 32 + packed_size
    if packed_end > len(data):
        raise GdbFormatError(
            f"truncated SDC stream: expected {packed_size} packed bytes"
        )
    encrypted = data[32:packed_end]
    actual_sum, actual_xor = _checksum(encrypted)
    if (actual_sum, actual_xor) != (expected_sum, expected_xor):
        raise GdbFormatError(
            "SDC checksum mismatch: "
            f"sum={actual_sum}/{expected_sum}, xor={actual_xor}/{expected_xor}"
        )

    random = _SdcRandom(seed)
    packed = bytes((value - random.next()) & 0xFF for value in encrypted)
    return _unpack_sdc(packed, output_size), seed, data[packed_end:]


def _pack_sdc(payload: bytes) -> bytes:
    """Reproduce BGI's greedy longest-match SDC compressor."""
    chains: dict[bytes, deque[int]] = defaultdict(deque)
    next_position_to_add = 0

    def longest_match(position: int) -> tuple[int, int]:
        nonlocal next_position_to_add
        # Offset 1 is not representable, so only positions <= current - 2 enter.
        while next_position_to_add <= position - 2:
            if next_position_to_add + 1 < len(payload):
                key = payload[next_position_to_add : next_position_to_add + 2]
                chains[key].append(next_position_to_add)
            next_position_to_add += 1

        if position + 1 >= len(payload):
            return 0, 0
        candidates = chains[payload[position : position + 2]]
        cutoff = position - 2049
        while candidates and candidates[0] < cutoff:
            candidates.popleft()

        best_length = 0
        best_offset = 0
        # Nearest-first makes equal-length ties match the original compressor.
        for previous in reversed(candidates):
            offset = position - previous
            length = 2
            while (
                length < 17
                and position + length < len(payload)
                and payload[position + length] == payload[position + length - offset]
            ):
                length += 1
            if length > best_length:
                best_length = length
                best_offset = offset
                if length == 17:
                    break
        return best_length, best_offset

    packed = bytearray()
    position = 0
    while position < len(payload):
        length, offset = longest_match(position)
        if length >= 2:
            encoded_offset = offset - 2
            packed.append(
                0x80 | ((length - 2) << 3) | ((encoded_offset >> 8) & 7)
            )
            packed.append(encoded_offset & 0xFF)
            position += length
            continue

        literal_start = position
        position += 1
        while position - literal_start < 128 and position < len(payload):
            next_length, _ = longest_match(position)
            if next_length >= 2:
                break
            position += 1
        packed.append(position - literal_start - 1)
        packed.extend(payload[literal_start:position])
    return bytes(packed)


def _encode_sdc(payload: bytes, seed: int, trailing_data: bytes) -> bytes:
    packed = _pack_sdc(payload)
    random = _SdcRandom(seed)
    encrypted = bytes((value + random.next()) & 0xFF for value in packed)
    total, xor = _checksum(encrypted)
    header = SDC_MAGIC + struct.pack(
        "<IIIHH", seed, len(encrypted), len(payload), total, xor
    )
    return header + encrypted + trailing_data


def _decode_text(raw: bytes) -> str | dict[str, str]:
    try:
        text = raw.decode("cp932")
        if text.encode("cp932") == raw:
            return text
    except UnicodeError:
        pass
    return {"display": raw.decode("cp932", errors="replace"), "encoded_bytes": list(raw)}


def _encode_text(value: Any, path: str) -> bytes:
    if isinstance(value, str):
        if "\0" in value:
            raise GdbFormatError(f"{path} contains a NUL character")
        try:
            return value.encode("cp932")
        except UnicodeEncodeError as error:
            raise GdbFormatError(f"{path} is not encodable as CP932: {error}") from error
    mapping = _mapping(value, path)
    _keys(mapping, {"display"}, {"raw_hex", "encoded_bytes"}, path)
    if not isinstance(mapping["display"], str):
        raise GdbFormatError(f"{path}.display must be a string")
    if ("encoded_bytes" in mapping) == ("raw_hex" in mapping):
        raise GdbFormatError(f"{path} must contain exactly one byte representation")
    raw = _byte_array(mapping["encoded_bytes"], f"{path}.encoded_bytes") if "encoded_bytes" in mapping else _hex_bytes(mapping["raw_hex"], f"{path}.raw_hex")
    if b"\0" in raw:
        raise GdbFormatError(f"{path} encoded bytes contain a NUL byte")
    return raw


def _sparse_region(data: bytes) -> dict[str, Any]:
    ranges: list[dict[str, Any]] = []
    position = 0
    while position < len(data):
        if data[position] == 0:
            position += 1
            continue
        start = position
        while position < len(data) and data[position] != 0:
            position += 1
        chunk = data[start:position]
        item: dict[str, Any] = {"offset": start}
        if len(chunk) >= 3:
            for field, encoding in (("text_utf8", "utf-8"), ("text_cp932", "cp932")):
                try:
                    text = chunk.decode(encoding)
                except UnicodeDecodeError:
                    continue
                if text.isprintable() and text.encode(encoding) == chunk:
                    item[field] = text
                    break
        if len(item) == 1:
            item["byte_values"] = list(chunk)
        ranges.append(item)
    return {"size": len(data), "nonzero_ranges": ranges}


def _set_ranges(data: bytes, bit_length: int) -> list[list[int]]:
    ranges: list[list[int]] = []
    position = 0
    while position < bit_length:
        if data[position // 8] & (1 << (position & 7)) == 0:
            position += 1
            continue
        start = position
        position += 1
        while (
            position < bit_length
            and data[position // 8] & (1 << (position & 7)) != 0
        ):
            position += 1
        ranges.append([start, position])
    return ranges


def _parse_gdb_payload(
    payload: bytes, seed: int, sdc_trailing_data: bytes
) -> dict[str, Any]:
    reader = _Reader(payload)
    if reader.read(16) != GDB_MAGIC:
        raise GdbFormatError("missing BURIKO GDB 3.00 header")
    declared_size = reader.u32()
    if declared_size < reader.position or declared_size > len(payload):
        raise GdbFormatError(
            f"invalid GDB declared size {declared_size} for {len(payload)} bytes"
        )
    x = reader.i32()
    y = reader.i32()

    fixed_size = reader.u32()
    if fixed_size > FIXED_REGION_LIMIT:
        raise GdbFormatError(f"GDB fixed region too large: {fixed_size}")
    fixed_region = reader.read(fixed_size)

    data_size = reader.u32()
    if data_size > DATA_REGION_LIMIT:
        raise GdbFormatError(f"GDB data region too large: {data_size}")
    data_region = reader.read(data_size)

    string_count = reader.u32()
    if string_count > MAX_STRING_COUNT:
        raise GdbFormatError(f"GDB string count too large: {string_count}")
    strings = [_decode_text(reader.c_string()) for _ in range(string_count)]

    named_count = reader.u32()
    if named_count > MAX_NAMED_BIT_ARRAYS:
        raise GdbFormatError(f"GDB named bit array count too large: {named_count}")
    named_bits: list[dict[str, Any]] = []
    for _ in range(named_count):
        name = _decode_text(reader.c_string())
        bit_length = reader.u32()
        if bit_length == 0:
            raise GdbFormatError("named bit array has zero bit length")
        byte_length = (bit_length + 7) // 8
        data = reader.read(byte_length)
        remainder = bit_length & 7
        unused_tail_bits = data[-1] >> remainder if remainder else 0
        named_bits.append(
            {
                "name": name,
                "bit_length": bit_length,
                "set_ranges": _set_ranges(data, bit_length),
                "unused_tail_bits": unused_tail_bits,
            }
        )

    return {
        "_schema": SCHEMA,
        "sdc": {
            "seed": seed,
            "trailing_bytes": list(sdc_trailing_data),
        },
        "gdb": {
            "declared_size_adjustment": declared_size - len(payload),
            "trailing_bytes": list(payload[reader.position :]),
        },
        "window_position": {"x": x, "y": y},
        "fixed_region": _sparse_region(fixed_region),
        "data_region": _sparse_region(data_region),
        "strings": strings,
        "named_bit_arrays": named_bits,
    }


def decode_gdb(data: bytes) -> dict[str, Any]:
    """Decode a legacy or BURIKO GDB 3.00 save into a semantic document."""
    payload, seed, trailing_data = _decode_sdc(data)
    if payload.startswith(GDB_MAGIC):
        return _parse_gdb_payload(payload, seed, trailing_data)
    from .gdb_legacy import parse_legacy_gdb
    return parse_legacy_gdb(payload, seed, trailing_data)


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GdbFormatError(f"{path} must be an object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise GdbFormatError(f"{path} must be an array")
    return value


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GdbFormatError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise GdbFormatError(f"{path} must be in range {minimum}..{maximum}")
    return value


def _keys(
    value: dict[str, Any], required: set[str], optional: set[str], path: str
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise GdbFormatError(f"{path} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise GdbFormatError(f"{path} has unknown fields: {', '.join(sorted(unknown))}")


def _hex_bytes(value: Any, path: str) -> bytes:
    if not isinstance(value, str):
        raise GdbFormatError(f"{path} must be a hexadecimal string")
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise GdbFormatError(f"{path} is not valid hexadecimal: {error}") from error


def _byte_array(value: Any, path: str) -> bytes:
    return bytes(_integer(item, f'{path}[{index}]', 0, 255)
        for index, item in enumerate(_list(value, path)))


def _trailing_bytes(mapping: dict, path: str) -> bytes:
    if 'trailing_bytes' in mapping and 'trailing_data_hex' in mapping:
        raise GdbFormatError(f'{path} has conflicting trailing-data representations')
    if 'trailing_bytes' in mapping:
        return _byte_array(mapping['trailing_bytes'], f'{path}.trailing_bytes')
    return _hex_bytes(mapping.get('trailing_data_hex',''), f'{path}.trailing_data_hex')


def _build_region(value: Any, path: str, size_limit: int) -> bytes:
    mapping = _mapping(value, path)
    _keys(mapping, {"size", "nonzero_ranges"}, set(), path)
    size = _integer(mapping["size"], f"{path}.size", 0, size_limit)
    output = bytearray(size)
    previous_end = 0
    for index, item in enumerate(_list(mapping["nonzero_ranges"], f"{path}.nonzero_ranges")):
        item_path = f"{path}.nonzero_ranges[{index}]"
        item_mapping = _mapping(item, item_path)
        _keys(
            item_mapping,
            {"offset"},
            {"hex", "byte_values", "text_utf8", "text_cp932"},
            item_path,
        )
        offset = _integer(item_mapping["offset"], f"{item_path}.offset", 0, size)
        representations = [
            field
            for field in ("hex", "byte_values", "text_utf8", "text_cp932")
            if field in item_mapping
        ]
        if len(representations) != 1:
            raise GdbFormatError(
                f"{item_path} must contain exactly one of byte_values, text_utf8, text_cp932 (legacy hex is also accepted)"
            )
        representation = representations[0]
        if representation == "byte_values":
            chunk = _byte_array(item_mapping["byte_values"], f"{item_path}.byte_values")
        elif representation == "hex":
            chunk = _hex_bytes(item_mapping["hex"], f"{item_path}.hex")
        else:
            text = item_mapping[representation]
            if not isinstance(text, str):
                raise GdbFormatError(f"{item_path}.{representation} must be a string")
            encoding = "utf-8" if representation == "text_utf8" else "cp932"
            try:
                chunk = text.encode(encoding)
            except UnicodeEncodeError as error:
                raise GdbFormatError(
                    f"{item_path}.{representation} cannot be encoded: {error}"
                ) from error
        end = offset + len(chunk)
        if not chunk:
            raise GdbFormatError(f"{item_path} data must not be empty")
        if offset < previous_end:
            raise GdbFormatError(f"{item_path} overlaps or is out of order")
        if end > size:
            raise GdbFormatError(f"{item_path} extends past region size {size}")
        output[offset:end] = chunk
        previous_end = end
    return bytes(output)


def _build_named_bits(value: Any, path: str) -> tuple[bytes, int, bytes]:
    mapping = _mapping(value, path)
    _keys(
        mapping,
        {"name", "bit_length", "set_ranges", "unused_tail_bits"},
        set(),
        path,
    )
    name = _encode_text(mapping["name"], f"{path}.name")
    bit_length = _integer(mapping["bit_length"], f"{path}.bit_length", 1, 0xFFFFFFFF)
    byte_length = (bit_length + 7) // 8
    if byte_length > MAX_UNPACKED_SIZE:
        raise GdbFormatError(f"{path} is too large")
    output = bytearray(byte_length)
    previous_end = 0
    for index, item in enumerate(_list(mapping["set_ranges"], f"{path}.set_ranges")):
        item_path = f"{path}.set_ranges[{index}]"
        pair = _list(item, item_path)
        if len(pair) != 2:
            raise GdbFormatError(f"{item_path} must contain [start, end_exclusive]")
        start = _integer(pair[0], f"{item_path}[0]", 0, bit_length)
        end = _integer(pair[1], f"{item_path}[1]", 0, bit_length)
        if start >= end:
            raise GdbFormatError(f"{item_path} must have start < end_exclusive")
        if start < previous_end:
            raise GdbFormatError(f"{item_path} overlaps or is out of order")
        for bit in range(start, end):
            output[bit // 8] |= 1 << (bit & 7)
        previous_end = end

    remainder = bit_length & 7
    max_tail = (1 << (8 - remainder)) - 1 if remainder else 0
    tail = _integer(
        mapping["unused_tail_bits"], f"{path}.unused_tail_bits", 0, max_tail
    )
    if remainder:
        output[-1] |= tail << remainder
    return name, bit_length, bytes(output)


def _build_gdb_payload(document: Any) -> tuple[bytes, int, bytes]:
    root = _mapping(document, "root")
    _keys(
        root,
        {
            "_schema",
            "sdc",
            "gdb",
            "window_position",
            "fixed_region",
            "data_region",
            "strings",
            "named_bit_arrays",
        },
        set(),
        "root",
    )
    if root["_schema"] != SCHEMA:
        raise GdbFormatError(f"unsupported schema: {root['_schema']!r}")

    sdc = _mapping(root["sdc"], "sdc")
    _keys(sdc, {"seed"}, {"trailing_bytes", "trailing_data_hex"}, "sdc")
    seed = _integer(sdc["seed"], "sdc.seed", 0, 0xFFFFFFFF)
    sdc_trailing = _trailing_bytes(sdc, "sdc")

    gdb = _mapping(root["gdb"], "gdb")
    _keys(gdb, {"declared_size_adjustment"}, {"trailing_bytes", "trailing_data_hex"}, "gdb")
    adjustment = _integer(
        gdb["declared_size_adjustment"],
        "gdb.declared_size_adjustment",
        -MAX_UNPACKED_SIZE,
        0,
    )
    payload_trailing = _trailing_bytes(gdb, "gdb")

    window = _mapping(root["window_position"], "window_position")
    _keys(window, {"x", "y"}, set(), "window_position")
    x = _integer(window["x"], "window_position.x", -0x80000000, 0x7FFFFFFF)
    y = _integer(window["y"], "window_position.y", -0x80000000, 0x7FFFFFFF)

    fixed = _build_region(root["fixed_region"], "fixed_region", FIXED_REGION_LIMIT)
    data_region = _build_region(root["data_region"], "data_region", DATA_REGION_LIMIT)
    strings = _list(root["strings"], "strings")
    if len(strings) > MAX_STRING_COUNT:
        raise GdbFormatError(f"too many strings: {len(strings)}")
    encoded_strings = [
        _encode_text(value, f"strings[{index}]") for index, value in enumerate(strings)
    ]

    named_items = _list(root["named_bit_arrays"], "named_bit_arrays")
    if len(named_items) > MAX_NAMED_BIT_ARRAYS:
        raise GdbFormatError(f"too many named bit arrays: {len(named_items)}")
    named_bits = [
        _build_named_bits(value, f"named_bit_arrays[{index}]")
        for index, value in enumerate(named_items)
    ]

    payload = bytearray(GDB_MAGIC)
    payload.extend(b"\0\0\0\0")
    payload.extend(struct.pack("<iiI", x, y, len(fixed)))
    payload.extend(fixed)
    payload.extend(struct.pack("<I", len(data_region)))
    payload.extend(data_region)
    payload.extend(struct.pack("<I", len(encoded_strings)))
    for value in encoded_strings:
        payload.extend(value)
        payload.append(0)
    payload.extend(struct.pack("<I", len(named_bits)))
    for name, bit_length, bits in named_bits:
        payload.extend(name)
        payload.append(0)
        payload.extend(struct.pack("<I", bit_length))
        payload.extend(bits)
    payload.extend(payload_trailing)

    declared_size = len(payload) + adjustment
    if declared_size < 20 or declared_size > len(payload):
        raise GdbFormatError(
            f"computed GDB declared size {declared_size} is invalid for {len(payload)} bytes"
        )
    struct.pack_into("<I", payload, 16, declared_size)
    return bytes(payload), seed, sdc_trailing


def encode_gdb(document: Any) -> bytes:
    """Rebuild the document in its original GDB layout and SDC wrapper."""
    if isinstance(document, dict) and document.get("_schema") == LEGACY_SCHEMA:
        from .gdb_legacy import build_legacy_gdb
        payload, seed, trailing_data = build_legacy_gdb(document)
    else:
        payload, seed, trailing_data = _build_gdb_payload(document)
    if len(payload) > MAX_UNPACKED_SIZE:
        raise GdbFormatError(f"GDB payload is too large: {len(payload)}")
    return _encode_sdc(payload, seed, trailing_data)


def _default_decode_output(input_path: Path) -> Path:
    return input_path.with_name(input_path.name + ".json")


def _default_encode_output(input_path: Path) -> Path:
    name = input_path.name
    if name.lower().endswith(".gdb.json"):
        name = name[:-9] + ".rebuilt.gdb"
    else:
        name = input_path.stem + ".gdb"
    return input_path.with_name(name)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GdbFormatError(f"cannot read {path}: {error}") from error


def _compact_json_value(value: Any) -> bool:
    if not isinstance(value, (dict, list)):
        return True
    if isinstance(value, dict):
        return all(not isinstance(item, (dict, list)) for item in value.values())
    return all(not isinstance(item, (dict, list)) for item in value) or all(
        isinstance(item, list)
        and all(not isinstance(part, (dict, list)) for part in item)
        for item in value
    )


def _pretty_json(value: Any, level: int = 0, width: int = 120) -> str:
    """Pretty-print JSON while keeping small semantic records on one line."""
    indent = "  " * level
    compact = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    if _compact_json_value(value) and len(indent) + len(compact) <= width:
        return indent + compact

    if isinstance(value, dict):
        if not value:
            return indent + "{}"
        lines = [indent + "{"]
        child_indent = "  " * (level + 1)
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            child = _pretty_json(item, level + 1, width).splitlines()
            key_text = json.dumps(key, ensure_ascii=False) + ": "
            child[0] = child_indent + key_text + child[0][len(child_indent) :]
            if index + 1 < len(items):
                child[-1] += ","
            lines.extend(child)
        lines.append(indent + "}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return indent + "[]"
        if all(
            isinstance(item, list)
            and item
            and all(not isinstance(part, (dict, list)) for part in item)
            for item in value
        ):
            lines = [indent + "["]
            child_indent = "  " * (level + 1)
            current = child_indent
            for index, item in enumerate(value):
                token = json.dumps(item, ensure_ascii=False, separators=(", ", ": "))
                if index + 1 < len(value):
                    token += ","
                separator = "" if current == child_indent else " "
                if len(current) + len(separator) + len(token) > width:
                    lines.append(current)
                    current = child_indent + token
                else:
                    current += separator + token
            lines.append(current)
            lines.append(indent + "]")
            return "\n".join(lines)
        lines = [indent + "["]
        for index, item in enumerate(value):
            child = _pretty_json(item, level + 1, width).splitlines()
            if index + 1 < len(value):
                child[-1] += ","
            lines.extend(child)
        lines.append(indent + "]")
        return "\n".join(lines)

    return indent + compact


def _write_json(path: Path, document: Any) -> None:
    text = _pretty_json(document) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _summary(document: dict[str, Any], file_size: int | None = None) -> str:
    named = document["named_bit_arrays"]
    lines = []
    if file_size is not None:
        lines.append(f"file_size: {file_size}")
    lines.extend(
        [
            f"schema: {document['_schema']}",
            f"layout: {document['gdb'].get('layout', 'BURIKO GDB 3.00')}",
            f"sdc_seed: {document['sdc']['seed']}",
            "window_position: "
            f"({document['window_position']['x']}, {document['window_position']['y']})",
            f"fixed_region_size: {document['fixed_region']['size']}",
            f"data_region_size: {document['data_region']['size']}",
            f"strings: {len(document['strings'])}",
            f"named_bit_arrays: {len(named)}",
        ]
    )
    lines.extend(
        f"  {item['name']!s}: {item['bit_length']} bits, "
        f"{len(item['set_ranges'])} set ranges"
        for item in named
    )
    return "\n".join(lines)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert legacy fixed-layout and BURIKO GDB 3.00 saves to JSON and back."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    decode_parser = subparsers.add_parser("decode", help="convert GDB to semantic JSON")
    decode_parser.add_argument("input", type=Path)
    decode_parser.add_argument("output", type=Path, nargs="?")

    encode_parser = subparsers.add_parser("encode", help="convert semantic JSON to GDB")
    encode_parser.add_argument("input", type=Path)
    encode_parser.add_argument("output", type=Path, nargs="?")

    inspect_parser = subparsers.add_parser("inspect", help="show a concise GDB summary")
    inspect_parser.add_argument("input", type=Path)

    verify_parser = subparsers.add_parser(
        "verify", help="decode and encode, then require byte-identical output"
    )
    verify_parser.add_argument("input", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "decode":
            source = args.input.read_bytes()
            document = decode_gdb(source)
            output = args.output or _default_decode_output(args.input)
            _write_json(output, document)
            print(f"decoded {args.input} -> {output}")
            print(_summary(document, len(source)))
            return 0

        if args.command == "encode":
            document = _load_json(args.input)
            encoded = encode_gdb(document)
            output = args.output or _default_encode_output(args.input)
            output.write_bytes(encoded)
            print(f"encoded {args.input} -> {output} ({len(encoded)} bytes)")
            return 0

        source = args.input.read_bytes()
        document = decode_gdb(source)
        if args.command == "inspect":
            print(_summary(document, len(source)))
            return 0

        rebuilt = encode_gdb(document)
        if rebuilt != source:
            difference = next(
                (index for index, pair in enumerate(zip(source, rebuilt)) if pair[0] != pair[1]),
                min(len(source), len(rebuilt)),
            )
            raise GdbFormatError(
                "round trip is not byte-identical: "
                f"original={len(source)} bytes rebuilt={len(rebuilt)} bytes "
                f"first_difference=0x{difference:x}"
            )
        print(f"OK: byte-identical round trip ({len(source)} bytes)")
        return 0
    except (GdbFormatError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
