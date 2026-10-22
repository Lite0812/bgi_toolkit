"""BGI BW stream export and FFmpeg audio conversion.

BW payload-offset handling follows GARbro's MIT-licensed Ethornell/AudioBGI.cs.
Named container header fields can be preserved in a UTF-8 JSON sidecar.
"""
from __future__ import annotations
from .i18n import tr

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import wave

from .detector import detect_input
from .models import InputKind
from .resource_codec import ResourceError
from .resource_metadata import bw_header_metadata, build_bw_header


FORMATS = ('original', 'wav', 'flac', 'ogg', 'mp3', 'aac', 'm4a', 'aiff', 'bw', 'bw_pcm')


class AudioCancelled(Exception):
    pass


@dataclass(frozen=True)
class AudioOptions:
    format: str = 'original'
    ffmpeg: str = 'ffmpeg'
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int = 192
    wav_bits: int = 16
    recursive: bool = True
    force: bool = False
    metadata: bool = True
    metadata_path: Path | None = None


@dataclass
class AudioResult:
    outputs: list[Path] = field(default_factory=list)
    succeeded: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def _stream_format(header, suffix=''):
    if header.startswith(b'OggS'):
        return 'ogg'
    if header.startswith((b'RIFF', b'RF64')) and header[8:12] == b'WAVE':
        return 'wav'
    if header.startswith(b'fLaC'):
        return 'flac'
    if header.startswith(b'ID3'):
        return 'mp3'
    if len(header) > 1 and header[0] == 0xff and header[1] & 0xe0 == 0xe0:
        return 'aac' if header[1] & 6 == 0 else 'mp3'
    if header[4:8] == b'ftyp' and suffix in ('.m4a', '.m4b'):
        return 'm4a'
    if header.startswith(b'FORM') and header[8:12] in (b'AIFF', b'AIFC'):
        return 'aiff'
    raise ResourceError(tr('无法识别音频头；支持 BGI BW、WAV、OGG、MP3、FLAC、AAC、M4A 和 AIFF。'))


def audio_info(source: Path) -> dict:
    source = Path(source)
    size = source.stat().st_size
    with source.open('rb') as stream:
        header = stream.read(128)
        offset, wrapper, original_header = 0, None, b''
        if header[4:8] == b'bw  ':
            offset = struct.unpack_from('<I', header)[0]
            if offset < 8 or offset >= size or offset > 1024 * 1024:
                raise ResourceError(tr('BW 音频数据偏移无效'))
            stream.seek(0)
            original_header = stream.read(offset)
            stream.seek(offset)
            header = stream.read(128)
            codec = struct.unpack_from('<I', original_header, 48)[0] if offset >= 64 else None
            if not header.startswith(b'OggS') and codec != 1:
                raise ResourceError(tr('暂不支持 BW 编码 {0}；当前支持 Ogg Vorbis 和 16 位 PCM', codec))
            wrapper = 'bw'
        pcm = bool(wrapper and not header.startswith(b'OggS') and codec == 1)
        kind = 'wav' if pcm else _stream_format(header, source.suffix.lower())
    result = {'schema':'bgi-audio-metadata', 'version':2, 'source_name':source.name,
        'wrapper':wrapper, 'audio_format':kind, 'source_size':size, 'payload_offset':offset,
        'payload_size':size-offset}
    if original_header:
        result['bw_header'] = bw_header_metadata(original_header) if len(original_header) >= 64 else {}
        if len(original_header) >= 64:
            frames, rate, channels, loop_flag, loop_start = struct.unpack_from('<5I', original_header, 12)
            result.update(frames=frames, sample_rate=rate, channels=channels,
                loop={'enabled': bool(loop_flag), 'start_frame': loop_start})
        if pcm:
            if channels not in (1, 2) or not 8000 <= rate <= 192000 or frames * channels * 2 != size - offset:
                raise ResourceError(tr('BW PCM 的帧数、采样率、声道或数据长度无效'))
            result.update(payload_encoding='pcm_s16le', bits=16, duration=frames/rate)

    if kind == 'wav' and not pcm:
        try:
            with wave.open(str(source), 'rb') as wav:
                result.update(channels=wav.getnchannels(), sample_rate=wav.getframerate(),
                    bits=wav.getsampwidth()*8, frames=wav.getnframes(), duration=wav.getnframes()/wav.getframerate())
        except (wave.Error, EOFError):
            pass
    if kind == 'ogg' and len(header) >= 27:
        packet = 27 + header[26]
        if header[packet:packet+7] == b'\x01vorbis' and packet + 16 <= len(header):
            result['channels'] = header[packet+11]
            result['sample_rate'] = struct.unpack_from('<I', header, packet+12)[0]
        details = _vorbis_details(source, offset)
        if details:
            result.update(details)
    return result


def _vorbis_details(source, offset=0):
    """Read page headers to obtain Vorbis parameters and exact sample count."""
    file_size = Path(source).stat().st_size
    with Path(source).open('rb') as stream:
        stream.seek(offset)
        first = stream.read(128)
        if len(first) < 28 or first[:4] != b'OggS':
            return None
        packet = 27 + first[26]
        if first[packet:packet+7] != b'\x01vorbis' or packet+16 > len(first):
            return None
        channels = first[packet+11]
        rate = struct.unpack_from('<I', first, packet+12)[0]
        serial = first[14:18]
        frames = None
        stream.seek(offset)
        while True:
            page = stream.read(27)
            if not page:
                break
            if len(page) != 27 or page[:4] != b'OggS' or page[4] != 0:
                return None
            segments = stream.read(page[26])
            if len(segments) != page[26]:
                return None
            size = sum(segments)
            if stream.tell() + size > file_size:
                return None
            stream.seek(size, 1)
            if page[14:18] == serial:
                granule = struct.unpack_from('<Q', page, 6)[0]
                if granule != 0xffffffffffffffff:
                    frames = granule
        if frames is None or not rate or not channels:
            return None
        return dict(channels=channels, sample_rate=rate, frames=frames, duration=frames/rate)


def _export_stream(source, output, info, cancelled):
    if info.get('payload_encoding') == 'pcm_s16le':
        with wave.open(str(output), 'wb') as target, source.open('rb') as stream:
            target.setnchannels(info['channels'])
            target.setsampwidth(2)
            target.setframerate(info['sample_rate'])
            stream.seek(info['payload_offset'])
            while data := stream.read(1024 * 1024):
                if cancelled():
                    raise AudioCancelled()
                target.writeframesraw(data)
    else:
        _copy_payload(source, output, info['payload_offset'], cancelled)


def _load_bw_metadata(source, options, info, log):
    path = options.metadata_path or Path(str(source) + '.json')

    if Path(path).is_file():
        document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if (document.get('schema') != 'bgi-audio-metadata' or document.get('wrapper') != 'bw'
                or document.get('version') not in (1,2)):
            raise ResourceError(tr('JSON 不是支持的 BW 音频元信息'))
        if 'bw_header' not in document and document.get('header_hex'):
            document['bw_header'] = bw_header_metadata(bytes.fromhex(document['header_hex']))
        return document
    if info.get('wrapper') == 'bw':
        return info
    log(tr('提示：未找到 BW 元信息 JSON：{0}；新建标准 64 字节头，默认关闭循环，其他保留字段置零。', path))
    return None


def _build_bw(source, output, options, info, folder, cancelled, executable, log):
    template = _load_bw_metadata(source, options, info, log)
    codec = 1 if options.format == 'bw_pcm' else 3
    input_file = source
    if info.get('wrapper'):
        input_file = folder / ('input.' + info['audio_format'])
        _export_stream(source, input_file, info, cancelled)
    need_change = bool(options.sample_rate or options.channels)
    if codec == 3:
        details = _vorbis_details(input_file) if info['audio_format'] == 'ogg' and not need_change else None
        if details is None:
            input_ogg = folder / 'encoded.ogg'
            _encode(executable or resolve_ffmpeg(options.ffmpeg), input_file, input_ogg,
                    replace(options, format='ogg'), cancelled)
            input_file = input_ogg
            details = _vorbis_details(input_file)
        if details is None:
            raise ResourceError(tr('无法读取 Ogg Vorbis 的采样率、声道或帧数'))
        payload = input_file.read_bytes()
    else:
        if (info['audio_format'] != 'wav' or info.get('bits') != 16 or need_change):
            input_wav = folder / 'encoded.wav'
            _encode(executable or resolve_ffmpeg(options.ffmpeg), input_file, input_wav,
                    replace(options, format='wav', wav_bits=16), cancelled)
            input_file = input_wav
        with wave.open(str(input_file), 'rb') as stream:
            if stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE':
                raise ResourceError(tr('BW PCM 需要 16 位无压缩 WAV'))
            details = dict(channels=stream.getnchannels(), sample_rate=stream.getframerate(), frames=stream.getnframes())
            payload = stream.readframes(stream.getnframes())
    if details['channels'] not in (1, 2) or details['frames'] > 0xffffffff:
        raise ResourceError(tr('BW 仅支持单声道或立体声，帧数不能超过 32 位'))
    header = build_bw_header(template.get('bw_header', {})) if template else build_bw_header({})
    if len(header) < 64 or len(header) > 1024 * 1024:
        raise ResourceError(tr('BW 原始头长度无效'))
    header[4:8] = b'bw  '
    struct.pack_into('<I', header, 0, len(header))
    struct.pack_into('<4I', header, 8, len(payload), details['frames'], details['sample_rate'], details['channels'])
    struct.pack_into('<I', header, 48, codec)
    # Keep unknown fields; explicit loop settings in JSON can be edited.
    loop = template.get('loop') if template else None
    if loop is not None:
        start = loop.get('start_frame', 0)
        if type(start) is not int or not 0 <= start <= 0xffffffff:
            raise ResourceError(tr('循环起始帧必须是非负的 32 位整数'))
        if loop.get('enabled') and start >= details['frames']:
            raise ResourceError(tr('循环起始帧超出新音频长度，请调整 JSON 中的 loop.start_frame'))
        struct.pack_into('<II', header, 24, int(bool(loop.get('enabled'))), start)
    with output.open('wb') as target:
        target.write(header)
        target.write(payload)


def resolve_ffmpeg(value='ffmpeg') -> str:
    value = str(value or 'ffmpeg').strip().strip('"')
    path = Path(value)
    resolved = str(path.resolve()) if path.is_file() else shutil.which(value)
    if not resolved:
        raise ResourceError(tr('找不到 FFmpeg，请点击“选择 FFmpeg”指定 ffmpeg.exe，或使用 --ffmpeg 指定路径。'))
    return resolved


def ffmpeg_version(value='ffmpeg') -> str:
    executable = resolve_ffmpeg(value)
    result = subprocess.run([executable, '-version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding='utf-8', errors='replace', timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ResourceError(result.stdout.strip() or tr('FFmpeg 检测失败'))
    return result.stdout.splitlines()[0]


def output_name(source: Path, kind: str) -> str:
    return source.name if source.suffix.lower() == '.' + kind else source.name + '.' + kind


def default_audio_output(source: Path, kind='original') -> Path:
    source = Path(source)
    if source.is_dir():
        return source.with_name(source.name + '_audio')
    actual = 'bw' if kind == 'bw_pcm' else kind
    if kind == 'original':
        try:
            actual = audio_info(source)['audio_format']
        except (OSError, ValueError):
            actual = 'ogg'
    target = source.with_name(output_name(source, actual))
    if target == source:
        target = source.with_name(source.stem + '.converted.' + actual)
    return target


def _copy_payload(source, target, offset, cancelled):
    with source.open('rb') as stream, target.open('wb') as output:
        stream.seek(offset)
        while True:
            if cancelled():
                raise AudioCancelled()
            data = stream.read(1024 * 1024)
            if not data:
                break
            output.write(data)


def _encode(executable, source, output, options, cancelled):
    codecs = {'wav':f'pcm_s{options.wav_bits}le', 'flac':'flac', 'ogg':'libvorbis', 'mp3':'libmp3lame',
        'aac':'aac', 'm4a':'aac', 'aiff':'pcm_s16be'}
    command = [executable, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y', '-i', str(source),
        '-map', '0:a:0', '-vn', '-c:a', codecs[options.format]]
    if options.format in ('ogg', 'mp3', 'aac', 'm4a'):
        command += ['-b:a', f'{options.bitrate}k']
    if options.sample_rate:
        command += ['-ar', str(options.sample_rate)]
    if options.channels:
        command += ['-ac', str(options.channels)]
    command.append(str(output))
    _run_ffmpeg(command, cancelled)


def _run_ffmpeg(command, cancelled, *, capture=False):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        while True:
            if cancelled():
                raise AudioCancelled()
            try:
                captured, error = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise ResourceError(error.decode('utf-8', errors='replace').strip()[-4000:] or tr('FFmpeg 转换失败'))
        if capture:
            return captured or b'', error or b''
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def convert_audio(source: Path, destination: Path, options: AudioOptions, *, info=None, cancelled=lambda:False,
                  executable=None, log=print) -> Path:
    source, destination = Path(source), Path(destination)
    info = info or audio_info(source)
    if options.format not in FORMATS:
        raise ResourceError(tr('不支持的输出格式'))
    if options.sample_rate is not None and not 8000 <= options.sample_rate <= 192000:
        raise ResourceError(tr('采样率必须在 8000～192000 Hz 之间'))
    if options.channels not in (None, 1, 2) or options.wav_bits not in (16, 24, 32):
        raise ResourceError(tr('声道或 WAV 位深无效'))
    if not 32 <= options.bitrate <= 320:
        raise ResourceError(tr('码率必须在 32～320 kbps 之间'))
    if source.resolve() == destination.resolve():
        raise ResourceError(tr('输出不能与输入音频相同，请选择其他文件或目录'))
    sidecar = Path(str(destination) + '.json')
    metadata = options.metadata and (info['wrapper'] == 'bw' or options.format in ('bw','bw_pcm'))
    if not options.force and (destination.exists() or (metadata and sidecar.exists())):
        raise ResourceError(tr('输出已存在：{0}；勾选允许覆盖或使用 --force', destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    kind = info['audio_format'] if options.format == 'original' else 'bw' if options.format == 'bw_pcm' else options.format
    with tempfile.TemporaryDirectory(prefix='.bgi-audio-', dir=destination.parent) as folder:
        folder = Path(folder)
        temporary = folder / ('output.' + kind)
        if options.format in ('bw', 'bw_pcm'):
            _build_bw(source, temporary, options, info, folder, cancelled, executable, log)
        elif options.format == 'original':
            _export_stream(source, temporary, info, cancelled)
        else:
            input_file = source
            if info['wrapper']:
                input_file = folder / ('input.' + info['audio_format'])
                _export_stream(source, input_file, info, cancelled)
            _encode(executable or resolve_ffmpeg(options.ffmpeg), input_file, temporary, options, cancelled)
        if cancelled():
            raise AudioCancelled()
        if options.force:
            os.replace(temporary, destination)
        else:
            with temporary.open('rb') as original, destination.open('xb') as target:
                shutil.copyfileobj(original, target)
        if metadata:
            document = dict(audio_info(destination) if kind == 'bw' else info, output_name=destination.name, output_format=kind)
            from .db_text_tool import output_file
            output_file(sidecar, (json.dumps(document, ensure_ascii=False, indent=2)+'\n').encode('utf-8'), force=options.force)
    return destination


def run_audio_task(source: Path, output: Path | None = None, *, options=None, inspect=False,
                   log=print, cancelled=lambda:False) -> AudioResult:
    options = options or AudioOptions()
    source = Path(source)
    if not source.exists():
        raise ResourceError(tr('输入不存在：{0}', source))
    batch = source.is_dir()
    files = [source]
    if batch:
        files = sorted(path for path in (source.rglob('*') if options.recursive else source.glob('*'))
            if path.is_file() and not any(part.startswith('.bgi-') for part in path.relative_to(source).parts)
            and detect_input(path) == InputKind.AUDIO
            and not (output and Path(output).resolve() != source.resolve() and path.resolve().is_relative_to(Path(output).resolve())))
    if not files:
        raise ResourceError(tr('没有找到可处理的音频文件'))
    inputs = {path.resolve() for path in files}
    result = AudioResult()
    if batch and options.metadata_path:
        raise ResourceError(tr('指定 BW 元信息 JSON 时请选择单个音频；目录转换会自动读取每个音频的同名 JSON'))
    executable = resolve_ffmpeg(options.ffmpeg) if not inspect and options.format not in ('original','bw','bw_pcm') else None
    target = Path(output) if output else default_audio_output(source, options.format)
    used = set()
    for index, path in enumerate(files, 1):
        if cancelled():
            result.cancelled = True
            break
        try:
            info = audio_info(path)
            log(f'[{index}/{len(files)}] {path}')
            if inspect:
                log(json.dumps(info, ensure_ascii=False, indent=2))
            else:
                if batch:
                    kind = info['audio_format'] if options.format == 'original' else 'bw' if options.format == 'bw_pcm' else options.format
                    relative = path.relative_to(source)
                    destination = target / relative.parent / output_name(path, kind)
                    number, original = 2, destination
                    while str(destination).casefold() in used:
                        destination = original.with_name(f'{original.stem}__{number}{original.suffix}')
                        number += 1
                else:
                    destination = target / output_name(path, info['audio_format'] if options.format == 'original' else 'bw' if options.format == 'bw_pcm' else options.format) if target.is_dir() else target
                used.add(str(destination).casefold())
                if destination.resolve() in inputs:
                    raise ResourceError(tr('输出会覆盖输入音频，请选择独立输出目录'))
                converted = convert_audio(path, destination, options, info=info, cancelled=cancelled, executable=executable, log=log)
                result.outputs.append(converted)
                log(tr('完成：{0}', converted) + (tr('（原音频流，未重新编码）') if options.format == 'original' else ''))
            result.succeeded += 1
        except AudioCancelled:
            result.cancelled = True
            break
        except (OSError, ValueError, wave.Error, struct.error) as error:
            result.errors.append(f'{path}：{error}')
            log(tr('失败 · {0}：{1}', path, error))
    log(tr('完成 {0} 个 · 失败 {1} 个', result.succeeded, len(result.errors)) + (tr(' · 已取消') if result.cancelled else ''))
    return result
