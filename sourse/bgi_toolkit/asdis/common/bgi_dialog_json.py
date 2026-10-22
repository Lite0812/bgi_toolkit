from ...i18n import tr
import os
import json
import re
import tempfile
from pathlib import Path
from .dialog_analysis import analyze_calls, canonical_function, parse_rules, infer_roles, is_standard, describe_calls, contains_non_ascii, split_role_spec, assign_pair_groups, collect_known_names
from . import asdis
from ...bsd2c.asm_parser import strip_inline_comment

RE_PUSH_STRING = re.compile(r'^\s*push_string\("((?:\\.|[^"\\])*)"\);\s*$')
RE_V0_CALL = re.compile(r'^(\s*)((?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*)\((.*)\);\s*(//.*)?$')
RE_V0_STR = re.compile(r'"((?:\\.|[^"\\])*)"')
RE_BPD_STRING = re.compile(r'^\s*"((?:\\.|[^"\\])*)"\s*$')

DIALOG_CONTROL_SUFFIX_CHARS = '<>&.'

def _toolkit_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _split_dialog_message_suffix(text):
    value = '' if text is None else str(text)
    suffix_chars = []
    while value and value[-1] in DIALOG_CONTROL_SUFFIX_CHARS:
        suffix_chars.append(value[-1])
        value = value[:-1]
    suffix_chars.reverse()
    return value, ''.join(suffix_chars)

def _make_dialog_entry(
    *,
    name,
    message,
    name_line_index,
    message_line_index,
    is_select,
    name_arg_index=None,
    message_arg_index=None,
    call_line=None,
    user_func_name=None
):
    visible_message, message_suffix = _split_dialog_message_suffix(message)
    if visible_message == "　" and not message_suffix:
        return None
    entry = {
        "name": name,
        "message": visible_message,
        "message_suffix": message_suffix,
        "name_line_index": name_line_index,
        "message_line_index": message_line_index,
        "is_select": is_select
    }
    if name_arg_index is not None:
        entry["name_arg_index"] = name_arg_index
    if message_arg_index is not None:
        entry["message_arg_index"] = message_arg_index
    if call_line is not None:
        entry["call_line"] = call_line
    if user_func_name is not None:
        entry["user_func_name"] = user_func_name
    return entry

def _restore_message_suffix(text, suffix):
    value = '' if text is None else str(text)
    tail = '' if suffix is None else str(suffix)
    if tail and not value.endswith(tail):
        return value + tail
    return value

def _extract_push_string(line):
    match = RE_PUSH_STRING.match(strip_inline_comment(line.rstrip('\r\n')))
    if not match:
        return None
    return asdis.unescape(match.group(1))

def _replace_push_string(line, new_text):
    if _extract_push_string(line) is None:
        return line
    return _replace_v0_call_string(line, 0, new_text)


def _extract_bpd_string_line(line):
    match = RE_BPD_STRING.match(line.rstrip('\r\n'))
    if not match:
        return None
    return asdis.unescape(match.group(1))

def _replace_bpd_string_line(line, new_text):
    match = RE_BPD_STRING.match(line.rstrip('\r\n'))
    if not match:
        return line
    indent_match = re.match(r'^(\s*)', line)
    indent = indent_match.group(1) if indent_match else ''
    suffix = '\n' if line.endswith('\n') else ''
    return f'{indent}"{asdis.escape(new_text)}"{suffix}'

def _replace_v0_call_string(line, literal_index, new_text):
    code = strip_inline_comment(line.rstrip('\r\n'))
    match = RE_V0_CALL.match(code)
    if not match:
        raise ValueError(tr('字符串源调用无法定位'))
    literals = list(RE_V0_STR.finditer(match.group(3)))
    index = int(literal_index)
    if not -len(literals) <= index < len(literals):
        raise ValueError(tr('字符串参数索引超出范围'))
    start, end = literals[index].span(1)
    offset = match.start(3)
    return line[:offset + start] + asdis.escape(new_text) + line[offset + end:]


def normalize_user_function_names(user_function_names):
    if not user_function_names:
        return []
    if isinstance(user_function_names, str):
        user_function_names = re.split(r'[\r\n,;]+', user_function_names)
    return list(dict.fromkeys(str(item).strip() for item in user_function_names if str(item).strip()))


def extract_dialog_entries(lines, user_function_names=None, *, calls=None):
    calls = analyze_calls(lines) if calls is None else calls
    rules = parse_rules(user_function_names, v0=bool(calls and calls[0].v0))
    known_names = collect_known_names(calls)
    entries = []
    used = set()
    pending_name = None

    def append_entry(name_arg, message_arg, role, call, pair_id=None):
        refs = [(arg.line_index, arg.literal_index) for arg in (name_arg, message_arg) if arg]
        if not refs or all(ref in used for ref in refs):
            return
        entry = _make_dialog_entry(
            name=name_arg.text if name_arg else None,
            message=message_arg.text if message_arg else "",
            name_line_index=name_arg.line_index if name_arg else None,
            message_line_index=message_arg.line_index if message_arg else None,
            name_arg_index=name_arg.literal_index if name_arg else None,
            message_arg_index=message_arg.literal_index if message_arg else None,
            is_select=role == "select", call_line=call.line,
        )
        if entry:
            entry["function"] = call.function
            entry["name_parameter"] = name_arg.position if name_arg else None
            entry["message_parameter"] = message_arg.position if message_arg else None
            if pair_id is not None:
                entry["pair_id"] = pair_id
            entries.append(entry)
            used.update(refs)

    for call in calls:
        key = canonical_function(call.function)
        standard = is_standard(call)
        if not standard and key not in rules:
            continue
        roles = infer_roles(call, known_names)
        pair_ids = {}
        for pos, spec in rules.get(key, {}).items():
            role, pair_id = split_role_spec(spec)
            if role != "auto":
                roles[pos] = role
            if pair_id is not None:
                pair_ids[pos] = pair_id
        if call.v0 and call.function in {"f_010", "f_PrintMessage"} and pending_name:
            # v0 supplies its speaker in the preceding SetName call.
            for pos, role in roles.items():
                if role == "message" and pos not in pair_ids:
                    pair_ids[pos] = pending_name[1]
        # Hidden ASCII resource parameters must not determine pair direction.
        visible_roles = {arg.position: roles[arg.position] for arg in call.arguments
            if contains_non_ascii(arg.text) and arg.position in roles}
        pair_ids = assign_pair_groups(visible_roles, pair_ids)
        names = [(arg, pair_ids.get(arg.position)) for arg in call.arguments if roles.get(arg.position) == "name"]
        messages = [arg for arg in call.arguments if roles.get(arg.position) in {"message", "select"}]
        if call.v0 and call.function in {"f_014", "f_SetName"}:
            if pending_name:
                append_entry(pending_name[0], None, "name", pending_name[2], pending_name[1])
            pending_name = (*names[0], call) if names and names[0][1] else None
            if names and not names[0][1]:
                append_entry(names[0][0], None, "name", call, 0)
            continue
        if call.v0 and call.function in {"f_010", "f_PrintMessage"} and pending_name:
            names = [(pending_name[0], pending_name[1]), *names]
            pending_name = None
        numbered_names = {}
        for name, pair_id in names:
            if pair_id:
                if pair_id in numbered_names:
                    raise ValueError(tr('{0} 的配对编号 {1} 对应多个姓名参数，请使用不同编号', call.function, pair_id))
                numbered_names[pair_id] = name
        consumed_names = set()
        for arg in messages:
            pair_id = pair_ids.get(arg.position) if roles[arg.position] == "message" else None
            name = None
            if roles[arg.position] == "message":
                if pair_id is not None:
                    name = numbered_names.get(pair_id)
            if name is not None:
                consumed_names.add((name.line_index, name.literal_index))
            append_entry(name, arg, roles[arg.position], call, pair_id)
        for name, pair_id in names:
            if (name.line_index, name.literal_index) not in consumed_names:
                append_entry(name, None, "name", call, pair_id)
    if pending_name:
        append_entry(pending_name[0], None, "name", pending_name[2], pending_name[1])

    # Choice text is recognized from real calls and parameter roles. A move(2)
    # is only a VM store and must not independently classify text as a choice.
    entries.sort(key=lambda entry: (entry.get("call_line") or entry["message_line_index"] + 1,
        entry.get("message_line_index") if entry.get("message_line_index") is not None else entry["name_line_index"]))
    return filter_dialog_entries(entries)


def filter_dialog_entries(entries):
    filtered = []
    for original in entries:
        entry = dict(original)
        for role in ("name", "message"):
            if contains_non_ascii(entry[role]):
                continue
            entry[role] = None if role == "name" else ""
            entry[role + "_line_index"] = None
            entry[role + "_parameter"] = None
            entry.pop(role + "_arg_index", None)
            if role == "message":
                entry["message_suffix"] = ""
                entry["is_select"] = False
        if entry["name_line_index"] is not None or entry["message_line_index"] is not None:
            filtered.append(entry)
    paired_names = {(entry["name_line_index"], entry.get("name_arg_index")) for entry in filtered
        if entry["name_line_index"] is not None and entry["message_line_index"] is not None}
    standalone_seen = set()
    result = []
    for entry in filtered:
        if entry["message_line_index"] is None:
            ref = (entry["name_line_index"], entry.get("name_arg_index"))
            if ref in paired_names or ref in standalone_seen:
                continue
            standalone_seen.add(ref)
        result.append(entry)
    return result


def write_dialog_metadata(output, lines, entries, user_function_names, *, calls=None):
    payload = {"format": "bgi-dialog-metadata", "version": 2,
        "rules": normalize_user_function_names(user_function_names),
        "entry_count": len(entries)}
    Path(str(output) + ".meta.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def entries_for_import(lines, translation_path, user_function_names):
    metadata_path = Path(str(translation_path) + ".meta.json")
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("format") != "bgi-dialog-metadata" or metadata.get("version") not in (1, 2):
            raise ValueError(tr('不支持的文本定位元数据格式'))
        if metadata['version'] == 2:
            rules, count = metadata.get('rules'), metadata.get('entry_count')
            if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules) or type(count) is not int or count < 0:
                raise ValueError(tr('文本定位元数据的规则或条目数无效'))
            entries = extract_dialog_entries(lines, rules)
            if len(entries) != count:
                raise ValueError(tr('按原提取规则重建的文本条目数不一致，请重新提取'))
            return entries
        # Legacy exports may include ASCII strings and move(2) choices that the
        # current extractor omits. Keep their explicit locations, but read text
        # and control suffixes from the current script without comparing content.
        entries = metadata.get("entries")
        if not isinstance(entries, list):
            raise ValueError(tr('文本定位元数据缺少 entries'))
        for entry in entries:
            if not isinstance(entry, dict) or not {"name", "message", "name_line_index", "message_line_index", "is_select"} <= entry.keys():
                raise ValueError(tr('文本定位元数据条目格式错误'))
            for role in ("name", "message"):
                line_index = entry[role + "_line_index"]
                if line_index is None:
                    continue
                if type(line_index) is not int or not 0 <= line_index < len(lines):
                    raise ValueError(tr('文本定位元数据的源行无效'))
                literal_index = entry.get(role + "_arg_index")
                if literal_index is None:
                    actual = _extract_push_string(lines[line_index])
                else:
                    match = RE_V0_CALL.match(strip_inline_comment(lines[line_index].rstrip('\r\n')))
                    literals = list(RE_V0_STR.finditer(match.group(3))) if match else []
                    if type(literal_index) is not int or not 0 <= literal_index < len(literals):
                        raise ValueError(tr('文本定位元数据的参数索引无效'))
                    actual = asdis.unescape(literals[literal_index][1])
                if actual is None:
                    raise ValueError(tr('文本定位元数据的源行无效'))
                if role == "message":
                    actual, entry["message_suffix"] = _split_dialog_message_suffix(actual)
                entry[role] = actual
        return entries
    return extract_dialog_entries(lines, user_function_names)


def extract_dialog_json_from_bsd(input_path, output_json, user_function_names=None):
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    calls = analyze_calls(lines)
    entries = extract_dialog_entries(lines, user_function_names=user_function_names, calls=calls)
    payload = []
    for e in entries:
        item = {}
        if e["name"] is not None and (e["name"] != "" or e["message_line_index"] is None):
            item["name"] = e["name"]
        if e["message_line_index"] is not None:
            item["message"] = e["message"]
        payload.append(item)
    out_dir = os.path.dirname(output_json)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    write_dialog_metadata(output_json, lines, entries, user_function_names, calls=calls)
    return len(entries)

def extract_push_string_entries_from_bpd(lines):
    push_entries = []
    string_entries = []
    in_strings = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '#strings':
            in_strings = True
            continue
        if not in_strings:
            text = _extract_push_string(line)
            if text is not None:
                push_entries.append({
                    "text": text,
                    "line_index": idx
                })
            continue
        if stripped.startswith('#strdata'):
            continue
        text = _extract_bpd_string_line(line)
        if text is not None:
            string_entries.append({
                "text": text,
                "line_index": idx
            })
    return push_entries, string_entries

def extract_push_string_json_from_bpd(input_bpd, output_json):
    with open(input_bpd, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    entries, _ = extract_push_string_entries_from_bpd(lines)
    payload = [{"text": entry["text"]} for entry in entries]
    out_dir = os.path.dirname(output_json)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(entries)

def apply_dialog_replacements(lines, replacements):
    targets = {}
    for line_index, literal_index, text in replacements:
        key = (line_index, literal_index)
        if key in targets and targets[key] != text:
            raise ValueError(tr('同一字符串源被多处引用，但译文不一致（BSD 第 {0} 行），请统一译文', line_index + 1))
        targets[key] = text
    for (line_index, literal_index), text in targets.items():
        if literal_index is None:
            lines[line_index] = _replace_push_string(lines[line_index], text)
        else:
            lines[line_index] = _replace_v0_call_string(lines[line_index], int(literal_index), text)


def import_dialog_json_to_bsd(input_bsd, input_json, output_bsd, user_function_names=None):
    with open(input_bsd, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    with open(input_json, 'r', encoding='utf-8') as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise Exception(tr('JSON 格式错误: 需要数组格式'))
    entries = entries_for_import(lines, input_json, user_function_names)
    if len(items) != len(entries):
        raise Exception(tr('JSON 条目数与对话条目数不一致: json={0} bsd={1}', len(items), len(entries)))
    applied = 0
    replacements = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise Exception(tr('第 {0} 项不是对象', idx))
        unknown_keys = set(item.keys()) - {"name", "message"}
        if unknown_keys:
            raise Exception(tr('第 {0} 项包含非法字段: {1}', idx, sorted(unknown_keys)))
        entry = entries[idx]
        if entry["message_line_index"] is not None and "message" not in item:
            raise Exception(tr('第 {0} 项缺少 message', idx))
        if entry["message_line_index"] is None and "name" not in item:
            raise Exception(tr('第 {0} 项缺少 name', idx))
        if "name" in item and entry["name_line_index"] is not None:
            replacements.append((entry["name_line_index"], entry.get("name_arg_index"), str(item["name"])))
            applied += 1
        if entry["message_line_index"] is None:
            continue
        message_text = _restore_message_suffix(item["message"], entry.get("message_suffix"))
        replacements.append((entry["message_line_index"], entry.get("message_arg_index"), message_text))
        applied += 1
    apply_dialog_replacements(lines, replacements)
    out_dir = os.path.dirname(output_bsd)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_bsd, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    return len(entries), applied

def import_push_string_json_to_bpd(input_bpd, input_json, output_bpd):
    with open(input_bpd, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    with open(input_json, 'r', encoding='utf-8') as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise Exception(tr('JSON 格式错误: 需要数组格式'))
    entries, string_entries = extract_push_string_entries_from_bpd(lines)
    if len(items) != len(entries):
        raise Exception(tr('JSON 条目数与 push_string 条目数不一致: json={0} bpd={1}', len(items), len(entries)))
    if string_entries and len(string_entries) != len(entries):
        raise Exception(tr('BPD 字符串区条目数与 push_string 条目数不一致: strings={0} push={1}', len(string_entries), len(entries)))
    applied = 0
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise Exception(tr('第 {0} 项不是对象', idx))
        unknown_keys = set(item.keys()) - {"text"}
        if unknown_keys:
            raise Exception(tr('第 {0} 项包含非法字段: {1}', idx, sorted(unknown_keys)))
        if "text" not in item:
            raise Exception(tr('第 {0} 项缺少 text', idx))
        new_text = '' if item["text"] is None else str(item["text"])
        lines[entries[idx]["line_index"]] = _replace_push_string(lines[entries[idx]["line_index"]], new_text)
        if string_entries:
            lines[string_entries[idx]["line_index"]] = _replace_bpd_string_line(lines[string_entries[idx]["line_index"]], new_text)
        applied += 1
    out_dir = os.path.dirname(output_bpd)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_bpd, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    return len(entries), applied

def extract_dialog_json_from_script(input_script, output_json, encoding='cp932', fallback_encoding='gbk', user_function_names=None):
    from ..service import AsdisOptions, disassemble_story
    with tempfile.TemporaryDirectory(prefix='bgi_json_extract_') as td:
        temp_bsd = os.path.join(td, os.path.basename(input_script) + '.bsd')
        disassemble_story(input_script, temp_bsd, AsdisOptions(encoding=encoding, fallback_encoding=fallback_encoding), use_function_names=False)
        return extract_dialog_json_from_bsd(temp_bsd, output_json, user_function_names=user_function_names)

def import_dialog_json_to_script(
    input_script,
    input_json,
    output_script,
    encoding='cp932',
    fallback_encoding='gbk',
    source_encoding=None,
    source_fallback_encoding=None,
    user_function_names=None
):
    from ..service import AsdisOptions, disassemble_story, assemble_story
    dis_encoding = source_encoding or encoding
    dis_fallback = source_fallback_encoding or fallback_encoding
    with tempfile.TemporaryDirectory(prefix='bgi_json_import_') as td:
        base_name = os.path.basename(input_script)
        temp_src_bsd = os.path.join(td, base_name + '.src.bsd')
        temp_out_bsd = os.path.join(td, base_name + '.out.bsd')
        _, version = disassemble_story(input_script, temp_src_bsd, AsdisOptions(encoding=dis_encoding, fallback_encoding=dis_fallback), use_function_names=False)
        count, applied = import_dialog_json_to_bsd(
            temp_src_bsd,
            input_json,
            temp_out_bsd,
            user_function_names=user_function_names
        )
        assemble_story(temp_out_bsd, output_script, AsdisOptions(encoding=encoding, fallback_encoding=fallback_encoding, script_version=version))
        return count, applied
