from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MultiResultSchema:
    type_name: str
    variable_name: str
    fields: tuple[str, ...]

    def declaration(self) -> str:
        lines = [f"typedef struct {self.type_name} {{"]
        lines.extend(f"    int32_t {field};" for field in self.fields)
        lines.append(f"}} {self.type_name};")
        return "\n".join(lines)


MEMORY_STATUS_SCHEMA = MultiResultSchema(
    type_name="BPDMemoryStatus",
    variable_name="memory_status",
    fields=("total_physical_bytes", "available_physical_bytes"),
)

GDB_LOAD_SCHEMA = MultiResultSchema(
    type_name="BPDGdbLoadResult",
    variable_name="gdb_load_result",
    fields=("window_x", "window_y", "status"),
)

CURSOR_POSITION_SCHEMA = MultiResultSchema(
    type_name="BPDCursorPosition",
    variable_name="cursor_position",
    fields=("x", "y"),
)


MULTI_RESULT_SCHEMAS = {
    "sys1_gdb_load": GDB_LOAD_SCHEMA,
    "sys1_get_cursor_pos_client": CURSOR_POSITION_SCHEMA,
    "sys1_get_memory_status_legacy": MEMORY_STATUS_SCHEMA,
}


def multi_result_schema(opcode: str) -> MultiResultSchema | None:
    return MULTI_RESULT_SCHEMAS.get(opcode)


def result_declarations(text: str) -> list[str]:
    return [
        schema.declaration()
        for schema in MULTI_RESULT_SCHEMAS.values()
        if schema.type_name in text
    ]
