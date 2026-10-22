"""Semantic cleanup for self-describing binary resource loaders.

The VM compiler emits ordinary BPD instructions for this module, so generic
stack lifting cannot know that a blob starts with a 64-byte header and that
the values inserted into the two keyed tables have different layouts.  This
module keeps that knowledge at the C-emission boundary.  Recognition is based
on the resource's self-describing magic and record operations rather than a
source filename, so renamed modules are handled identically.
"""

from __future__ import annotations

import re


HEADER_DECLARATION = """#pragma pack(push, 1)
typedef struct BPDAnimationDefinitionHeader {
    char type_name[32];
    uint16_t version_minor;
    uint16_t version_major;
    uint32_t mode;
    uint32_t record_count;
    uint8_t reserved[20];
} BPDAnimationDefinitionHeader;

typedef struct BPDAnimationDefinitionValue {
    int32_t string_ids[3];
    int32_t parameters[40];
} BPDAnimationDefinitionValue;

typedef struct BPDAnimationDefinitionSetValue {
    int32_t key_id;
    int32_t slot_ids[20];
} BPDAnimationDefinitionSetValue;
#pragma pack(pop)"""


def _matching_brace(text: str, opening: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _function_span(source: str, name: str) -> tuple[int, int] | None:
    match = re.search(
        rf"(?m)^static int32_t {re.escape(name)}\([^\n]*\)\n\{{",
        source,
    )
    if match is None:
        return None
    closing = _matching_brace(source, source.find("{", match.start()))
    if closing is None:
        return None
    return match.start(), closing + 1


def _replace_function(source: str, name: str, signature: str, body: str) -> str:
    span = _function_span(source, name)
    if span is None:
        return source
    start, end = span
    replacement = f"static int32_t {name}({signature})\n{{\n{body}\n}}"
    return source[:start] + replacement + source[end:]


def _replace_prototype(source: str, name: str, signature: str) -> str:
    return re.sub(
        rf"(?m)^static int32_t {re.escape(name)}\([^\n]*\);$",
        f"static int32_t {name}({signature});",
        source,
    )


def _find_function_by_marker(source: str, marker: str) -> str | None:
    candidates = re.findall(
        r"(?m)^static int32_t ([A-Za-z_][A-Za-z0-9_]*)\([^\n]*\)\n\{",
        source,
    )
    for name in candidates:
        span = _function_span(source, name)
        if span is not None and marker in source[span[0]:span[1]]:
            return name
    return None


def _table_name(source: str, function: str) -> str:
    span = _function_span(source, function)
    body = source[span[0]:span[1]] if span is not None else ""
    match = re.search(
        r"sys1_keyed_value_table_set\((?P<table>[A-Za-z_][A-Za-z0-9_]*),",
        body,
    )
    return match.group("table") if match is not None else "global_animation_definitions"


def _semantic_string_reader(name: str) -> tuple[str, str]:
    signature = (
        "char *text, int32_t *string_id, const uint8_t *data, "
        "int32_t *cursor, int32_t size, int32_t intern"
    )
    body = """    uint16_t length = 0;
    int32_t start = *cursor;
    if (start < 0 || start + 2 > size) {
        return 0;
    }
    memcpy(&length, data + start, sizeof(length));
    *cursor = start + 2;
    if (*cursor + (int32_t)length > size) {
        return 0;
    }
    memcpy(text, data + *cursor, length);
    text[length] = '\\0';
    *cursor += length;
    if (length == 0 || !intern) {
        *string_id = -1;
    } else {
        strlower_inplace(text);
        *string_id = sys1_string_array_table_append(1, text);
    }
    return 1;"""
    return signature, body


def _semantic_definition_parser(name: str, reader: str, table: str) -> tuple[str, str]:
    signature = "const uint8_t *data, int32_t size"
    body = f"""    const BPDAnimationDefinitionHeader *header =
        (const BPDAnimationDefinitionHeader *)(const void *)data;
    int32_t cursor = (int32_t)sizeof(*header);
    char definition_key[65536];
    char animation_name[65536];
    char animation_option[65536];
    BPDAnimationDefinitionValue value = {{0}};

    for (uint32_t record = 0; record < header->record_count; ++record) {{
        if (!{reader}(definition_key, &value.string_ids[0], data, &cursor, size, 1)
            || !{reader}(animation_name, &value.string_ids[1], data, &cursor, size, 1)
            || !{reader}(animation_option, &value.string_ids[2], data, &cursor, size, 1)) {{
            return 0;
        }}
        if (cursor < 0 || cursor + (int32_t)sizeof(value.parameters) > size) {{
            return 0;
        }}
        memcpy(value.parameters, data + cursor, sizeof(value.parameters));
        cursor += (int32_t)sizeof(value.parameters);

        /* These are runtime normalization rules, not file decoding. */
        if (value.parameters[5] < 3
            && (value.parameters[7] == 4 || value.parameters[7] == 7)) {{
            value.parameters[5] = 3;
        }}
        if (value.parameters[5] < 4
            && (value.parameters[7] == 5 || value.parameters[7] == 8)) {{
            value.parameters[5] = 4;
        }}
        value.parameters[3] = value.parameters[3] * 256 / 100;
        (void)sys1_keyed_value_table_set({table}, definition_key, &value);
    }}
    return 1;"""
    return signature, body


def _semantic_set_parser(name: str, reader: str, table: str) -> tuple[str, str]:
    signature = "const uint8_t *data, int32_t size"
    body = f"""    const BPDAnimationDefinitionHeader *header =
        (const BPDAnimationDefinitionHeader *)(const void *)data;
    int32_t cursor = (int32_t)sizeof(*header);
    const uint32_t slot_count = header->mode == 0 ? 16u : 20u;
    const uint32_t intern_start = header->mode == 0 ? 13u : 20u;
    char set_key[65536];
    char text[65536];
    BPDAnimationDefinitionSetValue value;

    for (uint32_t record = 0; record < header->record_count; ++record) {{
        for (uint32_t slot = 0; slot < 20; ++slot) {{
            value.slot_ids[slot] = -1;
        }}
        if (!{reader}(set_key, &value.key_id, data, &cursor, size, 1)) {{
            return 0;
        }}
        for (uint32_t slot = 0; slot < slot_count; ++slot) {{
            if (!{reader}(text, &value.slot_ids[slot], data, &cursor, size,
                          slot >= intern_start)) {{
                return 0;
            }}
        }}
        (void)sys1_keyed_value_table_set({table}, set_key, &value);
    }}
    return 1;"""
    return signature, body


def _rewrite_resource_loader(source: str) -> str:
    """Make the resource-loader byte/text boundary explicit."""

    loader = None
    candidates = re.findall(
        r"(?m)^static int32_t ([A-Za-z_][A-Za-z0-9_]*)\([^\n]*\)\n\{",
        source,
    )
    for candidate in candidates:
        span = _function_span(source, candidate)
        body = source[span[0]:span[1]] if span is not None else ""
        if (
            "sys1_file_size" in body
            and '"BurikoAnimationDefinition"' in body
            and '"BurikoAnimationDefinitionSet"' in body
        ):
            loader = candidate
            break
    if loader is None:
        return source
    span = _function_span(source, loader)
    if span is None:
        return source
    start, end = span
    body = source[start:end]
    blob = re.search(r"const char \*(?P<name>[A-Za-z_]\w*) = 0;", body)
    if blob is None:
        return source
    blob_name = blob.group("name")
    body = body.replace(blob.group(0), f"uint8_t *{blob_name} = 0;")
    body = body.replace(
        f"{blob_name} = (const char *)(uintptr_t)(",
        f"{blob_name} = (uint8_t *)(uintptr_t)(",
    )
    body = re.sub(
        rf"strcmp\({re.escape(blob_name)}, (\"BurikoAnimationDefinition(?:Set)?\")\)",
        rf"strcmp((const char *){blob_name}, \1)",
        body,
    )
    return source[:start] + body + source[end:]


def optimize_binary_resource_source(source: str) -> str:
    """Recover animation-definition layouts from generated C text.

    Function names, local names and source filenames are intentionally
    ignored.  The two magic strings and the record-size operations form the
    format signature.
    """

    if (
        '"BurikoAnimationDefinition"' not in source
        or '"BurikoAnimationDefinitionSet"' not in source
    ):
        return source

    # A Definition record has a fixed 160-byte integer tail.  The set parser
    # is the other table setter and contains the 20-slot mode loop.
    definition = None
    set_parser = None
    candidates = re.findall(
        r"(?m)^static int32_t ([A-Za-z_][A-Za-z0-9_]*)\([^\n]*\)\n\{",
        source,
    )
    for candidate in candidates:
        span = _function_span(source, candidate)
        body = source[span[0]:span[1]] if span is not None else ""
        if "sys1_keyed_value_table_set(" not in body:
            continue
        if "memcpy(" in body and ("0xa0" in body or " 160" in body):
            definition = candidate
        elif re.search(r"< 20", body):
            set_parser = candidate
    reader = None
    for candidate in candidates:
        span = _function_span(source, candidate)
        body = source[span[0]:span[1]] if span is not None else ""
        if (
            "sys1_string_array_table_append(" in body
            and "strlower_inplace(" in body
            and "memcpy(" in body
        ):
            reader = candidate
            break
    if definition is None or set_parser is None or reader is None:
        return source

    definition_table = _table_name(source, definition)
    set_table = _table_name(source, set_parser)

    for function, signature in (
        (definition, "const uint8_t *data, int32_t size"),
        (set_parser, "const uint8_t *data, int32_t size"),
        (reader, _semantic_string_reader(reader)[0]),
    ):
        source = _replace_prototype(source, function, signature)

    reader_signature, reader_body = _semantic_string_reader(reader)
    source = _replace_function(source, reader, reader_signature, reader_body)
    definition_signature, definition_body = _semantic_definition_parser(
        definition, reader, definition_table
    )
    source = _replace_function(source, definition, definition_signature, definition_body)
    set_signature, set_body = _semantic_set_parser(set_parser, reader, set_table)
    source = _replace_function(source, set_parser, set_signature, set_body)

    source = _rewrite_resource_loader(source)

    marker = '#include <string.h>\n'
    if "typedef struct BPDAnimationDefinitionHeader" not in source:
        source = source.replace(marker, marker + "\n" + HEADER_DECLARATION + "\n", 1)
    return source
