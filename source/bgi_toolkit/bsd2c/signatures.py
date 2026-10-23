from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .builtin_functions import load_builtin_resolver, normalize_opcode, signature_opcode


NO_DEFAULT = object()
BUILTIN_DEFINITIONS_PATH = Path(__file__).with_name("builtin_defs.json")


@dataclass(slots=True, frozen=True)
class ParamSpec:
    name: str
    type_name: str = "long"
    default: object = NO_DEFAULT

    @property
    def has_default(self) -> bool:
        return self.default is not NO_DEFAULT


@dataclass(slots=True, frozen=True)
class Signature:
    name: str
    return_type: str
    params: tuple[ParamSpec, ...] = ()
    aliases: tuple[str, ...] = ()
    source: str = "builtin"
    variadic: bool = False

    @property
    def arg_count(self) -> int:
        return len(self.params)

    @property
    def required_arg_count(self) -> int:
        count = len(self.params)
        while count and self.params[count - 1].has_default:
            count -= 1
        return count

    @property
    def returns_value(self) -> bool:
        return self.return_type.strip() != "void"


class SignatureLibrary:
    def __init__(self, signatures: list[Signature]):
        self._signatures: dict[str, Signature] = {}
        self._by_opcode: dict[str, Signature] = {}
        self.resolver = load_builtin_resolver()
        for signature in signatures:
            if (opcode := signature_opcode(signature) or self.resolver.resolve(signature.name)) is not None:
                self._by_opcode[opcode] = signature
            self._signatures[signature.name] = signature
            for alias in signature.aliases:
                self._signatures[alias] = signature

    def get(self, name: str) -> Signature | None:
        if name in self.resolver.ambiguous_names and normalize_opcode(name) is None:
            return None
        opcode = self.resolve_opcode(name)
        if opcode is not None:
            return self._by_opcode.get(opcode)
        return self._signatures.get(name)

    def resolve_opcode(self, name: str) -> str | None:
        opcode = self.resolver.resolve(name)
        if opcode is not None or name in self.resolver.ambiguous_names:
            return opcode
        signature = self._signatures.get(name)
        return signature_opcode(signature) if signature is not None else None

    def signatures(self) -> list[Signature]:
        unique: dict[str, Signature] = {}
        for signature in self._signatures.values():
            unique[signature.name] = signature
        return list(unique.values())


def param(name: str, type_name: str = "long", default: object = NO_DEFAULT) -> ParamSpec:
    return ParamSpec(name=name, type_name=type_name, default=default)


def signature(
    name: str,
    return_type: str,
    *params: ParamSpec,
    aliases: tuple[str, ...] = (),
    variadic: bool = False,
) -> Signature:
    return Signature(name=name, return_type=return_type, params=tuple(params), aliases=aliases, variadic=variadic)


def load_builtin_signature_library() -> SignatureLibrary:
    return _load_builtin_signature_library(load_builtin_resolver())


@lru_cache(maxsize=8)
def _load_builtin_signature_library(resolver) -> SignatureLibrary:
    from .definitions import load_project_definitions

    if not BUILTIN_DEFINITIONS_PATH.exists():
        return SignatureLibrary([])

    builtin_definitions = load_project_definitions(BUILTIN_DEFINITIONS_PATH)
    if not builtin_definitions.external_signatures:
        return SignatureLibrary([])
    return SignatureLibrary(list(builtin_definitions.external_signatures.values()))
