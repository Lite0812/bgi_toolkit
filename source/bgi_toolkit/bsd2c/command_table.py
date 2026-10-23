"""Import target-specific scenario signatures from ``bgiscn --scan`` output."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .builtin_functions import load_builtin_resolver, signature_opcode
from .definitions import ProjectDefinitions, load_project_definitions, write_project_definitions
from .signatures import Signature, load_builtin_signature_library, param


ROW = re.compile(r"^\s*0x([0-9a-fA-F]+)\s+(\w+)\s+(\w+)\s+(\d+)\s+(\d+)\s+0x([0-9a-fA-F]+)(?:\s|$)")


@dataclass(frozen=True, slots=True)
class ScenarioCommand:
    opcode: int
    module: str
    name: str
    arguments: int
    results: int
    handler: int


def parse_command_table(text: str) -> list[ScenarioCommand]:
    commands: dict[int, ScenarioCommand] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        match = ROW.match(line)
        if match is None:
            raise ValueError(f"Invalid command table row at line {line_number}: {line}")
        opcode, module, name, arguments, results, handler = match.groups()
        entry = ScenarioCommand(int(opcode, 16), module, name, int(arguments), int(results), int(handler, 16))
        if entry.opcode in commands and commands[entry.opcode] != entry:
            raise ValueError(f"Duplicate command opcode 0x{entry.opcode:03x}")
        if entry.opcode >= 0x100 and (entry.results > 1 or entry.arguments > 256):
            raise ValueError(f"Unsupported signature for opcode 0x{entry.opcode:03x}")
        commands[entry.opcode] = entry
    if not commands:
        raise ValueError("The command table is empty")
    return sorted(commands.values(), key=lambda entry: entry.opcode)


def import_command_table(table: str | Path, destination: str | Path) -> tuple[Path, int]:
    commands = parse_command_table(Path(table).read_text(encoding="utf-8-sig"))
    output = Path(destination)
    definitions = load_project_definitions(output) if output.is_file() else ProjectDefinitions()
    builtin = load_builtin_signature_library()
    resolver = load_builtin_resolver()
    count = 0
    for command in commands:
        # Base VM instructions have inline operands, not the command ABI.
        if command.opcode < 0x100:
            continue
        opcode = f"f_{command.opcode:03x}"
        old = builtin.get(opcode)
        params = old.params if old is not None and old.arg_count == command.arguments else tuple(
            param(f"arg{index}") for index in range(command.arguments)
        )
        return_type = (old.return_type if old is not None and old.returns_value else "long") if command.results else "void"
        for name, signature in list(definitions.external_signatures.items()):
            if (signature_opcode(signature) or resolver.resolve(name)) == opcode:
                del definitions.external_signatures[name]
        definitions.external_signatures[opcode] = Signature(opcode, return_type, params, source="project-system")
        count += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    return write_project_definitions(definitions, output), count
