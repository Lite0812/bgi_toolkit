from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .asdis.service import AsdisOptions


class InputKind(str, Enum):
    STORY_BINARY = "story-binary"
    BSD = "bsd"
    BP_BINARY = "bp-binary"
    BPD = "bpd"
    GDB = "gdb"
    BSI = "bsi"
    SCENARIO_DB = "scenario-db"
    STRINGS_DB = "strings-db"
    IMAGE = "image"
    AUDIO = "audio"
    ANIMATION = "animation"
    ARCHIVE = "archive"
    MOVIE = "movie"
    PACKED_RESOURCE = "packed-resource"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


class Operation(str, Enum):
    INSPECT = "inspect"
    DISASSEMBLE = "disassemble"
    ASSEMBLE = "assemble"
    DECOMPILE = "decompile"
    TEXT_EXPORT = "text-export"
    TEXT_IMPORT = "text-import"


class TextFormat(str, Enum):
    JSON = "json"
    TXT = "txt"


@dataclass(frozen=True, slots=True)
class TaskOptions:
    asdis: AsdisOptions = field(default_factory=AsdisOptions)
    recursive: bool = True
    keep_intermediate: bool = False
    emit_ir: bool = False
    include_source_comments: bool = True
    output_format: str = "c"
    bsd_hint: str = "auto"
    user_function_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskRequest:
    operation: Operation
    input_path: Path
    output_path: Path | None = None
    translation_path: Path | None = None
    text_format: TextFormat = TextFormat.JSON
    options: TaskOptions = field(default_factory=TaskOptions)


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: str
    path: Path
    source_path: Path


@dataclass(frozen=True, slots=True)
class Diagnostic:
    level: str
    message: str
    source_path: Path | None = None


@dataclass(slots=True)
class TaskResult:
    artifacts: list[Artifact] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    inspected: list[tuple[Path, InputKind]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not any(item.level == "error" for item in self.diagnostics)


ProgressCallback = Callable[[str], None]
