from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


KNOWN_IGNORED_BUILTINS = (
    "NotifyOfFunctionName",
    "NotifyOfLineCount",
    "RegisterLabel",
    "07b",
    "07e",
)

# Stable identities for the labels offered by the GUI's built-in checkboxes.
IGNORED_BUILTIN_OPCODES = dict(zip(KNOWN_IGNORED_BUILTINS, (
    "f_0fb", "f_0fe", "f_0f6", "f_07b", "f_07e",
)))


def normalize_ignored_builtin_names(names: Iterable[str]) -> frozenset[str]:
    return frozenset(name.strip() for name in names if name and name.strip())


DEFAULT_BSC_IGNORED_BUILTINS = normalize_ignored_builtin_names(KNOWN_IGNORED_BUILTINS)
DEFAULT_BS5_LIKE_IGNORED_BUILTINS = normalize_ignored_builtin_names(KNOWN_IGNORED_BUILTINS)


@dataclass(frozen=True, slots=True)
class IgnoreBuiltinConfig:
    bsc: frozenset[str] = field(default_factory=lambda: DEFAULT_BSC_IGNORED_BUILTINS)
    bs5_like: frozenset[str] = field(default_factory=lambda: DEFAULT_BS5_LIKE_IGNORED_BUILTINS)

    def names_for_source_type(self, source_type: str) -> frozenset[str]:
        if source_type == "bsc":
            return self.bsc
        return self.bs5_like


DEFAULT_IGNORE_BUILTIN_CONFIG = IgnoreBuiltinConfig()
