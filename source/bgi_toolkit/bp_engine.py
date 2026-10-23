"""BP engine profiles and evidence-based selection.

The historical generation order and product strings are format facts documented
by OpenBGI; the packaged survey records its source hash and generation order.
The 1.687 profile comes from the supplied x64 BGI handlers, not that older VM.
"""
from __future__ import annotations
from .i18n import tr

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import struct


GENERATIONS = (
    '1.58', '1.64', '1.66', '1.69/444', '1.69/451', '1.69/472',
    '1.494', '1.529', '1.535', '1.547', '1.553', '1.573', '1.588',
    '1.599', '1.616', '1.636', '1.640', '1.653', '1.654', '1.659', '1.662',
    '1.667', '1.669', '1.687',
)


@dataclass(frozen=True, slots=True)
class EngineProfile:
    name: str
    generation: str
    tag_shift: int = 28
    tag_code: int = 1
    tag_data: int = 2
    tag_heap: int = 3

    @property
    def rank(self) -> int:
        return GENERATIONS.index(self.generation)

    @property
    def compact(self) -> bool:
        return self.rank >= GENERATIONS.index('1.667')

    @property
    def historical(self) -> bool:
        return self.rank < GENERATIONS.index('1.687')

    @property
    def store_keeps_value(self) -> bool:
        return self.historical

    def before(self, generation: str) -> bool:
        return self.rank < GENERATIONS.index(generation)


def _make_profile(name: str) -> EngineProfile:
    rank = GENERATIONS.index(name)
    if rank < 3:
        return EngineProfile(name, name, 24, 0x11, 0x10, 0x12)
    if rank < 5:
        return EngineProfile(name, name, 25, 8, 9, 10)
    return EngineProfile(name, name, 28 if name == '1.687' else 26)


PROFILES = {name: _make_profile(name) for name in GENERATIONS}
# "current" preserves the existing, verified dialect without claiming that an
# unrecognized program proves the exact 1.687 engine build.
PROFILES['current'] = replace(PROFILES['1.687'], name='current')
CURRENT_ENGINE: ContextVar[EngineProfile] = ContextVar('bgi_bp_engine', default=PROFILES['current'])
ENGINE_DIRECTIVE = re.compile(r'^\s*#bp-engine\s+([^\s/]+(?:/\d+)?)\s*(?://.*)?$', re.MULTILINE)

# Full NUL-terminated string matches, in the same priority order as OpenBGI.
BUILTIN_GAMES = (
    ('NurseryRhymeTrial', '1.58'), ('NurseryRhyme', '1.64'),
    ('ItsukaTodokuAnoSorani', '1.66'), ('タユタマ【体験版】', '1.69/444'),
    ('Tayutama', '1.69/444'), ('タユタマ', '1.69/451'), ('TayutamaHD', '1.69/472'),
    ('PrismRhythm', '1.494'), ('DiamicDays', '1.529'), ('Gackoh_HD', '1.547'),
    ('Gackoh', '1.535'), ('HanairoHeptagram', '1.553'), ('MagicalCharming!', '1.573'),
    ('SekaiToSekaiNoMannakaDe', '1.588'), ('UnmeisenjouNoPhi', '1.599'),
    ('KodomoNoAsobi', '1.616'), ('SenNoHatouTsukisomeNoKouki', '1.636'),
    ('Tayutama2AS', '1.640'), ('WakabaironoQuartet', '1.653'),
    ('NekoTsukuSakura', '1.654'), ('MadoiShirokinoKamikakushi', '1.659'),
    ('Yumahorome', '1.662'), ('ArcanaAlchemia', '1.667'), ('HarukaAonoHanayomeni', '1.669'),
)


def add_engine_arguments(parser):
    parser.add_argument('--bp-engine', default='auto', help=tr('BP 引擎配置：auto、current、1.687 或历史版本／自定义配置名'))
    parser.add_argument('--bp-games', help=tr('OpenBGI 格式的 games.json / bp_engines.json'))
    parser.add_argument('--bp-exe', help=tr('用于识别引擎版本的 BGI EXE（只读取版本资源）'))


def get_profile(value: str | EngineProfile | None = None) -> EngineProfile:
    if isinstance(value, EngineProfile):
        return value
    if value is None:
        return CURRENT_ENGINE.get()
    try:
        return PROFILES[value]
    except KeyError:
        raise ValueError(tr('未知 BP 引擎配置：{0}', value)) from None


@lru_cache(maxsize=1)
def _opcode_survey():
    path = Path(__file__).with_name('asdis') / 'resources/bp_engine_opsets.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _opcode_sets():
    return _opcode_survey()['families']


def opcode_available(opcode: int, subopcode: int | None = None) -> bool:
    profile = get_profile()
    if not profile.historical:
        return True
    family = 'main' if subopcode is None else f'{opcode:02X}'
    table = _opcode_sets().get(family)
    if table is None:
        return True
    index = opcode if subopcode is None else subopcode
    # Masks belong to their survey's generation order, not the enum's position.
    rank = _opcode_survey()['generations'].index(profile.generation)
    return bool(int(table[index], 16) & (1 << rank))


def historical_names(profile: EngineProfile | None = None) -> dict[tuple[int, int], str]:
    profile = profile or get_profile()
    if not profile.historical:
        return {}
    result = {}
    if profile.before('1.636'):
        result.update({
            (0x80, 0xEC): 'sys1_check_registration', (0x80, 0xED): 'sys1_read_comap',
            (0x80, 0xEE): 'sys1_machine_key', (0x80, 0xEF): 'sys1_machine_key_xor',
        })
    if not profile.before('1.69/472') and profile.before('1.653'):
        result.update({
            (0x91, 0xF4): 'grp2_video_texture_control4',
            (0x91, 0xF5): 'grp2_video_texture_advance',
            (0x91, 0xF6): 'grp2_video_texture_release',
        })
    if profile.generation == '1.69/472':
        result[(0x92, 0x37)] = 'grp3_object_set_fixed_offset_at_37'
    if profile.before('1.653'):
        result.update({
            (0x80, 0xB0): 'sys1_named_lock_create', (0x80, 0xB1): 'sys1_named_lock_delete',
            (0x80, 0xB4): 'sys1_named_lock_wait', (0x80, 0xB5): 'sys1_named_lock_release',
            (0x80, 0xB6): 'sys1_named_lock_query',
        })
    if profile.before('1.66'):
        result.update({
            (0x90, 0x8A): 'grp1_window_set_spacing_at_8a',
            (0x90, 0x8B): 'grp1_window_set_punch_at_8b',
            (0x90, 0x8C): 'grp1_window_show_frame_at_8c',
            (0x90, 0x8D): 'grp1_window_draw_to_frame_at_8d',
            (0x91, 0x8F): 'grp2_window_get_cursor_at_8f',
            (0x90, 0x59): 'grp1_sprite_ripple_at_59',
            (0x90, 0x87): 'grp1_window_set_font_at_87',
            (0x91, 0x8C): 'grp2_window_show_text_at_8c',
            (0x91, 0x8D): 'grp2_window_draw_to_text_at_8d',
            (0x91, 0x8E): 'grp2_window_clear_at_8e',
        })
        if profile.generation == '1.58':
            result.update({
                (0x80, 0x63): 'sys1_minimize_main_window_at_63',
                (0x80, 0x64): 'sys1_set_main_window_title_at_64',
                (0x80, 0x66): 'sys1_set_cursor_shape_at_66',
                (0x90, 0x5A): 'grp1_sprite_scale_at_5a',
                (0x90, 0x5B): 'grp1_sprite_rescale_at_5b',
            })
        else:
            result.update({
                (0x90, 0x5A): 'grp1_sprite_transform_at_5a',
                (0x90, 0x5B): 'grp1_sprite_transform_at_5b',
            })
    result[(0x91, 0x98)] = 'grp2_set_rich_text_parameters'
    return result


@contextmanager
def engine_scope(profile: str | EngineProfile):
    token = CURRENT_ENGINE.set(get_profile(profile))
    try:
        yield CURRENT_ENGINE.get()
    finally:
        CURRENT_ENGINE.reset(token)


@dataclass(frozen=True, slots=True)
class EngineDetection:
    profile: EngineProfile
    confidence: str
    evidence: tuple[str, ...]
    detected_version: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        qualifier = tr('（具体构建未确定）') if self.profile.name == 'current' else ''
        return tr('BP 引擎：{0}{1}；依据：', self.profile.name, qualifier) + '；'.join(self.evidence)


def declared_engine(text: str) -> str | None:
    names = ENGINE_DIRECTIVE.findall(text)
    # Reject malformed/duplicate metadata instead of letting the assembler and
    # decompiler silently disagree about a directive.
    lines = [line for line in text.splitlines() if re.match(r'^\s*#bp-engine(?:\s|$)', line)]
    if len(lines) != len(names) or len(names) > 1:
        raise ValueError(tr('无效或重复的 #bp-engine 指令'))
    return names[0] if names else None


def declared_profile(text: str, profiles=None) -> EngineProfile | None:
    name = declared_engine(text)
    metadata = [line.strip() for line in text.splitlines() if re.match(r'^\s*#bp-profile(?:\s|$)', line)]
    lines = [line.split(None, 1)[1] if len(line.split(None, 1)) == 2 else '' for line in metadata]
    if len(lines) > 1:
        raise ValueError(tr('重复的 #bp-profile 指令'))
    if not lines:
        if name is None:
            return None
        registry = profiles or PROFILES
        if name not in registry:
            raise ValueError(tr('BPD 声明了未知 BP 引擎配置：{0}', name))
        return registry[name]
    try:
        profile = EngineProfile(**json.loads(lines[0]))
        if profile.generation not in GENERATIONS:
            raise ValueError('unknown profile generation')
        if profile.name != name or not 20 <= profile.tag_shift <= 30:
            raise ValueError('profile does not match engine directive')
        tags = (profile.tag_code, profile.tag_data, profile.tag_heap)
        if len(set(tags)) != 3 or any(not isinstance(t, int) or not 0 < t < (1 << (32 - profile.tag_shift)) for t in tags):
            raise ValueError('invalid pointer tags')
        return profile
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError(tr('无效的 #bp-profile 指令：{0}', error)) from error


def _without_json_comments(text: str) -> str:
    # OpenBGI writes // comments in games.json. Preserve // inside strings.
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\r\n]*',
                  lambda m: m.group() if m.group().startswith('"') else '', text)


def load_games(path: Path | None):
    profiles = dict(PROFILES)
    games = []
    if path is not None:
        document = json.loads(_without_json_comments(path.read_text(encoding='utf-8-sig')))
        if not isinstance(document, dict):
            raise ValueError(tr('BP 配置必须是含 profiles / games 的对象：{0}', path))
        for item in document.get('profiles', []):
            name = item['name']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/\d+)?', name):
                raise ValueError(tr('无效的 BP 配置名称：{0}', name))
            base = get_profile(item.get('generation', name))
            shift = item.get('tagShift', base.tag_shift)
            tags = [item.get(key, default) for key, default in (
                ('tagCode', base.tag_code), ('tagData', base.tag_data), ('tagHeap', base.tag_heap))]
            if not isinstance(shift, int) or not 20 <= shift <= 30 or any(
                not isinstance(tag, int) or not 0 < tag < (1 << (32 - shift)) for tag in tags
            ) or len(set(tags)) != 3:
                raise ValueError(tr('无效的 BP 指针布局：{0}', name))
            profiles[name] = replace(base, name=name, tag_shift=shift,
                                     tag_code=tags[0], tag_data=tags[1], tag_heap=tags[2])
        for item in document.get('games', []):
            if not item.get('match'):
                continue  # Launcher directory-only records do not identify BP.
            if item.get('profile') not in profiles:
                raise ValueError(tr('games.json 引用了未知 BP 配置：{0}', item.get('profile')))
            games.append((item['match'], item['profile']))
    known = {match for match, _ in games}
    games.extend((match, profile) for match, profile in BUILTIN_GAMES if match not in known)
    return profiles, games


def _related_directories(path: Path) -> list[Path]:
    directory = path if path.is_dir() else path.parent
    directories = [directory]
    if directory.name.endswith('_out'):
        original = directory.with_name(directory.name.removesuffix('_out'))
        if original.is_dir():
            directories.append(original)
    # One game-directory parent is sufficient; do not search unrelated games
    # recursively or walk arbitrarily far up the filesystem.
    directories.append(directory.parent)
    return list(dict.fromkeys(directories))


def _find_games(directories: list[Path], explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(tr('BP 游戏配置不存在：{0}', path))
        return path
    for directory in directories:
        for name in ('bp_engines.json', 'games.json'):
            path = directory / name
            if path.is_file():
                return path
    return None


def _contains_several_games(directory: Path) -> bool:
    """Do not borrow an EXE from a shared parent of several extracted games."""
    projects = set()
    for child in directory.iterdir():
        if not child.is_dir() or not any((child / name).is_file() for name in ('ipl._bp', 'ipl.bpd')):
            continue
        # bp and its generated bp_out directory belong to one game. Counting
        # them as two games suppressed the actual parent EXE for BP binaries.
        original = child.with_name(child.name.removesuffix('_out')) if child.name.endswith('_out') else child
        if original.is_dir():
            child = original
        projects.add(child.resolve())
        if len(projects) > 1:
            return True
    return False


def _literal_strings(path: Path) -> set[str]:
    if path.stat().st_size > 32 * 1024 * 1024:
        return set()
    data = path.read_bytes()
    if path.suffix.lower() == '.bpd':
        text = data.decode('utf-8-sig', errors='replace')
        result = set()
        for match in re.finditer(r'"(?:\\.|[^"\\])*"', text):
            try:
                result.add(json.loads(match.group()))
            except ValueError:
                pass
        return result
    # Skip binary headers, then scan bounded printable runs just as OpenBGI's
    # Engine_FindGame does. Instruction bytes are never substring matches.
    if len(data) >= 16:
        offset = struct.unpack_from('<I', data)[0]
        if 16 <= offset <= len(data):
            data = data[offset:]
    result = set()
    for match in re.finditer(rb'[\x20-\xff\t]{3,255}\x00', data):
        try:
            result.add(match.group()[:-1].decode('cp932'))
        except UnicodeDecodeError:
            pass
    return result


@lru_cache(maxsize=64)
def _exe_version_cached(path: str, mtime: int, size: int) -> tuple[str, str] | None:
    del mtime, size  # Included in the cache key so replaced EXEs are re-read.
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    library = ctypes.WinDLL('version', use_last_error=True)
    library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    library.GetFileVersionInfoW.restype = wintypes.BOOL
    library.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    library.VerQueryValueW.restype = wintypes.BOOL
    length = library.GetFileVersionInfoSizeW(path, None)
    if not length or length > 4 * 1024 * 1024:
        return None
    buffer = ctypes.create_string_buffer(length)
    if not library.GetFileVersionInfoW(path, 0, length, buffer):
        return None
    def query(key):
        pointer, count = ctypes.c_void_p(), wintypes.UINT()
        if library.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(count)):
            return pointer.value, count.value
        return None, 0
    pointer, count = query(r'\VarFileInfo\Translation')
    translations = struct.iter_unpack('<HH', ctypes.string_at(pointer, count)) if pointer and count % 4 == 0 else [(0x409, 0x4b0)]
    for language, codepage in translations:
        values = {}
        for key in ('FileDescription', 'InternalName', 'CompanyName', 'FileVersion'):
            pointer, count = query(f'\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}')
            values[key] = ctypes.wstring_at(pointer, max(0, count - 1)) if pointer else ''
        identity = ' '.join(values[k] for k in ('FileDescription', 'InternalName'))
        if not re.search(r'Ethornell|BURIKO General Interpreter', identity, re.I):
            continue
        text = values['FileVersion']
        match = re.search(r'(?:version\s*:|ver\s*)?\s*(1\.\d{2,3})(?:\.\d+)?', text, re.I)
        if match:
            version = match.group(1)
            build = re.search(r'build\s*:?\s*(\d+)', text, re.I)
            if version == '1.69' and build:
                version += '/' + build.group(1)
            return version, text
    return None


def executable_version(path: str | Path) -> tuple[str, str] | None:
    path = Path(path).resolve()
    stat = path.stat()
    return _exe_version_cached(str(path), stat.st_mtime_ns, stat.st_size)


def detect_engine(path: str | Path, engine: str | EngineProfile = 'auto', *,
                  games_path: str | Path | None = None, executable: str | Path | None = None) -> EngineDetection:
    path = Path(path)
    if isinstance(engine, EngineProfile):
        return EngineDetection(engine, 'explicit', (tr('调用方指定配置'),))
    directories = _related_directories(path)
    config = _find_games(directories, games_path)
    profiles, games = load_games(config)
    requested = engine if engine != 'auto' else os.environ.get('BGI_BP_ENGINE', 'auto')
    if requested != 'auto':
        if requested not in profiles:
            raise ValueError(tr('未知 BP 引擎配置：{0}', requested))
        return EngineDetection(profiles[requested], 'explicit', (tr('手动指定配置'),))
    if path.is_file() and path.suffix.lower() == '.bpd':
        profile = declared_profile(path.read_text(encoding='utf-8-sig', errors='replace'), profiles)
        if profile:
            return EngineDetection(profile, 'declared', (tr('BPD 的 #bp-engine 声明'),))
    candidates = []
    if executable is not None:
        exe = Path(executable)
        if not exe.is_file():
            raise FileNotFoundError(tr('BGI EXE 不存在：{0}', exe))
        info = executable_version(exe)
        if info is None:
            raise ValueError(tr('无法从指定文件读取 BGI 引擎版本：{0}', exe))
        candidates.append((exe, info))
    else:
        for directory in directories:
            if directory == directories[-1] and directory != directories[0] and _contains_several_games(directory):
                continue
            found = []
            for exe in sorted(directory.glob('*.exe'))[:64]:
                try:
                    info = executable_version(exe)
                except OSError:
                    continue
                if info:
                    found.append((exe, info))
            if found:
                candidates = found
                break
    versions = {version for _, (version, _) in candidates}
    if len(versions) == 1:
        version = next(iter(versions))
        if version in profiles:
            return EngineDetection(profiles[version], 'executable',
                                   (tr('BGI EXE 版本资源：{0}', candidates[0][0]),), version)
        return EngineDetection(profiles['current'], 'unknown',
                               (tr('发现未收录的 BGI 版本 {0}，使用当前兼容配置', version),), version)
    if len(versions) > 1:
        raise ValueError(tr('检测到多个不同版本的 BGI EXE，请用 --bp-exe 或 --bp-engine 指定：') + ', '.join(sorted(versions)))
    boot_files = []
    for directory in directories:
        for name in ('ipl._bp', 'ipl.bpd'):
            boot = directory / name
            if boot.is_file():
                boot_files.append(boot)
        if boot_files:
            break
    if path.is_file() and path not in boot_files:
        boot_files.append(path)
    for boot in boot_files:
        literals = _literal_strings(boot)
        for match, name in games:
            if match in literals:
                source = str(config) if config and (match, name) not in BUILTIN_GAMES else tr('内置产品表')
                return EngineDetection(profiles[name], 'product', (tr('{0} 中的完整字符串 {1!r}，{2}', boot, match, source),))
    return EngineDetection(profiles['current'], 'unknown', (tr('未匹配产品或 BGI EXE，使用当前兼容配置'),))
