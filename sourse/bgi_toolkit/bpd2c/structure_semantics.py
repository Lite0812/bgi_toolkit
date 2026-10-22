from __future__ import annotations

from dataclasses import dataclass
import re


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
C_TYPE_SIZES = {
    "char": 1,
    "int8_t": 1,
    "uint8_t": 1,
    "int16_t": 2,
    "uint16_t": 2,
    "int32_t": 4,
    "uint32_t": 4,
    "int64_t": 8,
    "uint64_t": 8,
}


@dataclass(frozen=True, slots=True)
class StructureField:
    name: str
    offset: int
    c_type: str
    type_id: int
    count: int = 1

    def __post_init__(self) -> None:
        if not IDENTIFIER_RE.fullmatch(self.name):
            raise ValueError(f"invalid structure field name: {self.name}")
        if self.offset < 0:
            raise ValueError(f"negative structure field offset: {self.offset}")
        if self.c_type not in C_TYPE_SIZES:
            raise ValueError(f"unsupported structure field type: {self.c_type}")
        if self.type_id not in {0, 1, 2, 3}:
            raise ValueError(f"invalid VM type id: {self.type_id}")
        if self.count < 1:
            raise ValueError(f"invalid structure field count: {self.count}")

    @property
    def element_size(self) -> int:
        return C_TYPE_SIZES[self.c_type]

    @property
    def byte_size(self) -> int:
        return self.element_size * self.count

    @property
    def is_array(self) -> bool:
        return self.count > 1

    @property
    def declaration(self) -> str:
        suffix = f"[{self.count}]" if self.is_array else ""
        return f"{self.c_type} {self.name}{suffix};"


@dataclass(frozen=True, slots=True)
class StructureRule:
    type_name: str
    array_name: str
    size: int
    bases: tuple[str | int, ...]
    base_expression: str
    fields: tuple[StructureField, ...]
    count: int | None = None
    default_string_field: str | None = None
    unanchored_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not IDENTIFIER_RE.fullmatch(self.type_name):
            raise ValueError(f"invalid structure type name: {self.type_name}")
        if not IDENTIFIER_RE.fullmatch(self.array_name):
            raise ValueError(f"invalid structure array name: {self.array_name}")
        if self.size < 1 or not self.bases or (self.count is not None and self.count < 1):
            raise ValueError(f"invalid structure rule: {self.type_name}")

        names: set[str] = set()
        end = 0
        for field in sorted(self.fields, key=lambda item: item.offset):
            if field.name in names or field.offset < end:
                raise ValueError(f"overlapping or duplicate field in {self.type_name}: {field.name}")
            if field.offset + field.byte_size > self.size:
                raise ValueError(f"field exceeds {self.type_name}: {field.name}")
            names.add(field.name)
            end = field.offset + field.byte_size
        if self.default_string_field is not None:
            string_field = self.field_named(self.default_string_field)
            if string_field is None or string_field.c_type != "char" or not string_field.is_array:
                raise ValueError(f"invalid default string field in {self.type_name}")
        if not self.unanchored_fields <= names:
            raise ValueError(f"unknown unanchored field in {self.type_name}")

    def field_named(self, name: str) -> StructureField | None:
        return next((field for field in self.fields if field.name == name), None)

    def matches_base(self, text: str) -> bool:
        token = _strip_parentheses(text)
        numeric = _parse_int(token)
        return any(
            numeric == base if isinstance(base, int) else token == base
            for base in self.bases
        )

    def index_from_pointer(self, text: str) -> str | None:
        token = _strip_parentheses(text)
        match = re.fullmatch(rf"&{re.escape(self.array_name)}\[(?P<index>.+)\]", token)
        return match.group("index").strip() if match else None

    def pointer(self, index: str) -> str:
        return f"&{self.array_name}[{index}]"

    def member(self, index: str, field: StructureField, element: int | None = None) -> str:
        access = f"{self.array_name}[{index}].{field.name}"
        if field.is_array and element is not None:
            access += f"[{element}]"
        return access

    def field_at(self, byte_offset: int, type_id: int | None = None) -> tuple[StructureField, int] | None:
        for field in self.fields:
            relative = byte_offset - field.offset
            if relative < 0 or relative >= field.byte_size:
                continue
            if relative % field.element_size:
                return None
            if type_id is not None and field.type_id != type_id:
                return None
            return field, relative // field.element_size
        return None

    def declaration(self) -> str:
        lines = ["#pragma pack(push, 1)", f"typedef struct {self.type_name} {{"]
        cursor = 0
        for field in sorted(self.fields, key=lambda item: item.offset):
            if field.offset > cursor:
                gap = field.offset - cursor
                lines.append(f"    uint8_t _padding_{cursor:04x}[{gap}];")
            lines.append(f"    {field.declaration}")
            cursor = field.offset + field.byte_size
        if cursor < self.size:
            lines.append(f"    uint8_t _padding_{cursor:04x}[{self.size - cursor}];")
        lines.extend([
            f"}} {self.type_name};",
            "#pragma pack(pop)",
            "",
            f"#define {self.array_name} (({self.type_name} *)(uintptr_t){self.base_expression})",
        ])
        return "\n".join(lines)


FONT_RECORD_RULE = StructureRule(
    type_name="FontRecord",
    array_name="font_records",
    size=0x20C,
    bases=("global_00000c00",),
    base_expression="global_00000c00",
    fields=(
        StructureField("face_name", 0x000, "char", 0, 256),
        StructureField("base_face_name", 0x100, "char", 0, 256),
        StructureField("font_id", 0x200, "int32_t", 2),
        StructureField("is_variable_pitch", 0x204, "int32_t", 2),
        StructureField("is_bold", 0x208, "int32_t", 2),
    ),
    default_string_field="face_name",
    unanchored_fields=frozenset({"font_id", "is_variable_pitch", "is_bold"}),
)

BBS_RECORD_RULE = StructureRule(
    type_name="BbsRecord",
    array_name="bbs_records",
    size=20,
    bases=(0x14EADC,),
    base_expression="0x14EADC",
    fields=(
        StructureField("registration_number", 0x00, "int32_t", 2),
        StructureField("number_text_index", 0x04, "int32_t", 2),
        StructureField("id_text_index", 0x08, "int32_t", 2),
        StructureField("body_text_index", 0x0C, "int32_t", 2),
        StructureField("id_text_color", 0x10, "int32_t", 2),
    ),
)

VM_THREAD_SLOT_RULE = StructureRule(
    type_name="VmThreadSlot",
    array_name="vm_thread_slots",
    size=0x10,
    bases=(0x6A980,),
    base_expression="0x6A980",
    fields=(
        StructureField("is_active", 0x00, "int32_t", 2),
        StructureField("thread_id", 0x04, "int32_t", 2),
        StructureField("field_08", 0x08, "int32_t", 2),
        StructureField("field_0c", 0x0C, "int32_t", 2),
    ),
    count=16,
)

STRUCTURE_RULES = (FONT_RECORD_RULE, BBS_RECORD_RULE, VM_THREAD_SLOT_RULE)


def _strip_parentheses(text: str) -> str:
    token = text.strip()
    while token.startswith("(") and token.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(token):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(token) - 1:
                    balanced = False
                    break
        if not balanced or depth:
            break
        token = token[1:-1].strip()
    return token


def _parse_int(text: str) -> int | None:
    try:
        return int(text.strip(), 0)
    except ValueError:
        return None


def _scaled_index(text: str, record_size: int) -> str | None:
    token = _strip_parentheses(text)
    suffix = f" * {record_size}"
    prefix = f"{record_size} * "
    if token.endswith(suffix):
        return token[: -len(suffix)].strip()
    if token.startswith(prefix):
        return token[len(prefix) :].strip()
    return None


def _adjust_index(index: str, delta: int) -> str:
    numeric = _parse_int(index)
    if numeric is not None:
        return str(numeric + delta)
    if delta == 0:
        return index
    operator = "+" if delta > 0 else "-"
    return f"{index} {operator} {abs(delta)}"


class StructureRegistry:
    def __init__(self, rules: tuple[StructureRule, ...]) -> None:
        self.rules = rules
        type_names = {rule.type_name for rule in rules}
        array_names = {rule.array_name for rule in rules}
        if len(type_names) != len(rules) or len(array_names) != len(rules):
            raise ValueError("duplicate structure type or array name")

    @staticmethod
    def _address(rule: StructureRule, index: str, byte_offset: int) -> str | None:
        record_delta, inner_offset = divmod(byte_offset, rule.size)
        adjusted_index = _adjust_index(index, record_delta)
        if inner_offset == 0:
            return rule.pointer(adjusted_index)
        located = rule.field_at(inner_offset)
        if located is None:
            return None
        field, element = located
        member = rule.member(adjusted_index, field, element if field.is_array else None)
        if field.is_array and element == 0:
            return rule.member(adjusted_index, field)
        return f"&{member}"

    @staticmethod
    def _field_value(rule: StructureRule, index: str, byte_offset: int, type_id: int) -> str | None:
        record_delta, inner_offset = divmod(byte_offset, rule.size)
        located = rule.field_at(inner_offset, type_id)
        if located is None:
            return None
        field, element = located
        adjusted_index = _adjust_index(index, record_delta)
        return rule.member(adjusted_index, field, element if field.is_array else None)

    def _absolute_field_value(self, address: str, byte_offset: int, type_id: int) -> str | None:
        numeric = _parse_int(_strip_parentheses(address))
        if numeric is None:
            return None
        candidates: list[str] = []
        for rule in self.rules:
            for base in rule.bases:
                if not isinstance(base, int):
                    continue
                relative = numeric + byte_offset - base
                limit = rule.size * (rule.count or 1)
                if relative < 0 or relative >= limit:
                    continue
                index, inner_offset = divmod(relative, rule.size)
                value = self._field_value(rule, str(index), inner_offset, type_id)
                if value is not None:
                    candidates.append(value)
        return candidates[0] if len(candidates) == 1 else None

    def recover_arithmetic(self, left: str, right: str) -> str | None:
        left_token = _strip_parentheses(left)
        right_token = _strip_parentheses(right)
        left_value = _parse_int(left_token)
        right_value = _parse_int(right_token)

        for rule in self.rules:
            if rule.matches_base(left_token):
                index = _scaled_index(right_token, rule.size)
                if index is not None:
                    return rule.pointer(index)
            if rule.matches_base(right_token):
                index = _scaled_index(left_token, rule.size)
                if index is not None:
                    return rule.pointer(index)
            index = rule.index_from_pointer(left_token)
            scaled_index = _scaled_index(right_token, rule.size)
            if index is not None and scaled_index is not None:
                return rule.pointer(f"{index} + {scaled_index}")
            if index is not None and right_value is not None:
                return self._address(rule, index, right_value)
            index = rule.index_from_pointer(right_token)
            scaled_index = _scaled_index(left_token, rule.size)
            if index is not None and scaled_index is not None:
                return rule.pointer(f"{index} + {scaled_index}")
            if index is not None and left_value is not None:
                return self._address(rule, index, left_value)
        return None

    def recover_field_load(self, base: str, byte_offset: int, type_id: int) -> str | None:
        absolute = self._absolute_field_value(base, byte_offset, type_id)
        if absolute is not None:
            return absolute
        for rule in self.rules:
            index = rule.index_from_pointer(base)
            if index is not None:
                return self._field_value(rule, index, byte_offset, type_id)

        candidates: list[tuple[StructureRule, StructureField, int]] = []
        for rule in self.rules:
            record_delta, inner_offset = divmod(byte_offset, rule.size)
            if record_delta:
                continue
            located = rule.field_at(inner_offset, type_id)
            if located is None or located[0].name not in rule.unanchored_fields:
                continue
            candidates.append((rule, *located))
        if len(candidates) != 1:
            return None
        rule, field, element = candidates[0]
        access = f"(({rule.type_name} *)(uintptr_t)({base}))->{field.name}"
        if field.is_array:
            access += f"[{element}]"
        return access

    def recover_store_target(self, address: str, type_id: int) -> str | None:
        absolute = self._absolute_field_value(address, 0, type_id)
        if absolute is not None:
            return absolute
        for rule in self.rules:
            index = rule.index_from_pointer(address)
            if index is not None:
                return self._field_value(rule, index, 0, type_id)
        return None

    def pointer_type_for_array(self, array_name: str) -> str | None:
        for rule in self.rules:
            if rule.array_name == array_name:
                return f"{rule.type_name} *"
        return None

    def pointer_field(self, pointer_type: str, byte_offset: int, type_id: int) -> str | None:
        normalized = pointer_type.strip().removesuffix("*").strip()
        for rule in self.rules:
            if rule.type_name != normalized:
                continue
            located = rule.field_at(byte_offset, type_id)
            if located is None:
                return None
            field, element = located
            return field.name + (f"[{element}]" if field.is_array else "")
        return None

    def recover_string_address(self, text: str) -> str:
        for rule in self.rules:
            index = rule.index_from_pointer(text)
            if index is None or rule.default_string_field is None:
                continue
            field = rule.field_named(rule.default_string_field)
            if field is not None:
                return rule.member(index, field)
        return text

    def declarations_for(self, text: str) -> list[str]:
        declarations: list[str] = []
        for rule in self.rules:
            if f"{rule.array_name}[" in text or f"({rule.type_name} *)" in text:
                declarations.append(rule.declaration())
        return declarations

    def recovered_copy_size(self, address: str, byte_count: int) -> str:
        for rule in self.rules:
            if byte_count == rule.size and rule.index_from_pointer(address) is not None:
                return f"sizeof({rule.type_name})"
        return str(byte_count)


STRUCTURE_REGISTRY = StructureRegistry(STRUCTURE_RULES)


def recover_structure_arithmetic(left: str, right: str) -> str | None:
    return STRUCTURE_REGISTRY.recover_arithmetic(left, right)


def recover_structure_field_load(base: str, byte_offset: int, type_id: int) -> str | None:
    return STRUCTURE_REGISTRY.recover_field_load(base, byte_offset, type_id)


def recover_structure_store_target(address: str, type_id: int) -> str | None:
    return STRUCTURE_REGISTRY.recover_store_target(address, type_id)


def structure_pointer_type(array_name: str) -> str | None:
    return STRUCTURE_REGISTRY.pointer_type_for_array(array_name)


def structure_pointer_field(pointer_type: str, byte_offset: int, type_id: int) -> str | None:
    return STRUCTURE_REGISTRY.pointer_field(pointer_type, byte_offset, type_id)


def recover_structure_string_address(text: str) -> str:
    return STRUCTURE_REGISTRY.recover_string_address(text)


def structure_declarations(text: str) -> list[str]:
    return STRUCTURE_REGISTRY.declarations_for(text)


def recovered_copy_size(address: str, byte_count: int) -> str:
    return STRUCTURE_REGISTRY.recovered_copy_size(address, byte_count)
