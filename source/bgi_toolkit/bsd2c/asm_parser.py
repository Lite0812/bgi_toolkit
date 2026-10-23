from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .builtin_functions import load_builtin_resolver


AUTO_LABEL_RE = re.compile(r"^L[0-9A-Fa-f]+$")
DIRECTIVE_RE = re.compile(r'^#(?P<name>header|header_padding|import|extra_import)\s+"(?P<value>.*)"(?:\s*//.*)?$')
EXPORT_RE = re.compile(r'^#export\s+"(?P<name>.*)"\s+(?P<offset>-?0x[0-9A-Fa-f]+|-?\d+)$')
DEFINE_RE = re.compile(r'^#define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<label>[A-Za-z_][A-Za-z0-9_]*)$')
EXTRA_IMPORT_GAP_RE = re.compile(r'^#extra_import_gap\s+(?P<size>-?\d+)$')
INSTRUCTION_RE = re.compile(r'^(?P<opcode>[A-Za-z_][A-Za-z0-9_:]*)\((?P<args>.*)\)\s*;?$')


@dataclass(slots=True)
class AsmInstruction:
    opcode: str
    args: list[str]
    line_no: int
    text: str


@dataclass(slots=True)
class AsmLabel:
    name: str
    line_no: int


AsmItem = AsmInstruction | AsmLabel


@dataclass(slots=True)
class AssemblyFunction:
    name: str
    items: list[AsmItem] = field(default_factory=list)


@dataclass(slots=True)
class AssemblyModule:
    path: Path
    header_text: str | None
    header_padding: str | None
    imports: list[str]
    extra_imports: list[str]
    exports: dict[str, int]
    defines: dict[str, str]
    functions: list[AssemblyFunction]
    source_type: str


def strip_inline_comment(text: str) -> str:
    if "//" not in text:
        return text
    if '"' not in text and "'" not in text:
        return text.partition("//")[0].rstrip()
    quote = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif quote and char == "\\":
            escaped = True
        elif char == quote:
            quote = None
        elif not quote and char in {'"', "'"}:
            quote = char
        elif not quote and text[index:index + 2] == "//":
            return text[:index].rstrip()
    return text


def split_arguments(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if "," not in text:
        return [text]
    if '"' not in text and "'" not in text:
        parts = text.split(",")
        if not parts[-1].strip():
            parts.pop()
        return [part.strip() for part in parts]

    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    quote_char = ""
    escape = False

    for char in text:
        if escape:
            current.append(char)
            escape = False
            continue

        if in_quote:
            current.append(char)
            if char == "\\":
                escape = True
            elif char == quote_char:
                in_quote = False
                quote_char = ""
            continue

        if char in {'"', "'"}:
            in_quote = True
            quote_char = char
            current.append(char)
            continue

        if char == ',':
            parts.append("".join(current).strip())
            current = []
            continue

        current.append(char)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def detect_source_type(items: list[AsmItem], path: Path) -> str:
    for item in items:
        if isinstance(item, AsmInstruction) and item.opcode == "line" and item.args:
            filename = item.args[0].strip().strip('"').lower()
            if filename.endswith(".bsc"):
                return "bsc"
            if filename.endswith(".bss"):
                return "bss"
            if filename.endswith(".bsb"):
                return "bsb"
            if filename.endswith(".bs5"):
                return "bs5"

    suffix = path.suffix.lower()
    if suffix == ".bsc":
        return "bsc"
    if suffix == ".bss":
        return "bss"
    if suffix == ".bsb":
        return "bsb"
    if any(part.lower() == "script_bsb" for part in path.parts):
        return "bsb"
    return "bs5"


def is_function_entry(label: str, export_names: set[str]) -> bool:
    return label in export_names or AUTO_LABEL_RE.fullmatch(label) is None


def is_identifier(text: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) is not None


def comment_identifier(line: str) -> str | None:
    _, separator, comment = line.partition("//")
    if not separator:
        return None
    candidate = comment.strip()
    return candidate if is_identifier(candidate) else None


def normalize_extra_imports(extra_imports: list[str], header_padding_name: str | None) -> list[str]:
    if header_padding_name is None:
        return extra_imports

    normalized = list(extra_imports)
    for index, name in enumerate(normalized):
        if is_identifier(name):
            continue
        normalized[index] = header_padding_name
        break
    return normalized


def guess_extra_import_name(raw_name: str, items: list[AsmItem], fallback_index: int) -> str:
    if is_identifier(raw_name):
        return raw_name

    instructions = [item for item in items if isinstance(item, AsmInstruction)]
    for index, instr in enumerate(instructions):
        if instr.opcode != "push_string" or not instr.args:
            continue
        candidate = instr.args[0].strip().strip('"').strip("'")
        if not is_identifier(candidate):
            continue
        next_item = instructions[index + 1] if index + 1 < len(instructions) else None
        if next_item is not None and (next_item.opcode.startswith("f_") or load_builtin_resolver().resolve(next_item.opcode)):
            return candidate

    return f"extra_import_{fallback_index:02d}"


def function_calls(function: AssemblyFunction) -> list[str]:
    resolver = load_builtin_resolver()
    return [resolver.resolve(item.opcode) or item.opcode for item in function.items
            if isinstance(item, AsmInstruction) and (item.opcode.startswith("f_") or item.opcode == "call" or resolver.resolve(item.opcode))]


def first_string_literal(function: AssemblyFunction) -> str | None:
    for item in function.items:
        if isinstance(item, AsmInstruction) and item.opcode == "push_string" and item.args:
            candidate = item.args[0].strip().strip('"').strip("'")
            if is_identifier(candidate):
                return candidate
    return None


def contains_string_fragment(function: AssemblyFunction, fragment: str) -> bool:
    fragment = fragment.lower()
    for item in function.items:
        if not isinstance(item, AsmInstruction) or item.opcode != "push_string" or not item.args:
            continue
        candidate = item.args[0].strip().strip('"').strip("'").lower()
        if fragment in candidate:
            return True
    return False


def resolve_extra_import_names(functions: list[AssemblyFunction], extra_imports: list[str]) -> list[AssemblyFunction]:
    available_names = [name for name in extra_imports if is_identifier(name)]
    used_names: set[str] = set()
    assigned_indices: set[int] = set()

    music_name = next((name for name in available_names if "Music" in name), None)
    if music_name is not None:
        for index, function in enumerate(functions):
            if contains_string_fragment(function, "music"):
                function.name = music_name
                used_names.add(music_name)
                assigned_indices.add(index)
                break

    for index, function in enumerate(functions):
        if index in assigned_indices:
            continue
        calls = function_calls(function)
        if "f_169" in calls:
            select_name = next((name for name in available_names if name.startswith("Select_") and name not in used_names), None)
            if select_name is not None:
                function.name = select_name
                used_names.add(select_name)
                assigned_indices.add(index)
                continue

        if "f_1a1" in calls:
            literal_name = first_string_literal(function)
            if literal_name is not None:
                function.name = literal_name
                used_names.add(literal_name)
                assigned_indices.add(index)
                continue

        if is_identifier(function.name) and function.name not in used_names:
            used_names.add(function.name)
            assigned_indices.add(index)

    remaining_names = [name for name in available_names if name not in used_names]
    for index, function in enumerate(functions):
        if index in assigned_indices or not remaining_names:
            continue
        if function.name.startswith("extra_import_") or function.name in available_names:
            function.name = remaining_names.pop(0)
            assigned_indices.add(index)

    return functions


def uniquify_function_names(functions: list[AssemblyFunction]) -> list[AssemblyFunction]:
    seen: dict[str, int] = {}
    for function in functions:
        count = seen.get(function.name, 0) + 1
        seen[function.name] = count
        if count == 1:
            continue
        function.name = f"{function.name}__{count}"
    return functions


def split_extra_import_functions(items: list[AsmItem], extra_imports: list[str]) -> list[AssemblyFunction]:
    functions: list[AssemblyFunction] = []
    current_items: list[AsmItem] = []
    function_index = 0

    for item in items:
        current_items.append(item)
        if isinstance(item, AsmInstruction) and item.opcode == "ret":
            raw_name = extra_imports[function_index] if function_index < len(extra_imports) else f"extra_import_{function_index:02d}"
            name = guess_extra_import_name(raw_name, current_items, function_index)
            functions.append(AssemblyFunction(name=name, items=current_items))
            current_items = []
            function_index += 1

    if current_items:
        raw_name = extra_imports[function_index] if function_index < len(extra_imports) else f"extra_import_{function_index:02d}"
        name = guess_extra_import_name(raw_name, current_items, function_index)
        functions.append(AssemblyFunction(name=name, items=current_items))

    return uniquify_function_names(resolve_extra_import_names(functions, extra_imports))


def implicit_frame_entries(items: list[AsmItem]) -> set[int]:
    """Find fresh stack frames after returns in scripts without function names."""
    local_targets = set()
    previous = None
    for item in items:
        if not isinstance(item, AsmInstruction):
            continue
        if (item.opcode in {"jmp", "jc", "reg_exception_handler"}
                and previous is not None and previous.opcode == "push_offset" and previous.args):
            local_targets.add(previous.args[0])
        previous = item

    entries = set()
    for index, item in enumerate(items):
        if not isinstance(item, AsmInstruction) or item.opcode != "load_base":
            continue
        prologue = items[index:index + 4]
        if (len(prologue) != 4 or not all(isinstance(op, AsmInstruction) for op in prologue)
                or [op.opcode for op in prologue] != ["load_base", "push_dword", "add", "store_base"]):
            continue
        if not prologue[1].args:
            continue
        try:
            if int(prologue[1].args[0], 0) < 0:
                continue
        except ValueError:
            continue
        start = index
        while start and isinstance(items[start - 1], AsmLabel):
            start -= 1
        before = items[start - 1] if start else None
        if not isinstance(before, AsmInstruction) or before.opcode != "ret":
            continue
        labels = [label.name for label in items[start:index]]
        # A branch/handler continuation must stay in its enclosing function.
        if local_targets.intersection(labels):
            continue
        entries.add(start)
    return entries


def split_functions(items: list[AsmItem], export_names: set[str], extra_imports: list[str] | None = None) -> list[AssemblyFunction]:
    has_explicit_entries = any(
        isinstance(item, AsmLabel) and is_function_entry(item.name, export_names)
        for item in items
    )
    if extra_imports and not has_explicit_entries:
        return split_extra_import_functions(items, extra_imports)

    functions: list[AssemblyFunction] = []
    current: AssemblyFunction | None = None
    implicit_entries = implicit_frame_entries(items)
    used_names = {item.name for item in items if isinstance(item, AsmLabel)} | export_names
    anonymous_index = 0

    for index, item in enumerate(items):
        if index in implicit_entries or (isinstance(item, AsmLabel) and is_function_entry(item.name, export_names)):
            if current is not None and current.items:
                functions.append(current)
            name = item.name if isinstance(item, AsmLabel) else None
            if name is None:
                while True:
                    anonymous_index += 1
                    name = f"anonymous_{anonymous_index:04d}"
                    if name not in used_names:
                        break
                used_names.add(name)
            current = AssemblyFunction(name=name, items=[item])
            continue

        if current is None:
            current = AssemblyFunction(name="main")
        current.items.append(item)

    if current is not None and current.items:
        functions.append(current)
    return uniquify_function_names(functions)


def first_source_line(function: AssemblyFunction, expected_suffix: str) -> int | None:
    for item in function.items:
        if not isinstance(item, AsmInstruction) or item.opcode != "line" or len(item.args) < 2:
            continue
        filename = item.args[0].strip().strip('"').lower()
        if not filename.endswith(expected_suffix):
            continue
        try:
            return int(item.args[1], 0)
        except ValueError:
            continue
    return None


def reorder_functions_by_source_line(functions: list[AssemblyFunction], source_type: str) -> list[AssemblyFunction]:
    if source_type != "bsc":
        return functions

    ordered: list[tuple[bool, int, int, AssemblyFunction]] = []
    for index, function in enumerate(functions):
        line_no = first_source_line(function, ".bsc")
        ordered.append((line_no is None, line_no if line_no is not None else 0, index, function))
    ordered.sort()
    return [function for _, _, _, function in ordered]


def parse_assembly_file(path: str | Path) -> AssemblyModule:
    source_path = Path(path)
    return parse_assembly_text(source_path.read_text(encoding="utf-8"), source_path)


def parse_assembly_text(text: str, path: str | Path = "script.bsd") -> AssemblyModule:
    source_path = Path(path)
    header_text: str | None = None
    header_padding: str | None = None
    header_padding_name: str | None = None
    imports: list[str] = []
    extra_imports: list[str] = []
    exports: dict[str, int] = {}
    defines: dict[str, str] = {}
    items: list[AsmItem] = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        if line[0] == "#":
            directive_match = DIRECTIVE_RE.match(line)
            if directive_match:
                name = directive_match.group("name")
                value = directive_match.group("value")
                if name == "header":
                    header_text = value
                elif name == "header_padding":
                    header_padding = value
                    header_padding_name = comment_identifier(raw_line)
                elif name == "import":
                    imports.append(value)
                elif name == "extra_import":
                    extra_imports.append(value)
                continue

            if EXTRA_IMPORT_GAP_RE.match(line):
                continue

            export_match = EXPORT_RE.match(line)
            if export_match:
                exports[export_match.group("name")] = int(export_match.group("offset"), 0)
                continue

            define_match = DEFINE_RE.match(line)
            if define_match:
                defines[define_match.group("name")] = define_match.group("label")
            continue

        if line.endswith(":"):
            items.append(AsmLabel(name=line[:-1], line_no=line_no))
            continue

        instruction_match = INSTRUCTION_RE.match(strip_inline_comment(line))
        if instruction_match:
            items.append(
                AsmInstruction(
                    opcode=instruction_match.group("opcode"),
                    args=split_arguments(instruction_match.group("args")),
                    line_no=line_no,
                    text=line,
                )
            )
            continue

    source_type = detect_source_type(items, source_path)
    extra_imports = normalize_extra_imports(extra_imports, header_padding_name)
    functions = split_functions(items, set(exports), extra_imports)
    functions = reorder_functions_by_source_line(functions, source_type)
    return AssemblyModule(
        path=source_path,
        header_text=header_text,
        header_padding=header_padding,
        imports=imports,
        extra_imports=extra_imports,
        exports=exports,
        defines=defines,
        functions=functions,
        source_type=source_type,
    )
