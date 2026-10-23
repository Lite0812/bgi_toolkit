"""Resolve editable display names to stable v1 engine opcodes.

The active name table takes precedence over aliases in bundled prototypes.
Ambiguous names never select an arbitrary engine function. Raw opcodes remain
usable when the display table is missing or invalid.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ir import CallExpr
    from .signatures import Signature


MAPPING_PATH = Path(__file__).resolve().parents[1] / "asdis" / "resources" / "bss_mapping.json"
DEFINITIONS_PATH = Path(__file__).with_name("builtin_defs.json")
RAW_OPCODE_RE = re.compile(r"(?:(?:sys_|msg_|snd_|grp_|slct)::)?f_([0-9a-fA-F]{3,})\Z")


def normalize_opcode(name: str) -> str | None:
    match = RAW_OPCODE_RE.fullmatch(name)
    return f"f_{int(match[1], 16):03x}" if match else None


def signature_opcode(signature: Signature) -> str | None:
    opcodes = {opcode for name in (signature.name, *signature.aliases)
               if (opcode := normalize_opcode(name)) is not None}
    return next(iter(opcodes)) if len(opcodes) == 1 else None


def _name_variants(name: str) -> tuple[str, ...]:
    if normalize_opcode(name) is not None:
        return (name,)
    return (name, name[2:] if name.startswith("f_") else f"f_{name}")


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class BuiltinFunctionResolver:
    def __init__(self, mapping: dict[str, str], aliases: dict[str, set[str]]):
        active: dict[str, set[str]] = {}
        self.display_names: dict[str, str] = {}
        for raw, name in mapping.items():
            opcode = normalize_opcode(raw)
            if opcode is None or not isinstance(name, str) or not name:
                continue
            self.display_names[opcode] = name
            for variant in _name_variants(name):
                active.setdefault(variant, set()).add(opcode)
        combined = {**aliases, **active}
        self.ambiguous_names = frozenset(name for name, ids in combined.items() if len(ids) != 1)
        self.opcodes_by_name = {name: next(iter(ids)) for name, ids in combined.items() if len(ids) == 1}

    def resolve(self, name: str) -> str | None:
        return normalize_opcode(name) or self.opcodes_by_name.get(name)

    def display_name(self, opcode: str, *, like: str | None = None) -> str:
        opcode = normalize_opcode(opcode) or opcode
        if like is not None and normalize_opcode(like) is not None:
            return opcode
        name = self.display_names.get(opcode, opcode)
        if self.resolve(name) != opcode:
            return opcode
        if like is not None and not like.startswith("f_") and normalize_opcode(name) is None:
            return name.removeprefix("f_")
        return name


def _file_key(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), 0, 0


@lru_cache(maxsize=8)
def _load_resolver(mapping_key: tuple, definitions_key: tuple) -> BuiltinFunctionResolver:
    aliases: dict[str, set[str]] = {}
    definitions = _read_json(Path(definitions_key[0]))
    for entry in definitions.get("external_functions", []):
        if not isinstance(entry, dict):
            continue
        names = entry.get("aliases", [])
        ids = {opcode for name in names if isinstance(name, str)
               and (opcode := normalize_opcode(name)) is not None}
        if len(ids) != 1:
            continue
        opcode = next(iter(ids))
        prototype = entry.get("prototype", "")
        if isinstance(prototype, str) and "(" in prototype:
            names = [*names, prototype.split("(", 1)[0].split()[-1]]
        for name in names:
            if isinstance(name, str):
                for variant in _name_variants(name):
                    aliases.setdefault(variant, set()).add(opcode)
    return BuiltinFunctionResolver(_read_json(Path(mapping_key[0])), aliases)


_active_resolver: ContextVar[BuiltinFunctionResolver | None] = ContextVar("bgi_builtin_resolver", default=None)


@contextmanager
def builtin_resolver_scope():
    """Use one name-table snapshot per analysis, isolated between worker contexts."""
    token = _active_resolver.set(load_builtin_resolver())
    try:
        yield
    finally:
        _active_resolver.reset(token)


def load_builtin_resolver() -> BuiltinFunctionResolver:
    active = _active_resolver.get()
    if active is not None:
        return active
    return _load_resolver(_file_key(MAPPING_PATH), _file_key(DEFINITIONS_PATH))


def call_opcode(call: CallExpr) -> str | None:
    # An empty identity marks a script or unresolved call, which must not be
    # reinterpreted just because a later name table contains the same name.
    if call.builtin_opcode is not None:
        return call.builtin_opcode or None
    return load_builtin_resolver().resolve(call.callee)
