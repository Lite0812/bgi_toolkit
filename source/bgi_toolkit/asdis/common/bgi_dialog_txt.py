from ...i18n import tr
import os
import re
import tempfile

from .bgi_dialog_json import (
    extract_dialog_entries,
    analyze_calls,
    write_dialog_metadata,
    entries_for_import,
    apply_dialog_replacements,
    extract_push_string_entries_from_bpd,
    _replace_bpd_string_line,
    _replace_push_string,
    _replace_v0_call_string,
    _restore_message_suffix
)


RE_TARGET_LINE = re.compile(r'^\s*★(\d{6})([NTS])★(.*)$')
RE_TEXT_ESCAPE = re.compile(r'\\([\\nrt])')


def _escape_txt_text(text):
    return str(text).replace('\\', '\\\\').replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t')


def _unescape_txt_text(text):
    def repl(match):
        value = match.group(1)
        if value == 'n':
            return '\n'
        if value == 'r':
            return '\r'
        if value == 't':
            return '\t'
        return '\\'
    return RE_TEXT_ESCAPE.sub(repl, str(text))


def _build_units(entries):
    units = []
    for entry in entries:
        has_name = entry["name"] is not None and (entry["name"] != "" or entry["message_line_index"] is None)
        if has_name:
            units.append({
                "type": "N",
                "text": entry["name"],
                "line_index": entry["name_line_index"],
                "arg_index": entry.get("name_arg_index")
            })
        if entry["message_line_index"] is None:
            continue
        msg_type = "S" if entry.get("is_select") else "T"
        units.append({
            "type": msg_type,
            "text": entry["message"],
            "line_index": entry["message_line_index"],
            "arg_index": entry.get("message_arg_index"),
            "message_suffix": entry.get("message_suffix", "")
        })
    return units


def extract_dialog_txt_from_bsd(input_path, output_txt, user_function_names=None):
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    calls = analyze_calls(lines)
    entries = extract_dialog_entries(lines, user_function_names=user_function_names, calls=calls)
    units = _build_units(entries)
    out_lines = []
    for idx, unit in enumerate(units, start=1):
        tag = f"{idx:06d}{unit['type']}"
        src_text = _escape_txt_text(unit["text"])
        out_lines.append(f"☆{tag}☆{src_text}\n")
        out_lines.append(f"★{tag}★{src_text}\n")
        out_lines.append("\n")
    out_dir = os.path.dirname(output_txt)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_txt, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)
    write_dialog_metadata(output_txt, lines, entries, user_function_names, calls=calls)
    return len(entries), len(units)

def extract_push_string_txt_from_bpd(input_path, output_txt):
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    entries, _ = extract_push_string_entries_from_bpd(lines)
    out_lines = []
    for idx, entry in enumerate(entries, start=1):
        tag = f"{idx:06d}T"
        src_text = _escape_txt_text(entry["text"])
        out_lines.append(f"☆{tag}☆{src_text}\n")
        out_lines.append(f"★{tag}★{src_text}\n")
        out_lines.append("\n")
    out_dir = os.path.dirname(output_txt)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_txt, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)
    return len(entries), len(entries)


def import_dialog_txt_to_bsd(input_bsd, input_txt, output_bsd, user_function_names=None):
    with open(input_bsd, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    entries = entries_for_import(lines, input_txt, user_function_names)
    units = _build_units(entries)
    with open(input_txt, 'r', encoding='utf-8') as f:
        txt_lines = [line.rstrip('\r\n') for line in f]
    if txt_lines:
        txt_lines[0] = txt_lines[0].lstrip('\ufeff')

    target_items = []
    for line in txt_lines:
        match = RE_TARGET_LINE.match(line)
        if match:
            target_items.append({
                "id": int(match.group(1)),
                "type": match.group(2),
                "text": _unescape_txt_text(match.group(3)),
            })
    if not target_items:
        out_dir = os.path.dirname(output_bsd)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_bsd, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        return len(entries), 0, len(units)

    if len(target_items) != len(units):
        raise Exception(tr('TXT 条目数与脚本条目数不一致: txt={0} expected={1}', len(target_items), len(units)))

    applied = 0
    replacements = []
    for idx, (target, unit) in enumerate(zip(target_items, units), start=1):
        if target["id"] != idx:
            raise Exception(tr('第 {0} 项编号不匹配: txt={1:06d} expected={2:06d}', idx, target['id'], idx))
        if target["type"] != unit["type"]:
            raise Exception(tr('第 {0} 项类型不匹配: txt={1} expected={2}', idx, target['type'], unit['type']))
        line_index = unit["line_index"]
        if line_index is None:
            raise Exception(tr('第 {0} 项目标行无效', idx))
        new_text = str(target["text"])
        if unit["type"] in {"T", "S"}:
            new_text = _restore_message_suffix(new_text, unit.get("message_suffix"))
        replacements.append((line_index, unit.get("arg_index"), new_text))
        applied += 1
    apply_dialog_replacements(lines, replacements)

    out_dir = os.path.dirname(output_bsd)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_bsd, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    return len(entries), applied, len(units)

def import_push_string_txt_to_bpd(input_bpd, input_txt, output_bpd):
    with open(input_bpd, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    entries, string_entries = extract_push_string_entries_from_bpd(lines)
    with open(input_txt, 'r', encoding='utf-8') as f:
        txt_lines = [line.rstrip('\r\n') for line in f]
    if txt_lines:
        txt_lines[0] = txt_lines[0].lstrip('\ufeff')

    target_items = []
    for line in txt_lines:
        match = RE_TARGET_LINE.match(line)
        if match:
            target_items.append({
                "id": int(match.group(1)),
                "type": match.group(2),
                "text": _unescape_txt_text(match.group(3)),
            })
    if not target_items:
        out_dir = os.path.dirname(output_bpd)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_bpd, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        return len(entries), 0, len(entries)

    if len(target_items) != len(entries):
        raise Exception(tr('TXT 条目数与 push_string 条目数不一致: txt={0} expected={1}', len(target_items), len(entries)))
    if string_entries and len(string_entries) != len(entries):
        raise Exception(tr('BPD 字符串区条目数与 push_string 条目数不一致: strings={0} push={1}', len(string_entries), len(entries)))

    applied = 0
    for idx, (target, entry) in enumerate(zip(target_items, entries), start=1):
        if target["id"] != idx:
            raise Exception(tr('第 {0} 项编号不匹配: txt={1:06d} expected={2:06d}', idx, target['id'], idx))
        if target["type"] != 'T':
            raise Exception(tr('第 {0} 项类型不匹配: txt={1} expected=T', idx, target['type']))
        new_text = str(target["text"])
        lines[entry["line_index"]] = _replace_push_string(lines[entry["line_index"]], new_text)
        if string_entries:
            lines[string_entries[idx - 1]["line_index"]] = _replace_bpd_string_line(lines[string_entries[idx - 1]["line_index"]], new_text)
        applied += 1

    out_dir = os.path.dirname(output_bpd)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_bpd, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    return len(entries), applied, len(entries)


def extract_dialog_txt_from_script(input_script, output_txt, encoding='cp932', fallback_encoding='gbk', user_function_names=None):
    from ..service import AsdisOptions, disassemble_story
    with tempfile.TemporaryDirectory(prefix='bgi_txt_extract_') as td:
        temp_bsd = os.path.join(td, os.path.basename(input_script) + '.bsd')
        disassemble_story(input_script, temp_bsd, AsdisOptions(encoding=encoding, fallback_encoding=fallback_encoding), use_function_names=False)
        return extract_dialog_txt_from_bsd(temp_bsd, output_txt, user_function_names=user_function_names)


def import_dialog_txt_to_script(
    input_script,
    input_txt,
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
    with tempfile.TemporaryDirectory(prefix='bgi_txt_import_') as td:
        base_name = os.path.basename(input_script)
        temp_src_bsd = os.path.join(td, base_name + '.src.bsd')
        temp_out_bsd = os.path.join(td, base_name + '.out.bsd')
        _, version = disassemble_story(input_script, temp_src_bsd, AsdisOptions(encoding=dis_encoding, fallback_encoding=dis_fallback), use_function_names=False)
        count, applied, units = import_dialog_txt_to_bsd(
            temp_src_bsd,
            input_txt,
            temp_out_bsd,
            user_function_names=user_function_names
        )
        assemble_story(temp_out_bsd, output_script, AsdisOptions(encoding=encoding, fallback_encoding=fallback_encoding, script_version=version))
        return count, applied, units
