from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Literal

from .v0 import bgias_v0, bgidis_v0, bgiop_v0
from .v1 import bgias, bgidis, bgiop


ScriptVersion = Literal["auto", "v0", "v1"]
F07eMode = Literal["auto", "offset", "string"]


@dataclass(frozen=True, slots=True)
class AsdisOptions:
    encoding: str = "cp932"
    fallback_encoding: str = "gbk"
    source_encoding: str | None = None
    source_fallback_encoding: str | None = None
    script_version: ScriptVersion = "auto"
    exact: bool = False
    f07e_mode: F07eMode = "auto"
    bp_engine: str = "auto"
    bp_games: str | None = None
    bp_executable: str | None = None

    @property
    def effective_source_encoding(self) -> str:
        return self.source_encoding or self.encoding

    @property
    def effective_source_fallback_encoding(self) -> str:
        return self.source_fallback_encoding or self.fallback_encoding


def v0_mapping_path() -> Path:
    return Path(__file__).with_name("resources") / "bss_mapping_v0.json"


def detect_bsd_version(path: str | Path) -> Literal["v0", "v1"]:
    with Path(path).open("r", encoding="utf-8", errors="replace") as source:
        for raw_line in source:
            line = raw_line.strip()
            if not line or line.startswith("//") or line.startswith(";"):
                continue
            if line.startswith("#v0"):
                return "v0"
            if line.startswith(("#header", "#import", "#extra_import", "#define")):
                return "v1"
            if line.startswith(("load_base(", "store_base(", "push_dword(",
                                "push_string(", "push_offset(", "push_base_offset(",
                                "nargs(", "move_arg(")):
                return "v1"
            if line.startswith("f_"):
                return "v0"
    return "v1"


def _try_parse_v1(data: bytes, options: AsdisOptions) -> tuple[bool, int, float]:
    try:
        encoding = bgiop.normalize_encoding(options.effective_source_encoding)
        inst, _, _, _, _, _, _, _, _, size, _ = bgidis.parse(
            data,
            b"",
            encoding,
            options.effective_source_fallback_encoding,
            f07e_mode=options.f07e_mode,
            use_function_names=False,
        )
        if not inst or size <= 0 or size > len(data):
            return False, 0, 0.0
        coverage = float(size) / float(max(1, len(data)))
        if len(inst) < 3 and coverage < 0.05:
            return False, 0, 0.0
        return True, len(inst), coverage
    except Exception:
        return False, 0, 0.0


def _try_parse_v0(data: bytes) -> tuple[bool, int, float]:
    try:
        code_end = bgidis_v0.estimate_code_end(data)
        if code_end <= 0 or code_end > len(data):
            return False, 0, 0.0
        position = 0
        instruction_count = 0
        template_hints: dict[int, str] = {}
        while position < code_end:
            _, _, _, next_position, _, _ = bgidis_v0.parse_instruction(
                data,
                position,
                set(),
                -1,
                template_hints=template_hints,
            )
            if next_position <= position:
                break
            position = next_position
            instruction_count += 1
            if instruction_count > 1_000_000:
                break
        if instruction_count == 0:
            return False, 0, 0.0
        coverage = float(code_end) / float(max(1, len(data)))
        if instruction_count < 3 and coverage < 0.05:
            return False, 0, 0.0
        return True, instruction_count, coverage
    except Exception:
        return False, 0, 0.0


def detect_script_version(
    path: str | Path,
    options: AsdisOptions | None = None,
) -> Literal["v0", "v1"]:
    options = options or AsdisOptions()
    if options.script_version in {"v0", "v1"}:
        return options.script_version

    source_path = Path(path)
    with source_path.open("rb") as source:
        prefix = source.read(32)
        if prefix.startswith(b"BurikoCompiledScriptVer1.00\x00"):
            return "v1"
        data = prefix + source.read()

    op32 = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else None
    op16 = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else None
    v1_hint = op32 in bgiop.ops if op32 is not None else False
    v0_hint = (
        op16 in bgiop_v0.OPERAND_TEMPLATES or op16 in bgiop_v0.SPECIAL_OPS
        if op16 is not None
        else False
    )

    v1_ok, v1_count, v1_coverage = _try_parse_v1(data, options)
    v0_ok, v0_count, v0_coverage = _try_parse_v0(data)
    if v1_ok and not v0_ok:
        return "v1"
    if v0_ok and not v1_ok:
        return "v0"
    if v1_ok and v0_ok:
        v1_weak = v1_count < 8 or v1_coverage < 0.10
        v0_weak = v0_count < 8 or v0_coverage < 0.10
        if v1_weak != v0_weak:
            return "v0" if v1_weak else "v1"
        if abs(v0_coverage - v1_coverage) > 0.08:
            return "v0" if v0_coverage > v1_coverage else "v1"
        return "v0" if v0_count >= v1_count else "v1"
    if v0_hint != v1_hint:
        return "v0" if v0_hint else "v1"
    return "v1"


def disassemble_story(
    source: str | Path,
    destination: str | Path,
    options: AsdisOptions | None = None,
    *,
    use_function_names: bool = True,
) -> tuple[Path, Literal["v0", "v1"]]:
    options = options or AsdisOptions()
    source_path = Path(source)
    from ..detector import non_story_notice
    notice = non_story_notice(source_path)
    if notice:
        raise ValueError(notice)
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    version = detect_script_version(source_path, options)
    if version == "v0":
        bgidis_v0.disassemble_file(
            source_path,
            output_path=output_path,
            encoding=options.effective_source_encoding,
            mapping_path=v0_mapping_path(),
            exact_mode=options.exact,
            use_function_names=use_function_names,
        )
    else:
        bgidis.dis(
            source_path,
            exact_mode=options.exact,
            encoding=options.effective_source_encoding,
            fallback_encoding=options.effective_source_fallback_encoding,
            output_path=output_path,
            f07e_mode=options.f07e_mode,
            use_function_names=use_function_names,
        )
    if not output_path.is_file():
        raise RuntimeError(f"BSD output was not created: {output_path}")
    return output_path, version


def assemble_story(
    source: str | Path,
    destination: str | Path,
    options: AsdisOptions | None = None,
) -> tuple[Path, Literal["v0", "v1"]]:
    options = options or AsdisOptions()
    source_path = Path(source)
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    version = (
        options.script_version
        if options.script_version in {"v0", "v1"}
        else detect_bsd_version(source_path)
    )
    if version == "v0":
        bgias_v0.assemble(
            source_path,
            output_path=output_path,
            mapping_path=v0_mapping_path(),
            encoding_override=options.encoding,
        )
    else:
        bgias.asm(
            source_path,
            encoding=options.encoding,
            fallback_encoding=options.fallback_encoding,
            output_path=output_path,
        )
    if not output_path.is_file():
        raise RuntimeError(f"script output was not created: {output_path}")
    return output_path, version


def disassemble_bp(
    source: str | Path,
    destination: str | Path,
    options: AsdisOptions | None = None,
) -> Path:
    from .bp import bpdis

    options = options or AsdisOptions()
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpdis.dis(
        Path(source),
        exact_mode=options.exact,
        encoding=options.effective_source_encoding,
        output_path=output_path,
        engine=options.bp_engine,
        games_path=options.bp_games,
        executable=options.bp_executable,
    )
    if not output_path.is_file():
        raise RuntimeError(f"BPD output was not created: {output_path}")
    return output_path


def assemble_bp(
    source: str | Path,
    destination: str | Path,
    options: AsdisOptions | None = None,
) -> Path:
    from .bp import bpas

    options = options or AsdisOptions()
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpas.asm(Path(source), encoding=options.encoding, output_path=output_path,
             engine=options.bp_engine, games_path=options.bp_games, executable=options.bp_executable)
    if not output_path.is_file():
        raise RuntimeError(f"BP output was not created: {output_path}")
    return output_path
