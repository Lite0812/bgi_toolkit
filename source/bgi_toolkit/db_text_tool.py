#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ScenarioDB / StringsDB <-> UTF-8 JSON, lossless for the supplied DB format.

Usage:
    python db_text_tool.py export ScenarioDB
    python db_text_tool.py export StringsDB
    python db_text_tool.py build ScenarioDB.json
    python db_text_tool.py build StringsDB.json
    python db_text_tool.py check ScenarioDB
    python db_text_tool.py check StringsDB

No third-party dependencies. All strings use the original game's CP932 encoding.
"""
from __future__ import annotations
from .i18n import tr

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
import re
import struct
import sys
from pathlib import Path
from typing import Any

ENCODING = "cp932"
SCENARIO_RECORD_SIZE = 128
SCENARIO_LABEL_RECORD_SIZE = 64
STRINGS_RECORD_SIZE = 32
SCENARIO_LIMIT = 0x400
STRINGS_LIMIT = 0x2000
SCHEMA_VERSION = 1


class DBError(ValueError):
    """Input format cannot be converted safely."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_c_string(data: bytes, *, where: str) -> str:
    """Require NUL termination and zero padding: don't silently lose unknown bytes."""
    nul = data.find(b"\x00")
    if nul < 0:
        raise DBError(tr('{0}: 找不到 NUL 结尾（字段长度 {1} 字节）', where, len(data)))
    if any(data[nul + 1 :]):
        raise DBError(tr('{0}: 字符串后有非零未知数据，拒绝有损转换', where))
    try:
        return data[:nul].decode(ENCODING, errors="strict")
    except UnicodeDecodeError as exc:
        raise DBError(tr('{0}: CP932 解码失败: {1}', where, exc)) from exc


def write_c_string(value: Any, size: int, *, where: str) -> bytes:
    if not isinstance(value, str):
        raise DBError(tr('{0}: 应为字符串', where))
    if "\x00" in value:
        raise DBError(tr('{0}: 不支持字符串内含 NUL 字符', where))
    try:
        encoded = value.encode(ENCODING, errors="strict")
    except UnicodeEncodeError as exc:
        raise DBError(tr('{0}: CP932 无法编码字符（游戏中不能直接存储）: {1}', where, exc)) from exc
    if len(encoded) >= size:  # includes room for the terminating 0x00
        raise DBError(tr('{0}: CP932 编码后 {1} 字节，最多 {2} 字节', where, len(encoded), size - 1))
    return encoded + b"\x00" * (size - len(encoded))


def get_count(data: bytes, record_size: int, limit: int, *, kind: str) -> int:
    if len(data) < 4:
        raise DBError(tr('{0}: 文件不足 4 字节', kind))
    (count,) = struct.unpack_from("<I", data)
    if count > limit:
        raise DBError(tr('{0}: 记录数 {1} 超过引擎限制 {2}', kind, count, limit))
    expected = 4 + count * record_size
    if len(data) != expected:
        raise DBError(
            tr('{0}: 文件大小 {1} 不正确：头部记录数 {2} × {3} + 4 = {4} 字节', kind, len(data), count, record_size, expected)
        )
    return count


def scenario_record_size(data: bytes) -> int:
    # The 64-byte layout stores a label; its row index determines the resource
    # name (Scenario%04d). The 128-byte layout stores resource + label.
    size = SCENARIO_RECORD_SIZE
    if len(data) >= 4:
        (count,) = struct.unpack_from("<I", data)
        if count and len(data) == 4 + count * SCENARIO_LABEL_RECORD_SIZE:
            size = SCENARIO_LABEL_RECORD_SIZE
    get_count(data, size, SCENARIO_LIMIT, kind="ScenarioDB")
    return size


def infer_kind(data: bytes, requested: str | None = None) -> str:
    if requested is not None:
        if requested not in ("ScenarioDB", "StringsDB"):
            raise DBError(tr('不支持的数据库类型：{0}', requested))
        if requested == "ScenarioDB":
            scenario_record_size(data)
        else:
            get_count(data, STRINGS_RECORD_SIZE, STRINGS_LIMIT, kind=requested)
        return requested
    possibilities = []
    for kind, size, limit in [
        ("ScenarioDB", SCENARIO_RECORD_SIZE, SCENARIO_LIMIT),
        ("StringsDB", STRINGS_RECORD_SIZE, STRINGS_LIMIT),
    ]:
        try:
            if kind == "ScenarioDB":
                scenario_record_size(data)
            else:
                get_count(data, size, limit, kind=kind)
        except DBError:
            continue
        possibilities.append(kind)
    if len(possibilities) != 1:
        raise DBError(tr('无法唯一判断数据库类型，请用 --kind ScenarioDB 或 --kind StringsDB 指定'))
    return possibilities[0]


def id_to_key(identifier: int) -> list[int]:
    q, fourth = divmod(identifier, 1000)
    q, third = divmod(q, 100)
    first, second = divmod(q, 100)
    return [first, second, third, fourth]


def key_to_id(value: Any, *, where: str) -> int:
    if not isinstance(value, list) or len(value) != 4:
        raise DBError(tr('{0}: key 应为 [第一管理号, 第二管理号, 第三管理号, 第四管理号]', where))
    if any(type(n) is not int for n in value):
        raise DBError(tr('{0}: key 中的管理号都必须是整数', where))
    a, b, c, d = value
    if a < 0 or not (0 <= b < 100) or not (0 <= c < 100) or not (0 <= d < 1000):
        raise DBError(tr('{0}: 管理号范围不正确（后三段分别为 0～99、0～99、0～999）', where))
    identifier = a * 10000000 + b * 100000 + c * 1000 + d
    if identifier > 0xFFFFFFFF:
        raise DBError(tr('{0}: 计算后的 ID 超出 uint32 范围', where))
    return identifier


def decode_db(data: bytes, kind: str) -> dict[str, Any]:
    if kind == "ScenarioDB":
        record_size = scenario_record_size(data)
        count = get_count(data, record_size, SCENARIO_LIMIT, kind=kind)
        records: list[dict[str, Any]] = []
        for i in range(count):
            row = data[4 + i * record_size: 4 + (i + 1) * record_size]
            if record_size == SCENARIO_LABEL_RECORD_SIZE:
                records.append({"label": read_c_string(row, where=f"ScenarioDB[{i}].label")})
            else:
                records.append({
                    "resource": read_c_string(row[:96], where=f"ScenarioDB[{i}].resource"),
                    "label": read_c_string(row[96:], where=f"ScenarioDB[{i}].label"),
                })
    elif kind == "StringsDB":
        count = get_count(data, STRINGS_RECORD_SIZE, STRINGS_LIMIT, kind=kind)
        records = []
        for i in range(count):
            row = data[4 + i * 32: 4 + (i + 1) * 32]
            (identifier,) = struct.unpack_from("<I", row)
            records.append({
                "key": id_to_key(identifier),
                "text": read_c_string(row[4:], where=f"StringsDB[{i}].text"),
            })
    else:
        raise DBError(tr('不支持的类型: {0}', kind))
    document = {
        "format": kind,
        "version": SCHEMA_VERSION,
        "encoding": "cp932",
    }
    if kind == "ScenarioDB" and record_size == SCENARIO_LABEL_RECORD_SIZE:
        document["record_size"] = record_size
    document["records"] = records
    return document


def encode_db(document: Any) -> bytes:
    if not isinstance(document, dict):
        raise DBError(tr('JSON 顶层必须是对象'))
    kind = document.get("format")
    if kind not in ("ScenarioDB", "StringsDB"):
        raise DBError(tr('JSON 的 format 必须是 ScenarioDB 或 StringsDB'))
    if document.get("version") != SCHEMA_VERSION or document.get("encoding") != ENCODING:
        raise DBError(tr('JSON 的 version 应为 1，encoding 应为 cp932'))
    records = document.get("records")
    if not isinstance(records, list):
        raise DBError(tr('JSON 的 records 必须是数组'))
    limit = SCENARIO_LIMIT if kind == "ScenarioDB" else STRINGS_LIMIT
    if len(records) > limit:
        raise DBError(tr('{0}: 记录数超过限制 {1}', kind, limit))
    if kind == "ScenarioDB":
        record_size = document.get("record_size", SCENARIO_RECORD_SIZE)
        if type(record_size) is not int or record_size not in (SCENARIO_LABEL_RECORD_SIZE, SCENARIO_RECORD_SIZE):
            raise DBError(tr('ScenarioDB 的 record_size 必须是 64 或 128'))
    result = bytearray(struct.pack("<I", len(records)))
    for i, item in enumerate(records):
        if not isinstance(item, dict):
            raise DBError(tr('records[{0}]: 记录必须是对象', i))
        if kind == "ScenarioDB":
            if record_size == SCENARIO_LABEL_RECORD_SIZE:
                if "label" not in item:
                    raise DBError(tr('ScenarioDB[{0}]: 缺少 label', i))
                if "resource" in item:
                    raise DBError(tr('ScenarioDB[{0}]: 64 字节格式不保存 resource，资源名由记录顺序决定', i))
                result += write_c_string(item["label"], record_size, where=f"ScenarioDB[{i}].label")
            else:
                if not {"resource", "label"} <= item.keys():
                    raise DBError(tr('ScenarioDB[{0}]: 缺少 resource 或 label', i))
                result += write_c_string(item["resource"], 96, where=f"ScenarioDB[{i}].resource")
                result += write_c_string(item["label"], 32, where=f"ScenarioDB[{i}].label")
        else:
            if not {"key", "text"} <= item.keys():
                raise DBError(tr('StringsDB[{0}]: 缺少 key 或 text', i))
            result += struct.pack("<I", key_to_id(item["key"], where=f"StringsDB[{i}].key"))
            result += write_c_string(item["text"], 28, where=f"StringsDB[{i}].text")
    return bytes(result)



# 仅把四段编号压到同一行，保留其他 JSON 的常规缩进。
_KEY_ARRAY_PATTERN = re.compile(
    r'(?m)^([ \t]*)"key": \[\n'
    r'[ \t]*([0-9]+),\n'
    r'[ \t]*([0-9]+),\n'
    r'[ \t]*([0-9]+),\n'
    r'[ \t]*([0-9]+)\n'
    r'[ \t]*\]'
)


def format_json(obj: dict[str, Any]) -> str:
    """输出易读 JSON；StringsDB 的 key 始终使用 [a, b, c, d] 单行格式。"""
    formatted = json.dumps(obj, ensure_ascii=False, indent=2)
    if obj.get("format") == "StringsDB":
        def compact_key(match: re.Match[str]) -> str:
            nums = ", ".join(match.group(i) for i in range(2, 6))
            return f'{match.group(1)}"key": [{nums}]'

        formatted, replaced = _KEY_ARRAY_PATTERN.subn(compact_key, formatted)
        if replaced != len(obj["records"]):
            raise DBError(tr('导出 JSON key 格式化数量异常：{0}/{1}', replaced, len(obj['records'])))
    return formatted + "\n"

def output_file(path: Path, contents: bytes, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        try:
            with path.open("xb") as stream:
                stream.write(contents)
        except FileExistsError as error:
            raise DBError(tr('输出文件已存在：{0}（勾选允许覆盖或使用 --force）', path)) from error
        return
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ensure_roundtrip(data: bytes, kind: str) -> dict[str, Any]:
    obj = decode_db(data, kind)
    built = encode_db(obj)
    if built != data:
        diff = next((i for i, (a, b) in enumerate(zip(data, built)) if a != b), min(len(data), len(built)))
        raise DBError(tr('原文件与重建文件第 0x{0:X} 字节不同；拒绝不可靠转换', diff))
    return obj


@dataclass
class DBTaskResult:
    outputs: list[Path] = field(default_factory=list)
    succeeded: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def default_output(source: Path, operation: str) -> Path | None:
    if operation == "check":
        return None
    if source.is_dir():
        return source.with_name(source.name + ("_db_json" if operation == "export" else "_db_rebuilt"))
    if operation == "export":
        return source.with_name(source.name + ".json")
    return source.with_suffix(".rebuilt")


def _named_kind(path: Path) -> str | None:
    stem = re.sub(r"__\d+$", "", path.name.split(".", 1)[0].casefold())
    return {"scenariodb": "ScenarioDB", "stringsdb": "StringsDB"}.get(stem)


def database_inputs(source: Path, operation: str, *, recursive=True, kind=None) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise DBError(tr('输入文件或目录不存在：{0}', source))
    files = source.rglob("*") if recursive else source.glob("*")
    selected = []
    for path in files:
        if not path.is_file() or any(part.startswith('.bgi-') for part in path.relative_to(source).parts):
            continue
        named = _named_kind(path)
        if operation == "build":
            if path.suffix.casefold() != ".json":
                continue
            if named is None:
                try:
                    obj = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, UnicodeError):
                    continue
                if not isinstance(obj, dict) or obj.get("format") not in ("ScenarioDB", "StringsDB"):
                    continue
            selected.append(path)
        elif named and (kind is None or named == kind) and path.suffix.casefold() in ("", ".bin", ".dat", ".rebuilt"):
            selected.append(path)
    return sorted(selected)


def run_db_task(operation: str, source: Path, output: Path | None = None, *, kind=None,
                force=False, recursive=True, log=print, cancelled=lambda: False) -> DBTaskResult:
    if operation not in ("export", "build", "check"):
        raise DBError(tr('不支持的数据库操作：{0}', operation))
    if kind not in (None, "ScenarioDB", "StringsDB"):
        raise DBError(tr('不支持的数据库类型：{0}', kind))
    source = Path(source)
    batch = source.is_dir()
    files = database_inputs(source, operation, recursive=recursive, kind=kind)
    if not files:
        raise DBError(tr('输入目录没有找到 ScenarioDB / StringsDB') + (tr(' JSON 文件') if operation == "build" else tr(' 文件')))
    target = Path(output) if output is not None else default_output(source, operation)
    result = DBTaskResult()
    for index, path in enumerate(files, 1):
        if cancelled():
            result.cancelled = True
            log(tr('已取消，完成 {0}/{1} 个文件', result.succeeded, len(files)))
            break
        try:
            log(f"[{index}/{len(files)}] {path}")
            if operation == "build":
                obj = json.loads(path.read_text(encoding="utf-8-sig"))
                data = encode_db(obj)
                actual_kind = obj["format"]
                if decode_db(data, actual_kind)["records"] != obj["records"]:
                    raise DBError(tr('JSON 重建后回读不一致，已停止'))
                contents = data
            else:
                data = path.read_bytes()
                # The empty table is structurally ambiguous; its resource name resolves it.
                actual_kind = infer_kind(data, kind or (_named_kind(path) if data == bytes(4) else None))
                obj = ensure_roundtrip(data, actual_kind)
                contents = format_json(obj).encode("utf-8") if operation == "export" else None
            log(tr('{0} · {1} 条记录 · {2} 字节 · SHA256={3}', actual_kind, len(obj['records']), len(data), digest(data)))
            if operation != "check":
                if batch:
                    relative = path.relative_to(source)
                    relative = relative.with_name(relative.name + ".json") if operation == "export" else relative.with_suffix("")
                    destination = target / relative
                else:
                    destination = target
                if destination.resolve() == path.resolve():
                    raise DBError(tr('输出路径不能与输入文件相同'))
                output_file(destination, contents, force=force)
                result.outputs.append(destination)
                log(f"{tr('导出') if operation == 'export' else tr('重建')}：{destination}")
            else:
                log(tr('验证通过：二进制 → JSON → 二进制逐字节一致'))
            result.succeeded += 1
        except (DBError, OSError, ValueError, UnicodeError) as error:
            message = f"{path}：{error}"
            result.errors.append(message)
            log(tr('失败 · {0}', message))
    log(tr('完成 {0} 个文件 · 失败 {1} 个', result.succeeded, len(result.errors)) + (tr(' · 已取消') if result.cancelled else ""))
    return result


def add_conversion_arguments(parser, operation):
    parser.add_argument("input", type=Path, help=tr('输入文件或目录'))
    if operation != "check":
        parser.add_argument("-o", "--output", type=Path)
        parser.add_argument("--force", action="store_true", help=tr('允许覆盖已有输出文件'))
    if operation in ("export", "check"):
        parser.add_argument("--kind", choices=["ScenarioDB", "StringsDB"], help=tr('手动指定数据库类型'))
    parser.add_argument("--no-recursive", action="store_true", help=tr('目录只处理当前层'))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=tr('ScenarioDB / StringsDB 与 UTF-8 JSON 互转（CP932，严格无损校验）'))
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in ("export", "build", "check"):
        add_conversion_arguments(commands.add_parser(operation), operation)
    args = parser.parse_args(argv)
    try:
        result = run_db_task(args.command, args.input, getattr(args, "output", None),
            kind=getattr(args, "kind", None), force=getattr(args, "force", False), recursive=not args.no_recursive)
        return 1 if result.errors or result.cancelled else 0
    except (DBError, OSError, ValueError, UnicodeError) as error:
        print(tr('错误：{0}', error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
