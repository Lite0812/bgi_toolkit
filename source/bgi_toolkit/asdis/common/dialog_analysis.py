"""Stable call identities and text roles, using the BSD decompiler's stack lifter."""
from __future__ import annotations
from ...i18n import tr

from dataclasses import dataclass
from functools import lru_cache
import re

from ...bsd2c.asm_parser import AsmInstruction, parse_assembly_text
from ...bsd2c.builtin_functions import builtin_resolver_scope
from ...bsd2c.ignore_config import IgnoreBuiltinConfig
from ...bsd2c.emitter import match_select_setup_sources
from ...bsd2c.ir import AssignStmt, CallExpr, Function, LocalVar, StringLiteral
from ...bsd2c.lifter import (CORE_OPS, build_internal_arity_map, build_internal_call_name_map,
    lift_block, split_basic_blocks, token_to_expr, finalize_cfg, compute_reaching_value_states,
    expand_expr_with_reaching_values, rewrite_stmt_with_reaching_values, advance_reaching_value_state)
from ...bsd2c.signatures import load_builtin_signature_library
from . import asdis

ROLES = ("auto", "name", "message", "select", "ignore")
RAW_NAME = re.compile(r"^f_([0-9a-fA-F]{3,})$")


@lru_cache(maxsize=1)
def _stable_aliases():
    aliases = {}
    for signature in load_builtin_signature_library().signatures():
        raw = next((name.lower() for name in signature.aliases if RAW_NAME.fullmatch(name)), None)
        if raw:
            for name in (signature.name, *signature.aliases):
                aliases[name] = raw
    return aliases


def canonical_function(name, *, v0=False):
    return _canonical_function(str(name), v0)


@lru_cache(maxsize=8192)
def _canonical_function(name, v0):
    name = name.strip().rsplit("::", 1)[-1]
    if v0:
        name = {"f_PrintMessage": "f_010", "f_SetName": "f_014", "f_Select": "f_0b0"}.get(name, name)
    if RAW_NAME.fullmatch(name):
        return name.lower()
    return _stable_aliases().get(name, name.lstrip("_"))


def split_role_spec(spec):
    match = re.fullmatch(r"(auto|name|message|select|ignore)(?:#(0|[1-9]\d*))?", str(spec))
    if not match:
        raise ValueError(tr('参数角色格式错误: {0}', spec))
    role, number = match.groups()
    pair_id = int(number) if number else None
    if pair_id is not None and (role not in {"auto", "name", "message"} or pair_id > 2147483647):
        raise ValueError(tr('无效的配对编号: {0}', spec))
    return role, pair_id


def assign_pair_groups(roles, existing=None):
    """Pair nearby bodies with speakers; zero means explicit or standalone text."""
    paired = {pos: role for pos, role in roles.items() if role in {"name", "message"}}
    result = {pos: group for pos, group in (existing or {}).items() if pos in paired and group is not None}
    used = set(result.values())
    candidate = 1

    def allocate():
        nonlocal candidate
        while candidate in used:
            candidate += 1
        used.add(candidate)
        return candidate

    names = sorted(pos for pos, role in paired.items() if role == "name")
    for pos in names:
        if pos not in result:
            result[pos] = allocate()
    names = [pos for pos in names if result[pos]]
    # Repeated (text, speaker) layouts must pair forward, while
    # (speaker, text) layouts pair backward. Explicit standalone fields do not vote.
    first_message = next((pos for pos, role in sorted(paired.items())
        if role == "message" and result.get(pos) != 0), None)
    message_first = bool(names and first_message is not None and first_message < names[0])
    for pos, role in sorted(paired.items()):
        if role != "message" or pos in result:
            continue
        if names:
            if message_first:
                speaker = next((name for name in names if name > pos), names[-1])
            else:
                speaker = next((name for name in reversed(names) if name < pos), names[0])
            result[pos] = result[speaker]
        else:
            result[pos] = 0
    return result


def parse_rules(value, *, v0=False):
    """Examples: _Talk; f_321:1=name 2=message 3=ignore (positions are 1-based)."""
    if not value:
        return {}
    if isinstance(value, str):
        value = re.split(r"[;,\r\n]+", value)
    result = {}
    for item in value:
        item = str(item).strip()
        if not item:
            continue
        # A single colon separates roles; qualified function names remain valid.
        match = re.fullmatch(r"((?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)(?:\s*:\s*(.+))?", item)
        if not match:
            raise ValueError(tr('文本函数规则格式错误: {0}', item))
        roles = {}
        if match[2]:
            for token in re.split(r"[|\s]+", match[2].strip()):
                part = re.fullmatch(r"([1-9]\d*)=((?:auto|name|message|select|ignore)(?:#(?:0|[1-9]\d*))?)", token)
                if not part:
                    raise ValueError(tr('参数角色格式错误: {0}；示例 f_321:1=name 2=message', token))
                split_role_spec(part[2])
                roles[int(part[1])] = part[2]
        result[canonical_function(match[1], v0=v0)] = roles
    return result


@dataclass
class TextArgument:
    position: int
    text: str
    line_index: int
    literal_index: int | None = None
    label: str | None = None


@dataclass
class TextCall:
    function: str
    line: int
    arguments: list[TextArgument]
    arg_count: int
    v0: bool = False
    selection: bool = False
    outline_position: int | None = None


def _literal_argument(expr, position, lines):
    if not isinstance(expr, StringLiteral) or expr.source_line is None:
        return None
    index = expr.source_line - 1
    # Decode from the original assembly with its full escaping rules.
    literals = list(re.finditer(r'"((?:\\.|[^"\\])*)"', lines[index]))
    literal_index = expr.literal_index or 0
    if literal_index >= len(literals):
        return None
    return TextArgument(position, asdis.unescape(literals[literal_index][1]), index, expr.literal_index)


def analyze_calls(lines):
    with builtin_resolver_scope():
        return _analyze_calls(lines)


def _analyze_calls(lines):
    module = parse_assembly_text("".join(lines))
    v0 = any(line.strip() == "#v0" for line in lines)
    calls = []
    if v0:
        for function in module.functions:
            for item in function.items:
                if not isinstance(item, AsmInstruction) or item.opcode in CORE_OPS:
                    continue
                args = []
                literal_index = 0
                for pos, token in enumerate(item.args, 1):
                    expr = token_to_expr(token, item.line_no, literal_index)
                    arg = _literal_argument(expr, pos, lines)
                    if isinstance(expr, StringLiteral):
                        literal_index += 1
                    if arg:
                        args.append(arg)
                calls.append(TextCall(canonical_function(item.opcode, v0=True), item.line_no, args, len(item.args), True))
        return sorted(calls, key=lambda call: call.line)

    # Normalize only with the decompiler's fixed opcode aliases, never bss_mapping.json.
    normalized_opcodes = {}
    aliases = _stable_aliases()
    for function in module.functions:
        for item in function.items:
            if isinstance(item, AsmInstruction):
                normalized = normalized_opcodes.get(item.opcode)
                if normalized is None:
                    normalized = aliases.get(item.opcode.rsplit("::", 1)[-1], item.opcode)
                    raw = normalized.rsplit("::", 1)[-1]
                    if RAW_NAME.fullmatch(raw):
                        normalized = raw.lower()
                    normalized_opcodes[item.opcode] = normalized
                item.opcode = normalized
    arities = build_internal_arity_map(module)
    names = build_internal_call_name_map(module)
    library = load_builtin_signature_library()

    events = {}

    def observe(item, call, block):
        events.setdefault((id(block), len(block.body)), []).append((item, call))

    ignore_config = IgnoreBuiltinConfig(bsc=frozenset(), bs5_like=frozenset())
    for function in module.functions:
        blocks = [lift_block(block, arities.get(function.name, 0), module.source_type,
                arities, names, {}, library,
                ignore_config, call_observer=observe)
            for block in split_basic_blocks(function, module.source_type)]
        finalize_cfg(blocks)
        lifted = Function(function.name, "void", arities.get(function.name, 0), blocks=blocks)
        local_assignments = [stmt for block in blocks for stmt in block.body
            if isinstance(stmt, AssignStmt) and isinstance(stmt.target, LocalVar)]
        in_states = compute_reaching_value_states(lifted) if local_assignments else {}
        # Equal strings from different assignments must not be collapsed to one
        # writable source location at a control-flow join.
        origins = {}
        for stmt in local_assignments:
            if isinstance(stmt.value, StringLiteral):
                origins.setdefault(stmt.value.value, set()).add(stmt.value.source_line)
        for block in blocks:
            selections = {}
            cursor = 0
            while cursor < len(block.body):
                setup = match_select_setup_sources(block.body, cursor)
                if setup is None:
                    cursor += 1
                else:
                    end, options, _, outline = setup
                    selections[end - 2] = (options, outline)
                    cursor = end
            state = {offset: value for offset, value in in_states.get(block.label, {}).items()
                if not isinstance(value.expr, StringLiteral) or len(origins.get(value.expr.value, ())) <= 1}
            for index in range(len(block.body) + 1):
                for item, call in events.pop((id(block), index), ()):
                    args = [arg for pos, expr in enumerate(call.args, 1)
                        if (arg := _literal_argument(expand_expr_with_reaching_values(expr, state) if state else expr, pos, lines)) is not None]
                    selection = selections.get(index)
                    if selection is not None:
                        options, _ = selection
                        for position, expr in enumerate(options, len(call.args) + 1):
                            arg = _literal_argument(expr, position, lines)
                            if arg is not None:
                                arg.label = f"选项 {position - len(call.args)} · 缓冲区"
                                args.append(arg)
                        for arg in args:
                            if arg.position == 3:
                                arg.label = "选择提示 · 参数 3"
                    calls.append(TextCall(call.callee, item.line_no, args, len(call.args),
                        selection=selection is not None, outline_position=3 if selection is not None else None))
                if index < len(block.body):
                    stmt = block.body[index]
                    if isinstance(stmt, AssignStmt) and isinstance(stmt.target, LocalVar):
                        stmt = rewrite_stmt_with_reaching_values(stmt, state)
                        state = advance_reaching_value_state(stmt, state)
    observed_lines = {call.line for call in calls}
    for function in module.functions:
        for item in function.items:
            if isinstance(item, AsmInstruction) and item.line_no not in observed_lines and RAW_NAME.fullmatch(item.opcode):
                calls.append(TextCall(item.opcode, item.line_no, [], len(item.args)))
    return sorted(calls, key=lambda call: call.line)


SELECTION_OPCODES = {"f_160", "f_168", "f_169", "f_16a", "f_16b", "f_17c", "f_17e", "f_17f", "f_460"}


@lru_cache(maxsize=8192)
def _selection_function(function, v0):
    key = canonical_function(function, v0=v0)
    if RAW_NAME.fullmatch(key):
        return key == "f_0b0" if v0 else key in SELECTION_OPCODES
    return bool(re.search(r"select|selection|choice|選択", key, re.I))


def is_selection(call):
    return call.selection or _selection_function(call.function, call.v0)


def is_standard(call):
    if is_selection(call):
        return True
    if call.v0:
        return call.function in {"f_010", "f_014", "f_PrintMessage", "f_SetName"}
    return canonical_function(call.function) == "f_140"


def _short_name(text):
    return bool(text.strip()) and len(text) <= 24 and not re.search(r"[\s。！？!?、，,「」『』〈〉<>＆&./\\:]", text)


def infer_roles(call, known_names=()):
    roles = {arg.position: "message" for arg in call.arguments}
    if call.selection:
        return {arg.position: "message" if arg.position == call.outline_position else "select" for arg in call.arguments}
    if is_selection(call):
        return {arg.position: "select" for arg in call.arguments}
    if call.v0:
        if call.function in {"f_014", "f_SetName"}:
            return {arg.position: "name" for arg in call.arguments}
    if not call.v0 and canonical_function(call.function) == "f_140":
        # Engines use both (flags, name, text) and (text, name, flags).
        args = call.arguments
        if len(args) >= 2:
            if args[-1].position == call.arg_count:
                roles[args[-2].position] = "name"
            else:
                roles[args[1].position] = "name"
        return roles
    args = call.arguments
    for arg in args:
        if arg.text in known_names and _short_name(arg.text):
            roles[arg.position] = "name"
    for index, arg in enumerate(args[:-1]):
        following = args[index + 1].text
        if arg.text in known_names or (_short_name(arg.text) and
                (re.search(r"[。！？!?「」『』<>]", following) or len(following) >= max(18, len(arg.text) * 3))):
            roles[arg.position] = "name"
    return roles


def contains_non_ascii(text):
    """Keep decoded text with any character outside U+0000..U+007F."""
    return isinstance(text, str) and not text.isascii()


def collect_known_names(calls):
    names = set()
    for call in calls:
        if is_standard(call):
            roles = infer_roles(call)
            names.update(arg.text for arg in call.arguments if roles.get(arg.position) == "name")
    return names


def describe_calls(calls, rules=None, *, include_all=False):
    rules = parse_rules(rules, v0=bool(calls and calls[0].v0))
    known_names = collect_known_names(calls)
    groups = {}
    for call in calls:
        key = call.function if call.v0 else canonical_function(call.function)
        standard = is_standard(call)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"function": key, "calls": 0,
                "enabled": standard or canonical_function(key) in rules,
                "standard": standard, "versions": [], "arguments": {}}
        group["standard"] = group["standard"] or standard
        group["enabled"] = group["standard"] or canonical_function(key) in rules
        version = "v0" if call.v0 else "v1"
        if version not in group["versions"]:
            group["versions"].append(version)
        group["calls"] += 1
        auto = infer_roles(call, known_names)
        for arg in call.arguments:
            if not contains_non_ascii(arg.text):
                continue
            row = group["arguments"].setdefault(arg.position, {"position": arg.position,
                "samples": [], "roles": set()})
            if arg.text not in row["samples"] and len(row["samples"]) < 3:
                row["samples"].append(arg.text)
            if arg.label:
                row["label"] = arg.label
            row["roles"].add(auto.get(arg.position, "message"))
    result = []
    for key, group in groups.items():
        for pos, row in sorted(group["arguments"].items()):
            inferred = next(iter(row["roles"])) if len(row["roles"]) == 1 else "message"
            row["auto_role"] = inferred
            row["uncertain"] = len(row.pop("roles")) > 1 or not group["enabled"]
            row["role"] = rules.get(canonical_function(key), {}).get(pos, "auto")
        group["arguments"] = list(group["arguments"].values())
        if include_all or group["arguments"]:
            result.append(group)
    return result
