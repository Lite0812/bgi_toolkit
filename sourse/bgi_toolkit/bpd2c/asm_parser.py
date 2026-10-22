from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re

from .instruction_sizes import instruction_size
from ..bp_engine import EngineDetection, EngineProfile, declared_profile, detect_engine, engine_scope, get_profile


INSTRUCTION_RE = re.compile(r"^(?P<opcode>[A-Za-z_][A-Za-z0-9_:]*)\s*\((?P<args>.*)\)\s*;?$")
LABEL_RE = re.compile(r"^(?P<label>[A-Za-z_][A-Za-z0-9_]*)\s*:$")
LABEL_ADDEND_RE = re.compile(
    r"^#label-addend\s+(?P<label>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?P<addend>[+-]?(?:0[xX][0-9A-Fa-f]+|\d+))$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class AsmInstruction:
    opcode: str
    args: list[str]
    line_no: int
    text: str
    address: int | None = None


@dataclass(slots=True)
class AsmLabel:
    name: str
    line_no: int
    # ``address`` is where the label is written in the BPD instruction
    # stream. ``symbol_address`` is the value bpas assigns to the symbol.
    # They differ for #label-addend sources.
    address: int | None = None
    symbol_address: int | None = None
    addend: int = 0


AsmItem = AsmInstruction | AsmLabel


@dataclass(slots=True)
class ParseDiagnostic:
    line_no: int
    message: str
    text: str


@dataclass(slots=True)
class AssemblyModule:
    path: Path
    items: list[AsmItem] = field(default_factory=list)
    directives: list[tuple[int, str]] = field(default_factory=list)
    data_lines: list[tuple[int, str]] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    runtime_relative_branches: bool = False
    label_addends: dict[str, int] = field(default_factory=dict)
    engine: EngineProfile = field(default_factory=get_profile)
    engine_detection: EngineDetection | None = None

    @property
    def instruction_count(self) -> int:
        return sum(isinstance(item, AsmInstruction) for item in self.items)

    @property
    def label_count(self) -> int:
        return sum(isinstance(item, AsmLabel) for item in self.items)


@lru_cache(maxsize=32768)
def split_arguments(text: str) -> list[str]:
    """Split arguments while preserving strings and nested list/call syntax."""
    if not text.strip():
        return []

    parts: list[str] = []
    current: list[str] = []
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    quote: str | None = None
    escaped = False

    for char in text:
        if quote is not None:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "([{":
            stack.append(char)
            current.append(char)
            continue
        if char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
            current.append(char)
            continue
        if char == "," and not stack:
            parts.append("".join(current).strip())
            current.clear()
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return parts


def strip_inline_comment(text: str) -> str:
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text) - 1:
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "/" and text[index + 1] == "/":
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def read_bpd_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_assembly_text(text: str, path: str | Path = "<memory>.bpd", *, engine=None) -> AssemblyModule:
    declared = declared_profile(text) if engine is None or engine == 'auto' else None
    profile = (declared or get_profile()) if engine is None or engine == 'auto' else get_profile(engine)
    with engine_scope(profile):
        return _parse_assembly_text(text, path)


def _parse_assembly_text(text: str, path: str | Path) -> AssemblyModule:
    module = AssemblyModule(path=Path(path))
    in_data_section = False
    current_address: int | None = 0
    pending_label_addends: dict[str, tuple[int, int]] = {}

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        if stripped.startswith("#"):
            module.directives.append((line_no, stripped))
            directive_line = strip_inline_comment(stripped).strip()
            directive = directive_line.split(None, 1)[0].lower()
            in_data_section = directive in {"#strings", "#strdata"}

            if directive_line.lower() == "#runtime-relative-branches":
                module.runtime_relative_branches = True
            elif directive_line.lower() in {"#absolute-labels", "#relocatable-labels"}:
                module.diagnostics.append(
                    ParseDiagnostic(line_no, "unsupported legacy BPD directive", raw_line)
                )
            elif directive == "#label-addend":
                addend_match = LABEL_ADDEND_RE.fullmatch(directive_line)
                if addend_match is None:
                    module.diagnostics.append(
                        ParseDiagnostic(line_no, "invalid #label-addend directive", raw_line)
                    )
                else:
                    label = addend_match.group("label")
                    addend = int(addend_match.group("addend"), 0)
                    if label in pending_label_addends or label in module.label_addends:
                        module.diagnostics.append(
                            ParseDiagnostic(
                                line_no,
                                f"duplicate #label-addend for {label}",
                                raw_line,
                            )
                        )
                    else:
                        pending_label_addends[label] = (addend, line_no)
                        module.label_addends[label] = addend
            continue

        line = strip_inline_comment(stripped).strip()
        if not line:
            continue

        label_match = LABEL_RE.fullmatch(line)
        if label_match:
            in_data_section = False
            label = label_match.group("label")
            addend, _ = pending_label_addends.pop(label, (0, line_no))
            if current_address is not None:
                symbol_address = current_address + addend
            else:
                symbol_address = None
            module.items.append(
                AsmLabel(
                    name=label,
                    line_no=line_no,
                    address=current_address,
                    symbol_address=symbol_address,
                    addend=addend,
                )
            )
            continue

        instruction_match = INSTRUCTION_RE.fullmatch(line)
        if instruction_match:
            in_data_section = False
            opcode = instruction_match.group("opcode")
            args = split_arguments(instruction_match.group("args"))
            from ..asdis.bp import bpop
            if opcode in bpop.rops:
                bpop.validate_instruction_name(opcode, bpop.rops[opcode])
            module.items.append(
                AsmInstruction(
                    opcode=opcode,
                    args=args,
                    line_no=line_no,
                    text=line,
                    address=current_address,
                )
            )
            size = instruction_size(opcode, args)
            current_address = current_address + size if current_address is not None and size is not None else None
            continue

        if in_data_section or (len(line) >= 2 and line[0] in {'"', "'"} and line[-1] == line[0]):
            module.data_lines.append((line_no, line))
            continue

        module.diagnostics.append(ParseDiagnostic(line_no, "unrecognized BPD source line", raw_line))

    for label, (_, directive_line_no) in pending_label_addends.items():
        module.diagnostics.append(
            ParseDiagnostic(
                directive_line_no,
                f"#label-addend has no matching label: {label}",
                text.splitlines()[directive_line_no - 1],
            )
        )

    return module


def parse_assembly_file(path: str | Path, *, engine='auto', games_path=None, executable=None) -> AssemblyModule:
    source_path = Path(path)
    detection = detect_engine(source_path, engine, games_path=games_path, executable=executable)
    module = parse_assembly_text(read_bpd_text(source_path), source_path, engine=detection.profile)
    module.engine_detection = detection
    return module
