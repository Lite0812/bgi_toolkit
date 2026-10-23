"""Semantic CSV conversion for Buriko animation definition binaries.

The CSV is intentionally a wide, one-record-per-row format. A short comment
block at the top stores file-level metadata once; the table itself contains
only an index, text values, and signed 32-bit parameters. The fixed binary
header is rebuilt from the metadata when importing the CSV.

The compiler format stores strings as CP932 (or another explicitly selected
single-byte encoding) with a two-byte length prefix. A string must therefore
decode and encode cleanly with the selected encoding for a semantic CSV
round-trip. Unsupported opaque bytes are rejected instead of being hidden in
an implementation-detail column.
"""

from __future__ import annotations

import argparse
import csv
import io
import struct
from dataclasses import dataclass
from pathlib import Path
from .text_encoding import normalize_encoding

HEADER_SIZE = 64
VERSION_OFFSET = 32
MODE_OFFSET = 36
COUNT_OFFSET = 40
RESERVED_OFFSET = 44
RESERVED_WORD_COUNT = 5
STRING_ENCODING = "cp932"
CSV_FORMAT = "anmdef-csv-v3"
DEFINITION_MAGIC = "BurikoAnimationDefinition"
SET_MAGIC = "BurikoAnimationDefinitionSet"
METADATA_FIELDS = (
    "file_type",
    "version_major",
    "version_minor",
    "mode",
    "record_count",
    "encoding",
    *(f"reserved_word_{i:02d}" for i in range(1, RESERVED_WORD_COUNT + 1)),
)
REQUIRED_METADATA_FIELDS = METADATA_FIELDS[:6]
DEFINITION_TEXT_FIELDS = ("definition_key", "animation_name", "animation_option")
DEFINITION_PARAMETER_FIELDS = tuple(f"parameter_{i:02d}" for i in range(1, 41))


class AnimationFormatError(ValueError):
    """Raised when a binary or CSV animation definition is malformed."""


@dataclass
class BinaryString:
    """A length-prefixed string payload."""

    raw: bytes

    def text(self, encoding: str) -> str:
        return self.raw.decode(encoding)


@dataclass
class DefinitionRecord:
    strings: list[BinaryString]
    integers: list[int]


@dataclass
class DefinitionSetRecord:
    strings: list[BinaryString]


@dataclass
class AnimationDocument:
    kind: str
    header: bytes
    mode: int
    records: list[DefinitionRecord | DefinitionSetRecord]
    tail: bytes = b""
    encoding: str = STRING_ENCODING


def _checked_encoding(encoding: str) -> str:
    encoding = normalize_encoding(encoding)
    try:
        "".encode(encoding)
    except LookupError as exc:
        raise AnimationFormatError(f"unknown text encoding: {encoding}") from exc
    return encoding


def _version_parts(header: bytes) -> tuple[int, int]:
    version_word = struct.unpack_from("<I", header, VERSION_OFFSET)[0]
    return version_word >> 16, version_word & 0xFFFF


def _make_header(
    kind: str,
    version_major: int,
    version_minor: int,
    mode: int,
    count: int,
    reserved_words: tuple[int, ...] = (0,) * RESERVED_WORD_COUNT,
) -> bytes:
    if kind not in (DEFINITION_MAGIC, SET_MAGIC):
        raise AnimationFormatError(f"unsupported animation definition type: {kind!r}")
    for name, value in (
        ("version_major", version_major),
        ("version_minor", version_minor),
        ("mode", mode),
        ("record_count", count),
    ):
        if not 0 <= value <= 0xFFFFFFFF:
            raise AnimationFormatError(f"{name} is outside uint32 range")
    if version_major > 0xFFFF or version_minor > 0xFFFF:
        raise AnimationFormatError("version components must fit in uint16")
    if len(reserved_words) != RESERVED_WORD_COUNT:
        raise AnimationFormatError(f"header must have {RESERVED_WORD_COUNT} reserved words")
    if any(not 0 <= value <= 0xFFFFFFFF for value in reserved_words):
        raise AnimationFormatError("reserved header words must fit in uint32")
    magic = kind.encode("ascii")
    if len(magic) > 32:
        raise AnimationFormatError("animation type name is longer than the header field")
    header = bytearray(HEADER_SIZE)
    header[: len(magic)] = magic
    struct.pack_into("<I", header, VERSION_OFFSET, (version_major << 16) | version_minor)
    struct.pack_into("<I", header, MODE_OFFSET, mode)
    struct.pack_into("<I", header, COUNT_OFFSET, count)
    struct.pack_into("<5I", header, RESERVED_OFFSET, *reserved_words)
    return bytes(header)


def _read_lp_string(data: bytes, cursor: int) -> tuple[BinaryString, int]:
    if cursor + 2 > len(data):
        raise AnimationFormatError(f"missing string length at byte {cursor}")
    length = struct.unpack_from("<H", data, cursor)[0]
    cursor += 2
    end = cursor + length
    if end > len(data):
        raise AnimationFormatError(
            f"string at byte {cursor - 2} exceeds file size (length={length})"
        )
    return BinaryString(data[cursor:end]), end


def _write_lp_string(field: BinaryString) -> bytes:
    if len(field.raw) > 0xFFFF:
        raise AnimationFormatError("string is too long for a uint16 length prefix")
    return struct.pack("<H", len(field.raw)) + field.raw


def _validate_semantic_string(field: BinaryString, encoding: str, label: str) -> None:
    try:
        text = field.raw.decode(encoding)
        encoded = text.encode(encoding)
    except UnicodeError as exc:
        raise AnimationFormatError(
            f"{label} cannot be represented with {encoding}; semantic CSV cannot preserve it"
        ) from exc
    if encoded != field.raw:
        raise AnimationFormatError(
            f"{label} is not canonical {encoding} text; semantic CSV cannot preserve it"
        )


def parse_binary(source: bytes | bytearray | memoryview, *, encoding: str = STRING_ENCODING) -> AnimationDocument:
    """Parse one compiled animation-definition binary."""

    encoding = _checked_encoding(encoding)
    data = bytes(source)
    if len(data) < HEADER_SIZE:
        raise AnimationFormatError(f"file is shorter than the {HEADER_SIZE}-byte header")

    header = data[:HEADER_SIZE]
    magic_bytes = header[:32].split(b"\0", 1)[0]
    try:
        kind = magic_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AnimationFormatError("header type is not ASCII") from exc
    if kind not in (DEFINITION_MAGIC, SET_MAGIC):
        raise AnimationFormatError(f"unsupported animation definition type: {kind!r}")

    mode = struct.unpack_from("<I", header, MODE_OFFSET)[0]
    count = struct.unpack_from("<I", header, COUNT_OFFSET)[0]
    cursor = HEADER_SIZE
    records: list[DefinitionRecord | DefinitionSetRecord] = []

    if kind == DEFINITION_MAGIC:
        for index in range(count):
            strings: list[BinaryString] = []
            for field_name in DEFINITION_TEXT_FIELDS:
                field, cursor = _read_lp_string(data, cursor)
                _validate_semantic_string(field, encoding, f"record {index} {field_name}")
                strings.append(field)
            end = cursor + 40 * 4
            if end > len(data):
                raise AnimationFormatError(
                    f"definition record {index} exceeds file size at byte {cursor}"
                )
            integers = list(struct.unpack_from("<40i", data, cursor))
            cursor = end
            records.append(DefinitionRecord(strings, integers))
    else:
        string_count = 16 if mode == 0 else 20
        for index in range(count):
            strings = []
            for field_number in range(1 + string_count):
                field, cursor = _read_lp_string(data, cursor)
                label = "set_key" if field_number == 0 else f"slot_{field_number:02d}"
                _validate_semantic_string(field, encoding, f"record {index} {label}")
                strings.append(field)
            records.append(DefinitionSetRecord(strings))

    return AnimationDocument(kind, header, mode, records, data[cursor:], encoding)


def build_binary(document: AnimationDocument) -> bytes:
    """Serialize an :class:`AnimationDocument` back to its binary format."""

    if document.kind not in (DEFINITION_MAGIC, SET_MAGIC):
        raise AnimationFormatError(f"unsupported animation definition type: {document.kind!r}")
    if len(document.header) != HEADER_SIZE:
        raise AnimationFormatError("header must contain exactly 64 bytes")
    if document.kind == DEFINITION_MAGIC and any(
        not isinstance(record, DefinitionRecord) for record in document.records
    ):
        raise AnimationFormatError("definition document contains a set record")
    if document.kind == SET_MAGIC and any(
        not isinstance(record, DefinitionSetRecord) for record in document.records
    ):
        raise AnimationFormatError("definition-set document contains a definition record")

    header = bytearray(document.header)
    expected_magic = document.kind.encode("ascii")
    if header[:32].split(b"\0", 1)[0] != expected_magic:
        raise AnimationFormatError("header type does not match document kind")
    struct.pack_into("<I", header, MODE_OFFSET, document.mode)
    struct.pack_into("<I", header, COUNT_OFFSET, len(document.records))

    output = bytearray(header)
    if document.kind == DEFINITION_MAGIC:
        for index, record in enumerate(document.records):
            if len(record.strings) != 3 or len(record.integers) != 40:
                raise AnimationFormatError(
                    f"definition record {index} must have 3 strings and 40 integers"
                )
            for field_number, field in enumerate(record.strings, start=1):
                _validate_semantic_string(field, document.encoding, f"record {index} text {field_number}")
                output.extend(_write_lp_string(field))
            try:
                output.extend(struct.pack("<40i", *record.integers))
            except struct.error as exc:
                raise AnimationFormatError(f"definition record {index} contains an int32 overflow") from exc
    else:
        string_count = 16 if document.mode == 0 else 20
        for index, record in enumerate(document.records):
            if len(record.strings) != 1 + string_count:
                raise AnimationFormatError(
                    f"definition-set record {index} must have {1 + string_count} strings"
                )
            for field_number, field in enumerate(record.strings):
                label = "set_key" if field_number == 0 else f"slot_{field_number:02d}"
                _validate_semantic_string(field, document.encoding, f"record {index} {label}")
                output.extend(_write_lp_string(field))
    output.extend(document.tail)
    return bytes(output)


def _fields_for_kind(kind: str, mode: int) -> tuple[str, ...]:
    if kind == DEFINITION_MAGIC:
        return ("index",) + DEFINITION_TEXT_FIELDS + DEFINITION_PARAMETER_FIELDS
    if kind == SET_MAGIC:
        slot_count = 16 if mode == 0 else 20
        return ("index", "set_key") + tuple(f"slot_{i:02d}" for i in range(1, slot_count + 1))
    raise AnimationFormatError(f"unsupported animation definition type: {kind!r}")


def _metadata(document: AnimationDocument) -> dict[str, str]:
    version_major, version_minor = _version_parts(document.header)
    metadata = {
        "file_type": document.kind,
        "version_major": str(version_major),
        "version_minor": str(version_minor),
        "mode": str(document.mode),
        "record_count": str(len(document.records)),
        "encoding": document.encoding,
    }
    metadata.update(
        {
            field_name: str(value)
            for field_name, value in zip(
                (f"reserved_word_{i:02d}" for i in range(1, RESERVED_WORD_COUNT + 1)),
                struct.unpack_from("<5I", document.header, RESERVED_OFFSET),
            )
        }
    )
    return metadata


def _write_rows(document: AnimationDocument, writer: csv.DictWriter) -> None:
    if document.tail:
        raise AnimationFormatError(
            "binary contains trailing data; this semantic CSV format has no opaque byte column"
        )
    if not document.records:
        raise AnimationFormatError("cannot infer a semantic CSV schema from a file with no records")
    for index, record in enumerate(document.records):
        row = {"index": str(index)}
        if isinstance(record, DefinitionRecord):
            row.update(
                {
                    field_name: field.text(document.encoding)
                    for field_name, field in zip(DEFINITION_TEXT_FIELDS, record.strings)
                }
            )
            row.update(
                {
                    field_name: str(value)
                    for field_name, value in zip(DEFINITION_PARAMETER_FIELDS, record.integers)
                }
            )
        else:
            row.update(
                {
                    field_name: field.text(document.encoding)
                    for field_name, field in zip(
                        ("set_key",) + tuple(f"slot_{i:02d}" for i in range(1, len(record.strings))),
                        record.strings,
                    )
                }
            )
        writer.writerow(row)


def binary_to_csv(source: str | Path, destination: str | Path, *, encoding: str = STRING_ENCODING) -> AnimationDocument:
    """Convert a compiled binary to a UTF-8 semantic CSV file."""

    source_path = Path(source)
    destination_path = Path(destination)
    document = parse_binary(source_path.read_bytes(), encoding=encoding)
    fields = _fields_for_kind(document.kind, document.mode)
    with destination_path.open("w", encoding="utf-8-sig", newline="") as stream:
        stream.write(f"# {CSV_FORMAT}\n")
        for key, value in _metadata(document).items():
            stream.write(f"# {key}={value}\n")
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        _write_rows(document, writer)
    return document


def _read_csv_rows(stream) -> tuple[dict[str, str], tuple[str, ...], list[dict[str, str]]]:
    metadata: dict[str, str] = {}
    csv_lines: list[str] = []
    saw_data = False
    for line_number, line in enumerate(stream, start=1):
        stripped = line.strip()
        if not saw_data and not stripped:
            continue
        if not saw_data and stripped.startswith("#"):
            marker = stripped[1:].strip()
            if marker == CSV_FORMAT:
                if "format" in metadata:
                    raise AnimationFormatError(f"duplicate format marker at line {line_number}")
                metadata["format"] = CSV_FORMAT
                continue
            if "=" not in marker:
                raise AnimationFormatError(f"invalid metadata comment at line {line_number}")
            key, value = marker.split("=", 1)
            key = key.strip()
            if key not in METADATA_FIELDS or key in metadata:
                raise AnimationFormatError(f"invalid or duplicate metadata at line {line_number}")
            metadata[key] = value.strip()
            continue
        saw_data = True
        csv_lines.append(line)

    reader = csv.DictReader(io.StringIO("".join(csv_lines)))
    fieldnames = tuple(reader.fieldnames or ())
    if not fieldnames:
        raise AnimationFormatError("CSV has no header row")
    rows = [dict(row) for row in reader]
    if not rows:
        raise AnimationFormatError("CSV must contain at least one record row")
    if any(value is None for row in rows for value in row.values()):
        raise AnimationFormatError("CSV contains a row with too few columns")
    return metadata, fieldnames, rows


def _parse_uint(value: str, label: str, maximum: int = 0xFFFFFFFF) -> int:
    try:
        result = int(value.strip(), 10)
    except ValueError as exc:
        raise AnimationFormatError(f"{label} must be a decimal integer") from exc
    if not 0 <= result <= maximum:
        raise AnimationFormatError(f"{label} is outside the allowed range")
    return result


def _parse_int32(value: str, label: str) -> int:
    try:
        result = int(value.strip(), 10)
    except ValueError as exc:
        raise AnimationFormatError(f"{label} must be a decimal integer") from exc
    if not -0x80000000 <= result <= 0x7FFFFFFF:
        raise AnimationFormatError(f"{label} is outside signed int32 range")
    return result


def _string_from_text(value: str, encoding: str, label: str) -> BinaryString:
    try:
        raw = value.encode(encoding)
    except UnicodeEncodeError as exc:
        raise AnimationFormatError(f"{label} cannot be encoded as {encoding}") from exc
    return BinaryString(raw)


def _parse_document_rows(
    metadata: dict[str, str], fieldnames: tuple[str, ...], rows: list[dict[str, str]]
) -> AnimationDocument:
    required_metadata = {"format", *REQUIRED_METADATA_FIELDS}
    missing = ", ".join(sorted(required_metadata - set(metadata)))
    if missing:
        raise AnimationFormatError(f"CSV is missing metadata comments: {missing}")
    if metadata["format"] != CSV_FORMAT:
        raise AnimationFormatError(f"unsupported CSV format marker (expected {CSV_FORMAT})")

    kind = metadata["file_type"].strip()
    mode = _parse_uint(metadata["mode"], "mode")
    expected_fields = _fields_for_kind(kind, mode)
    if tuple(fieldnames) != expected_fields:
        raise AnimationFormatError(
            "CSV columns do not match the selected file type and mode; expected: "
            + ", ".join(expected_fields)
        )

    version_major = _parse_uint(metadata["version_major"], "version_major", 0xFFFF)
    version_minor = _parse_uint(metadata["version_minor"], "version_minor", 0xFFFF)
    record_count = _parse_uint(metadata["record_count"], "record_count")
    encoding = _checked_encoding(metadata["encoding"].strip() or STRING_ENCODING)
    reserved_words = tuple(
        _parse_uint(metadata.get(field_name, "0"), field_name)
        for field_name in (f"reserved_word_{i:02d}" for i in range(1, RESERVED_WORD_COUNT + 1))
    )

    records_by_index: dict[int, dict[str, str]] = {}
    for line_number, row in enumerate(rows, start=2):
        index = _parse_uint(row.get("index", ""), f"index at line {line_number}")
        if index in records_by_index:
            raise AnimationFormatError(f"duplicate record index {index}")
        records_by_index[index] = row

    indexes = sorted(records_by_index)
    if indexes != list(range(len(rows))):
        raise AnimationFormatError("record indexes must be contiguous and start at zero")
    if record_count != len(rows):
        raise AnimationFormatError(
            f"record_count says {record_count}, but CSV contains {len(rows)} record rows"
        )

    records: list[DefinitionRecord | DefinitionSetRecord] = []
    if kind == DEFINITION_MAGIC:
        for index in indexes:
            row = records_by_index[index]
            strings = [
                _string_from_text(row[field_name], encoding, f"record {index} {field_name}")
                for field_name in DEFINITION_TEXT_FIELDS
            ]
            integers = [
                _parse_int32(row[field_name], f"record {index} {field_name}")
                for field_name in DEFINITION_PARAMETER_FIELDS
            ]
            records.append(DefinitionRecord(strings, integers))
    else:
        slot_count = 16 if mode == 0 else 20
        set_fields = ("set_key",) + tuple(f"slot_{i:02d}" for i in range(1, slot_count + 1))
        for index in indexes:
            row = records_by_index[index]
            strings = [
                _string_from_text(row[field_name], encoding, f"record {index} {field_name}")
                for field_name in set_fields
            ]
            records.append(DefinitionSetRecord(strings))

    header = _make_header(kind, version_major, version_minor, mode, len(records), reserved_words)
    return AnimationDocument(kind, header, mode, records, b"", encoding)


def csv_to_document(source: str | Path) -> AnimationDocument:
    """Read a semantic animation CSV without writing a binary."""

    source_path = Path(source)
    with source_path.open("r", encoding="utf-8-sig", newline="") as stream:
        metadata, fieldnames, rows = _read_csv_rows(stream)
    return _parse_document_rows(metadata, fieldnames, rows)


def csv_to_binary(source: str | Path, destination: str | Path) -> AnimationDocument:
    """Convert a semantic animation CSV back to its binary form."""

    document = csv_to_document(source)
    Path(destination).write_bytes(build_binary(document))
    return document


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Buriko animation definition binaries to/from semantic CSV"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="binary -> semantic CSV")
    export_parser.add_argument("source", type=Path)
    export_parser.add_argument("destination", type=Path)
    export_parser.add_argument("--encoding", default=STRING_ENCODING)

    import_parser = subparsers.add_parser("import", help="semantic CSV -> binary")
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("destination", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "export":
            binary_to_csv(args.source, args.destination, encoding=args.encoding)
        else:
            csv_to_binary(args.source, args.destination)
    except (OSError, AnimationFormatError) as exc:
        print(f"error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
