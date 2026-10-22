"""Encode frame sequences and FFmpeg-readable videos as BF_Movie keyframes."""
from __future__ import annotations
from .i18n import tr
from dataclasses import dataclass
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
from PIL import Image

from .audio_tool import AudioCancelled, _run_ffmpeg, resolve_ffmpeg
from .dct_codec import quantization_tables
from .native_codec import transform
from .resource_codec import ResourceError
from .resource_metadata import byte_values


@dataclass(frozen=True)
class MovieEncodeOptions:
    fps: int | None = None
    bpp: int | None = None
    quality: int | None = None
    ffmpeg: str = 'ffmpeg'
    metadata_path: Path | None = None
    force: bool = False
    limit: int | None = None


def _natural_key(path):
    return [(1,int(part)) if part.isdigit() else (0,part.casefold()) for part in re.split(r'(\d+)',path.name)]


def _video_frame_rate(source, executable, cancelled):
    paired = Path(executable).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    probe = str(paired) if paired.is_file() else shutil.which('ffprobe')
    if probe:
        command = [probe,'-v','error','-select_streams','v:0','-show_entries',
            'stream=avg_frame_rate,r_frame_rate','-of','json',str(source)]
        output,_ = _run_ffmpeg(command,cancelled,capture=True)
        document = json.loads(output)
        streams = document.get('streams',[])
        if not streams:
            raise ResourceError(tr('输入文件没有视频画面'))
        for name in ('avg_frame_rate','r_frame_rate'):
            try:
                rate = Fraction(streams[0].get(name,'0/1'))
                if rate > 0:
                    return rate
            except (ValueError,ZeroDivisionError):
                pass
    # FFmpeg remains sufficient when no ffprobe is installed beside it.
    _,diagnostic = _run_ffmpeg([executable,'-hide_banner','-nostdin','-i',str(source),
        '-map','0:v:0','-frames:v','0','-f','null','-'],cancelled,capture=True)
    for line in diagnostic.decode('utf-8',errors='replace').splitlines():
        if 'Video:' not in line:
            continue
        match = re.search(r'(\d+(?:\.\d+)?)\s+fps',line)
        if match:
            return Fraction(match.group(1))
    raise ResourceError(tr('无法读取视频原帧率，请检查视频或 FFmpeg'))


def encode_movie(source, output=None, *, options=None, log=print, cancelled=lambda:False):
    source=Path(source);options=options or MovieEncodeOptions()
    output=Path(output) if output else source.with_name(source.stem+'.rebuilt.bm' if source.suffix.lower()=='.bm' else source.name+'.bm')
    if not source.exists():
        raise ResourceError(tr('输入不存在：{0}', source))
    if source.resolve()==output.resolve() or (source.is_dir() and output.resolve().is_relative_to(source.resolve())):
        raise ResourceError(tr('BM 输出请选择输入文件或帧目录之外的位置'))
    if output.exists() and not options.force:
        raise ResourceError(tr('输出已存在：{0}；勾选允许覆盖或使用 --force', output))
    sidecar=Path(options.metadata_path) if options.metadata_path else source/'movie.json' if source.is_dir() else Path(str(source)+'.json')
    sequence=source.is_dir()
    metadata={}
    if sidecar.is_file():
        metadata=json.loads(sidecar.read_text(encoding='utf-8-sig'))
        if metadata.get('schema')!='bgi-movie-metadata':
            raise ResourceError(tr('不是 BM 元信息 JSON'))
    else:
        log(tr('提示：未找到 BM 元信息 JSON：{0}；使用指定参数或默认 32 位、质量 90，保留字段置零。', sidecar) + (tr('图片序列默认 30 FPS。') if sequence else tr('帧率与帧数跟随视频。')))
    fps=(options.fps if options.fps is not None else metadata.get('fps',30)) if sequence else None
    bpp=options.bpp if options.bpp is not None else metadata.get('bpp',32)
    if (sequence and (type(fps) is not int or not 1<=fps<=240)) or bpp not in (24,32):
        raise ResourceError(tr('BM 帧率需为 1～240 的整数，位深需为 24 或 32'))
    if sequence and options.limit is not None and options.limit<=0:
        raise ResourceError(tr('最大帧数必须大于 0'))
    if options.quality is None and metadata.get('quantization_table'):
        quant=byte_values(metadata['quantization_table'],tr('BM 量化表'))
        if len(quant)!=128 or 0 in quant:
            raise ResourceError(tr('BM 量化表必须包含 128 个 1～255 的整数'))
    else:
        quant=quantization_tables(quality=options.quality)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bgi-movie-',dir=output.parent) as temp:
        folder=Path(temp)
        if source.is_dir():
            frames=sorted((p for p in source.iterdir() if p.is_file() and p.suffix.lower() in ('.png','.bmp','.jpg','.jpeg','.webp','.tif','.tiff')),key=_natural_key)
        else:
            executable=resolve_ffmpeg(options.ffmpeg)
            rate=_video_frame_rate(source,executable,cancelled)
            fps=max(1,int(rate+Fraction(1,2)))
            if fps>0xffffffff:
                raise ResourceError(tr('视频帧率超过 BM 的整数范围'))
            log(tr('视频原帧率：{0:.6g} FPS；保留全部原始帧，不补帧、不抽帧。', float(rate)))
            if rate.denominator!=1:
                log(tr('提示：BM 帧率字段只能保存整数，{0} FPS 在 BM 头中记为 {1} FPS；原始帧数保留。', rate, fps))
            if options.fps is not None or options.limit is not None:
                log(tr('提示：手动帧率和最大帧数只对图片序列生效，视频输入忽略这两项。'))
            log(tr('正在用 FFmpeg 提取视频帧；BM 文件只写入画面，音轨可用音频工具单独转换。'))
            command=[executable,'-hide_banner','-nostdin','-loglevel','error','-y','-i',str(source),
                '-map','0:v:0','-vsync','0','-pix_fmt','rgba']
            command+=[str(folder/'frame_%08d.png')]
            _run_ffmpeg(command,cancelled)
            frames=sorted(folder.glob('frame_*.png'))
        if sequence and options.limit:
            frames=frames[:options.limit]
        if not frames:
            raise ResourceError(tr('没有找到可编码的图像帧'))
        temporary=folder/'output.bm'
        with Image.open(frames[0]) as image:
            width,height=image.size
        if not width or not height or width>65535 or height>65535 or width*height>134217728:
            raise ResourceError(tr('BM 图像尺寸超出支持范围'))
        header=bytearray(64)
        header[:16]=b'BF_Movie_______\0'
        struct.pack_into('<7I',header,16,0x10001,width,height,bpp,1 if bpp==24 else 2,fps,len(frames))
        for item in metadata.get('reserved_header_fields',[]):
            offset=item['offset']
            if offset not in (44,48,52,56,60):
                raise ResourceError(tr('BM 保留字段位置无效'))
            struct.pack_into('<I',header,offset,item['value'])
        offsets=[]
        with temporary.open('w+b') as stream:
            stream.write(header);stream.write(quant);stream.write(bytes(len(frames)*4))
            for index,path in enumerate(frames,1):
                if cancelled():
                    raise AudioCancelled()
                with Image.open(path) as loaded:
                    if loaded.size!=(width,height):
                        raise ResourceError(tr('帧尺寸不一致：{0} 为 {1}，应为 {2}', path.name, loaded.size, (width, height)))
                    image=loaded.convert('RGBA' if bpp==32 else 'RGB')
                    raw=image.tobytes('raw','BGRA' if bpp==32 else 'BGR')
                encoded=transform(3,raw,width=width,height=height,bpp=bpp,header=bytes(48)+quant)
                body=encoded[180:]
                offset=stream.tell()
                if offset+len(body)>0xffffffff:
                    raise ResourceError(tr('BM 文件超过 32 位偏移范围（4 GiB）'))
                offsets.append(offset);stream.write(body)
                log(f'[{index}/{len(frames)}] {path.name}')
            stream.seek(192)
            stream.write(struct.pack(f'<{len(offsets)}I',*offsets))
        if cancelled():
            raise AudioCancelled()
        if options.force:
            os.replace(temporary,output)
        else:
            with temporary.open('rb') as stream,output.open('xb') as target:
                shutil.copyfileobj(stream,target,1024*1024)
    log(tr('完成：{0} · {1} 帧 · {2} FPS · {3}×{4} · {5} 位', output, len(frames), fps, width, height, bpp))
    return output
