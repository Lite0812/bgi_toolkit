"""Shared batch operations used by the resource GUI and CLI."""
from __future__ import annotations
from .i18n import tr

import json
from pathlib import Path

from .archive_tool import PACKED_DATA, extract_archive, pack_archive, read_archive
from .resource_codec import ResourceError


def run_asset_task(operation: str, sources: list[Path], output: Path | None = None, *,
                   options: dict | None = None, log=print, cancelled=lambda: False) -> list[Path]:
    options = options or {}
    outputs = []
    for source in sources:
        if not source.exists():
            raise ResourceError(tr('输入不存在：{0}', source))
        if cancelled():
            raise ResourceError(tr('任务已取消'))
        if operation.startswith(('sdc-', 'dsc-')):
            from .sdc_tool import run_sdc_task
            result = run_sdc_task(operation[4:], source, output,
                metadata_path=options.get('metadata_path'), metadata=options.get('metadata', True),
                force=options.get('force', False), recursive=options.get('recursive', True),
                output_format=operation[:3], format_filter=operation[:3],
                log=log, cancelled=cancelled)
            outputs.extend(result.outputs)
            if result.errors or result.cancelled:
                raise ResourceError(tr('{0} 完成 {1} 个，失败 {2} 个', operation[:3].upper(), result.succeeded, len(result.errors)) + (tr('，已取消') if result.cancelled else ''))
            continue
        if operation == 'archive-list':
            version, entries = read_archive(source)
            log(f'{source.name}: ARC v{version}, {len(entries)} entries')
            for entry in entries:
                log(f'{entry.size:>10}  {entry.name}')
            continue
        if operation == 'archive-pack':
            target = output or source.with_name(source.name + '.arc')
            if len(sources) > 1 or options.get('output_directory'):
                target = target / (source.name + '.arc')
            outputs.append(pack_archive(source, target, version=options.get('version'), log=log,
                                        cancelled=cancelled, import_images=options.get('import_images', False)))
            continue
        if operation == 'archive-unpack':
            target = output or source.with_name(source.name + '.unpacked')
            if len(sources) > 1 or options.get('output_directory'):
                target = target / source.stem
            extract_archive(source, target, log=log, cancelled=cancelled)
            outputs.append(target)
            if options.get('convert_images'):
                outputs.extend(run_asset_task('image-export', [target], target / '.bgi-images',
                    options={'output_directory': True, 'allow_empty': True}, log=log, cancelled=cancelled))
            continue
        if operation == 'bm-encode':
            from .movie_encode import MovieEncodeOptions, encode_movie
            settings = MovieEncodeOptions(fps=options.get('fps'), bpp=options.get('bpp'),
                quality=options.get('quality'), ffmpeg=options.get('ffmpeg','ffmpeg'),
                metadata_path=options.get('metadata'), force=options.get('force',False), limit=options.get('limit'))
            outputs.append(encode_movie(source, output, options=settings, log=log, cancelled=cancelled))
            continue
        if operation.startswith('bm-'):
            from .movie_tool import export_movie, movie_info
            files = [source] if source.is_file() else sorted(source.rglob('*.bm'))
            for file in files:
                if operation == 'bm-info':
                    log(json.dumps(movie_info(file), ensure_ascii=False, indent=2))
                else:
                    target = output or file.with_name(file.name + '.frames')
                    if source.is_dir() or len(sources) > 1 or options.get('output_directory'):
                        target = target / (file.name + '.frames')
                    outputs.append(export_movie(file, target, backend=options.get('backend', 'auto'),
                        limit=options.get('limit'), log=log, cancelled=cancelled))
            continue
        from .image_tool import encode_image, export_image, image_kind
        root = source if source.is_dir() else source.parent
        files = [source] if source.is_file() else sorted(source.rglob('*'))
        converted = 0
        for file in files:
            if source.is_dir() and PACKED_DATA in file.relative_to(root).parts:
                continue
            if not file.is_file() or (output and file.resolve().is_relative_to(output.resolve())):
                continue
            if cancelled():
                raise ResourceError(tr('任务已取消'))
            if operation == 'image-encode':
                if file.suffix.lower() != '.png':
                    continue
                sidecar = Path(str(file) + '.json')
                meta = json.loads(sidecar.read_text(encoding='utf-8-sig')) if sidecar.is_file() else {}
                name = Path(meta.get('source_name', file.stem + '.cbg')).name
            else:
                with file.open('rb') as stream:
                    head = stream.read(64)
                if not image_kind(head):
                    continue
                name = file.name + '.png'
            target = output or file.with_name(name if operation == 'image-export' else name + '.rebuilt')
            if source.is_dir() or len(sources) > 1 or options.get('output_directory'):
                target = (output or source.with_name(source.name + '_images')) / file.relative_to(root).parent / name
            if operation == 'image-encode':
                encode_image(file, target, metadata=options.get('metadata'), output_format=options.get('format', 'original'), quality=options.get('quality'), log=log)
            else:
                try:
                    export_image(file, target)
                except ResourceError as error:
                    if source.is_dir() and str(error) == tr('解压后的资源不是受支持的 BGI 图片'):
                        continue
                    raise
            outputs.append(target)
            converted += 1
            log(f'[{converted}] {file} -> {target}')
        if not converted and operation.startswith('image-') and not options.get('allow_empty'):
            raise ResourceError(tr('未找到可转换的图片：{0}', source))
    return outputs
