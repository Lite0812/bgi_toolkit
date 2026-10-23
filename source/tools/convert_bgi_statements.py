#!/usr/bin/env python3

import argparse
import ast
import codecs
import hashlib
import io
import json
import sys
import tokenize
from collections import Counter
from pathlib import Path


OPCODE_SCHEMAS = {
    "AchievementGrant": 1,
    "AutoSaveRange": 1,
    "ClearSubWindow": 0,
    "DrawEndroll": 7,
    "SNS": 0,
    "SelectColor": 6,
    "SelectColorEx": 6,
    "Select_07": 0,
    "SetBustshotTime": 1,
    "SetSubWindow": 3,
    "SetSubWindowSprite": 8,
    "SoundStopAll": 1,
    "SpriteMove": 4,
    "StartEndroll": 5,
    "StartShakingOfWindowSx": 4,
    "_AnimateSPR": 67,
    "_ChangeVolumeOfBGM": 3,
    "_ChangeVolumeOfStreamSE": 3,
    "_DrawBS": 66,
    "_DrawFilm": 7,
    "_DrawSPR": 64,
    "_DrawScene": 15,
    "_EnableGradation": 5,
    "_ExecuteScheduledControl": 1,
    "_FadeBGM": 3,
    "_FadeBS": 10,
    "_FadeFilm": 2,
    "_FadeSE": 2,
    "_FadeSPR": 10,
    "_FadeScene": 3,
    "_FadeStreamSE": 3,
    "_Font": 3,
    "_MoveBS": 58,
    "_MoveRepeatBS": 14,
    "_MoveSPR": 56,
    "_MoveSight": 5,
    "_PauseBGM": 4,
    "_PlayBGM": 6,
    "_PlayMovie": 3,
    "_PlaySE": 6,
    "_PlayStreamSE": 7,
    "_PlayVoice": 7,
    "_QuakeEx": 6,
    "_ScrollBG": 8,
    "_ScrollSPRContinuously": 5,
    "_ShakeBS": 5,
    "_ShakeSPR": 5,
    "_StopSE": 1,
    "_StopShakingBS": 3,
    "_StopShakingSPR": 3,
    "_StopVoice": 1,
    "_Wait": 2,
    "_WaitToFinishSE": 3,
    "_WaitToFinishVoice": 2,
    "clear_bs": 1,
    "clear_sp": 1,
    "f_ActivateScreenEffectForcedly": 1,
    "f_CallScript": 1,
    "f_CancelSkipping": 0,
    "f_ChangeWindowDesign": 3,
    "f_DisableLabelJumping": 0,
    "f_DisableWindow": 0,
    "f_DisableWindowEx": 1,
    "f_EnableLabelJumping": 2,
    "f_EnableWindowEx": 1,
    "f_EndLine": 1,
    "f_ExecuteLine": 1,
    "f_IsDefaultName": 1,
    "f_PrintMessage": 5,
    "f_RegisterTextToHistory": 2,
    "f_SelectMap": 0,
    "f_SetCaption": 1,
    "f_SetClearColor": 1,
    "f_SetEndOfDay": 0,
    "f_SetGlobalFlag": 2,
    "f_SetLocalFlag": 2,
    "f_SetTargetOfMessageWindow": 5,
    "f_SetUsageConditionOfBitmap": 2,
    "f_SetValidityOfAutomaticWindowDisabling": 1,
    "f_SetValidityOfInput": 1,
    "f_StoreValueToLocalWork": 2,
    "f_SuspendSkippingAndAutomaticMode": 2,
    "f_TerminateExecutionOfScript": 0,
}


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=["game/script"])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expect-converted", type=int)
    return parser.parse_args()


def discover_files(paths):
    files = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(path.rglob("*.rpy"))
        elif path.suffix == ".rpy":
            files.append(path)
        else:
            raise ValueError("not an .rpy file or directory: {}".format(path))
    return sorted(set(files))


def read_source(path):
    data = path.read_bytes()
    has_bom = data.startswith(codecs.BOM_UTF8)
    return data.decode("utf-8-sig"), has_bom


def write_source(path, source, has_bom):
    data = source.encode("utf-8")
    if has_bom:
        data = codecs.BOM_UTF8 + data
    path.write_bytes(data)


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def literal_is_supported(value):
    if type(value) in (int, str):
        return True
    if isinstance(value, list):
        return all(literal_is_supported(item) for item in value)
    return False


def split_line_ending(line):
    content = line.rstrip("\r\n")
    return content, line[len(content):]


def comment_tail(body):
    try:
        tokens = tokenize.generate_tokens(io.StringIO(body).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                return body[token.start[1]:]
    except tokenize.TokenError:
        pass
    return ""


def normalize_legacy(body):
    try:
        module = ast.parse(body, mode="exec")
    except SyntaxError:
        return None, "parse_error"

    if len(module.body) != 1:
        return None, "not_single_statement"
    statement = module.body[0]
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None, "not_direct_call"

    call = statement.value
    if not isinstance(call.func, ast.Name):
        return None, "not_name_call"
    opcode = call.func.id
    if opcode not in OPCODE_SCHEMAS:
        return None, "unknown_opcode"
    if call.keywords or any(isinstance(argument, ast.Starred) for argument in call.args):
        return None, "complex_arguments"

    expected_length = OPCODE_SCHEMAS[opcode]
    if not call.args:
        if expected_length != 0:
            return None, "missing_parameter_list"
        return (opcode, False, ()), None
    if len(call.args) != 1:
        return None, "multiple_arguments"

    try:
        params = ast.literal_eval(call.args[0])
    except Exception:
        return None, "dynamic_arguments"
    if not isinstance(params, list) or not literal_is_supported(params):
        return None, "unsupported_literal"
    if len(params) != expected_length:
        return None, "schema_mismatch"
    return (opcode, True, params), None


def normalize_bgi(body):
    parts = body.strip().split(None, 2)
    if len(parts) < 2 or parts[0] != "bgi":
        return None, "not_bgi"
    opcode = parts[1]
    if opcode not in OPCODE_SCHEMAS:
        return None, "unknown_opcode"

    expected_length = OPCODE_SCHEMAS[opcode]
    if len(parts) == 2:
        if expected_length != 0:
            return None, "missing_parameter_list"
        return (opcode, False, ()), None

    payload = parts[2]
    tail = comment_tail(payload)
    if tail:
        payload = payload[:-len(tail)].rstrip()
    try:
        params = ast.literal_eval(payload)
    except Exception:
        return None, "invalid_bgi_literal"
    if not isinstance(params, list) or not literal_is_supported(params):
        return None, "unsupported_literal"
    if len(params) != expected_length:
        return None, "schema_mismatch"
    return (opcode, True, params), None


def convert_line(line):
    content, ending = split_line_ending(line)
    stripped = content.lstrip(" \t")
    indent = content[:len(content) - len(stripped)]
    if not stripped.startswith("$"):
        return None, None, None

    body = stripped[1:].lstrip(" \t")
    normalized, reason = normalize_legacy(body)
    if normalized is None:
        return None, reason, None

    opcode, has_params, _params = normalized
    module = ast.parse(body, mode="exec")
    call = module.body[0].value
    tail = comment_tail(body)
    suffix = ""
    if tail:
        comment_index = body.index(tail)
        code_without_comment = body[:comment_index]
        suffix = code_without_comment[len(code_without_comment.rstrip()):] + tail

    if has_params:
        payload = ast.get_source_segment(body, call.args[0])
        converted = "{}bgi {} {}{}{}".format(indent, opcode, payload, suffix, ending)
    else:
        converted = "{}bgi {}{}{}".format(indent, opcode, suffix, ending)

    round_trip, round_trip_reason = normalize_bgi(converted[len(indent):].rstrip("\r\n"))
    if round_trip_reason is not None or round_trip != normalized:
        raise ValueError("round-trip mismatch: {!r} -> {!r}".format(content, converted))
    return converted, None, normalized


def process_file(path, write):
    source, has_bom = read_source(path)
    lines = source.splitlines(keepends=True)
    converted_lines = []
    entries = []
    skipped = Counter()
    existing_bgi = 0
    invalid_bgi = []

    for line_number, line in enumerate(lines, 1):
        content, _ending = split_line_ending(line)
        stripped = content.lstrip(" \t")
        if stripped.startswith("bgi "):
            existing_bgi += 1
            _normalized, reason = normalize_bgi(stripped)
            if reason is not None:
                invalid_bgi.append({"file": path.as_posix(), "line": line_number, "reason": reason})

        converted, reason, normalized = convert_line(line)
        if converted is None:
            converted_lines.append(line)
            if stripped.startswith("$"):
                skipped[reason] += 1
            continue

        converted_lines.append(converted)
        opcode = normalized[0]
        entries.append(
            {
                "file": path.as_posix(),
                "line": line_number,
                "opcode": opcode,
                "before_sha256": sha256_text(line),
                "after_sha256": sha256_text(converted),
            }
        )

    new_source = "".join(converted_lines)
    if write and new_source != source:
        write_source(path, new_source, has_bom)
    return entries, skipped, existing_bgi, invalid_bgi


def main():
    arguments = parse_arguments()
    try:
        files = discover_files(arguments.paths)
    except ValueError as exception:
        print(exception, file=sys.stderr)
        return 2

    entries = []
    skipped = Counter()
    existing_bgi = 0
    invalid_bgi = []
    for path in files:
        file_entries, file_skipped, file_existing, file_invalid = process_file(path, arguments.write)
        entries.extend(file_entries)
        skipped.update(file_skipped)
        existing_bgi += file_existing
        invalid_bgi.extend(file_invalid)

    summary = {
        "files": len(files),
        "convertible": len(entries),
        "existing_bgi": existing_bgi,
        "skipped": dict(sorted(skipped.items())),
        "invalid_bgi": len(invalid_bgi),
        "wrote": bool(arguments.write),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))

    if arguments.manifest:
        arguments.manifest.parent.mkdir(parents=True, exist_ok=True)
        arguments.manifest.write_text(
            json.dumps(
                {"summary": summary, "converted": entries, "invalid_bgi": invalid_bgi},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    if arguments.expect_converted is not None and len(entries) != arguments.expect_converted:
        print(
            "expected {} convertible statements, found {}".format(
                arguments.expect_converted, len(entries)
            ),
            file=sys.stderr,
        )
        return 1
    if invalid_bgi:
        for item in invalid_bgi[:20]:
            print("{file}:{line}: {reason}".format(**item), file=sys.stderr)
        return 1
    if arguments.check and entries:
        print("{} legacy BGI statements still need conversion".format(len(entries)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
