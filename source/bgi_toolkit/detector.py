from __future__ import annotations
from .i18n import tr

from pathlib import Path
import re
import stat
import struct

from .models import InputKind


def detect_input(path: str | Path) -> InputKind:
    candidate = Path(path)
    try:
        mode = candidate.stat().st_mode
    except OSError:
        return InputKind.UNKNOWN
    if stat.S_ISDIR(mode):
        return InputKind.DIRECTORY
    if not stat.S_ISREG(mode):
        return InputKind.UNKNOWN

    try:
        with candidate.open('rb') as stream:
            header = stream.read(64)
    except OSError:
        return InputKind.UNKNOWN
    if header.startswith((b'BURIKO ARC20', b'PackFile    ')):
        return InputKind.ARCHIVE
    if header.startswith(b'BF_Movie'):
        return InputKind.MOVIE
    if header.startswith(b'BurikoCompiledScriptVer1.00\0'):
        return InputKind.STORY_BINARY
    if header.startswith(b'BurikoAnimationDefinition'):
        return InputKind.ANIMATION
    if (header[4:8] == b'bw  ' or header.startswith((b'OggS', b'fLaC', b'ID3', b'#!AMR'))
            or (header.startswith((b'RIFF', b'RF64')) and header[8:12] == b'WAVE')
            or (header.startswith(b'FORM') and header[8:12] in (b'AIFF', b'AIFC'))
            or (len(header) > 1 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
            or (header[4:8] == b'ftyp' and candidate.suffix.lower() in ('.m4a', '.m4b', '.aac'))):
        return InputKind.AUDIO
    if (header.startswith((b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'BM'))
            or (header.startswith(b'RIFF') and header[8:12] == b'WEBP')):
        return InputKind.IMAGE
    if header.startswith((b'CompressedBG___', b'\x89PNG\r\n\x1a\n')):
        return InputKind.IMAGE
    if header.startswith(b'SDC FORMAT 1.00'):
        return InputKind.GDB if candidate.suffix.lower() == '.gdb' else InputKind.PACKED_RESOURCE
    if header.startswith((b'DSC FORMAT 1.00', b'BSE 1.')):
        return InputKind.PACKED_RESOURCE
    if len(header) >= 16:
        width,height,bpp,flag = struct.unpack_from('<4H',header)
        if (0 < width <= 32767 and 0 < height <= 32767 and bpp in (8,24,32) and flag in (0,1)
            and candidate.stat().st_size >= 16 + width*height*(bpp//8)):
            return InputKind.IMAGE

    name = candidate.name.lower()
    if candidate.suffix.lower() in ('.wav', '.ogg', '.oga', '.opus', '.mp3', '.flac', '.m4a', '.aac', '.aif', '.aiff', '.bw', '._bw'):
        return InputKind.AUDIO
    if candidate.suffix.lower() in ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tif', '.tiff', '.cbg'):
        return InputKind.IMAGE
    if not header.startswith(b"BurikoCompiledScriptVer1.00\0"):
        db_name = re.sub(r"__\d+$", "", name.split(".", 1)[0])
        if db_name == "scenariodb" and candidate.suffix.lower() in ("", ".bin", ".dat", ".rebuilt"):
            return InputKind.SCENARIO_DB
        if db_name == "stringsdb" and candidate.suffix.lower() in ("", ".bin", ".dat", ".rebuilt"):
            return InputKind.STRINGS_DB
    if name.endswith(".bsd") or name.endswith(".bsd.txt"):
        return InputKind.BSD
    if name.endswith(".bpd"):
        return InputKind.BPD
    if name.endswith(".gdb"):
        return InputKind.GDB
    if name.endswith("._bp"):
        return InputKind.BP_BINARY
    if name.endswith("._bs") or candidate.suffix == "":
        return InputKind.STORY_BINARY

    if candidate.suffix.lower() not in {".txt", ".asm", ".src"}:
        return InputKind.UNKNOWN

    try:
        sample = candidate.read_bytes()[:4096]
        text = sample.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return InputKind.UNKNOWN
    if "#runtime-relative-branches" in text or "#label-addend" in text:
        return InputKind.BPD
    if "#v0" in text or "#header" in text or "#export" in text:
        return InputKind.BSD
    return InputKind.UNKNOWN


NON_STORY_RESOURCES = {
    InputKind.IMAGE: ('图片', '图片工具'),
    InputKind.AUDIO: ('音频', '音频工具'),
    InputKind.ANIMATION: ('动画定义', '动画定义工具'),
    InputKind.SCENARIO_DB: ('ScenarioDB 数据库', '数据库工具'),
    InputKind.STRINGS_DB: ('StringsDB 数据库', '数据库工具'),
    InputKind.GDB: ('GDB 存档数据库', 'GDB 工具'),
    InputKind.ARCHIVE: ('ARC 封包', '封包工具'),
    InputKind.PACKED_RESOURCE: ('DSC / BSE / SDC 封装资源', '封包工具先解包，再按资源类型处理'),
    InputKind.MOVIE: ('BM 动画', 'BM 工具'),
    InputKind.BP_BINARY: ('BP 系统脚本', 'BP 系统脚本工具'),
    InputKind.BPD: ('BP 反汇编文本', 'BP 系统脚本工具或 BPD 反编译工具'),
}


def non_story_notice(path: str | Path, kind: InputKind | None = None) -> str | None:
    kind = kind or detect_input(path)
    resource = NON_STORY_RESOURCES.get(kind)
    if resource is None:
        return None
    label, tool = (tr(value) for value in resource)
    return tr('检测到{0}：{1}。这不是 BCS 剧本，已跳过；请使用{2}。', label, Path(path), tool)


def iter_inputs(
    target: str | Path,
    accepted: set[InputKind],
    recursive: bool = True,
    *,
    on_skip=None,
) -> list[Path]:
    target_path = Path(target)
    if target_path.is_file():
        kind = detect_input(target_path)
        if kind in accepted:
            return [target_path]
        if on_skip is not None and kind in NON_STORY_RESOURCES:
            on_skip(target_path, kind)
        return []
    if not target_path.is_dir():
        return []
    iterator = target_path.rglob("*") if recursive else target_path.glob("*")
    files = []
    for path in sorted(iterator):
        if not path.is_file() or any(part.startswith('.bgi-') for part in path.relative_to(target_path).parts):
            continue
        kind = detect_input(path)
        if kind in accepted:
            files.append(path)
        elif on_skip is not None and kind in NON_STORY_RESOURCES:
            on_skip(path, kind)
    return files
