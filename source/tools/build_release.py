"""Build a Windows x64 portable release with an explicit public mapping."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTIONS = ('PyQt6', 'PyQt6-Qt6', 'PyQt6-sip', 'Pillow', 'darkdetect', 'numpy',
                 'numba', 'llvmlite', 'opencv-python', 'imageio-ffmpeg', 'pyinstaller')


def copy_source(destination):
    shutil.copytree(ROOT / 'bgi_toolkit', destination / 'bgi_toolkit',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '*.obj', '*.exp', '*.lib'))
    for name in ('pyproject.toml', 'README.md', 'run_gui.bat'):
        if name != 'README.md' or (ROOT / name).is_file():
            shutil.copy2(ROOT / name, destination / name)
    (destination / 'tools').mkdir()
    for name in ('build_release.py', 'build_wheel.py', 'release_entry.py', 'release_smoke.py',
                 'release_README.md', 'release_README_EN.md'):
        shutil.copy2(ROOT / 'tools' / name, destination / 'tools' / name)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping', type=Path, required=True, help='Explicit release-only BSS name table')
    parser.add_argument('--output', type=Path, default=ROOT / 'release')
    parser.add_argument('--work', type=Path)
    parser.add_argument('--ffmpeg', type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or sys.maxsize <= 2**32:
        parser.error('Windows x64 Python is required')
    mapping_path = args.mapping.resolve()
    public_mapping = json.loads(mapping_path.read_text(encoding='utf-8-sig'))
    if not isinstance(public_mapping, dict) or len(public_mapping) != 56:
        parser.error('This release requires the 56-entry public Wiki mapping')
    output = args.output.resolve() / 'BGI_Toolkit'
    if output.exists():
        parser.error(f'Refusing to overwrite {output}')
    work = (args.work or ROOT / 'artifacts' / ('release_build_' + datetime.now().strftime('%Y%m%d_%H%M%S'))).resolve()
    work.mkdir(parents=True, exist_ok=False)
    staged = work / 'source'
    staged.mkdir()
    copy_source(staged)
    source_mapping = staged / 'bgi_toolkit/asdis/resources/bss_mapping.json'
    shutil.copy2(mapping_path, source_mapping)
    # No additional name-table copies are shipped inside the runtime.
    for extra in source_mapping.parent.glob('bss_mapping_*release*.json'):
        extra.unlink()
    if args.ffmpeg:
        ffmpeg = args.ffmpeg.resolve()
    else:
        import imageio_ffmpeg
        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    bundled_ffmpeg = work / 'ffmpeg.exe'
    shutil.copy2(ffmpeg, bundled_ffmpeg)
    package = staged / 'bgi_toolkit'
    data_files = [(str(path), str(path.parent.relative_to(staged)))
                  for path in package.rglob('*') if path.is_file()
                  and path.suffix.lower() in {'.json', '.svg', '.c', '.cpp', '.h', '.bat', '.txt'}]
    binaries = [(str(path), 'bgi_toolkit/native') for path in (package / 'native').glob('*.dll')]
    binaries.append((str(bundled_ffmpeg), 'ffmpeg'))
    spec = work / 'BGI_Toolkit.spec'
    spec.write_text(f'''
from PyInstaller.utils.hooks import collect_submodules, copy_metadata
datas = {data_files!r}
for distribution in {DISTRIBUTIONS!r}:
    datas += copy_metadata(distribution)
a = Analysis([{str(staged / 'tools/release_entry.py')!r}],
    pathex=[{str(staged)!r}, {str(staged / 'tools')!r}],
    binaries={binaries!r}, datas=datas,
    hiddenimports=collect_submodules('bgi_toolkit'),
    excludes=['PyQt5', 'PySide2', 'PySide6', 'matplotlib', 'scipy', 'pandas',
              'IPython', 'notebook', 'torch', 'tensorflow', 'sklearn', 'tkinter'],
    module_collection_mode={{'bgi_toolkit': 'pyz+py', 'numba': 'pyz+py'}},
    noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BGI_Toolkit',
          debug=False, strip=False, upx=False, console=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BGI_Toolkit_cli',
          debug=False, strip=False, upx=False, console=True)
COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name='BGI_Toolkit')
''', encoding='utf-8')
    # Foreign toolchains on PATH may supply an incompatible icuuc.dll for Qt.
    # Qt uses the Windows system ICU; never pick up Poppler/Conda's version.
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    build_path = os.pathsep.join(map(str, (Path(sys.executable).parent,
        Path(sys.base_prefix) / 'Scripts', windows / 'System32', windows)))
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PATH=build_path)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--log-level', 'INFO',
                    '--distpath', str(work / 'dist'), '--workpath', str(work / 'build'), str(spec)],
                   cwd=staged, env=environment, check=True)
    built = work / 'dist/BGI_Toolkit'
    shutil.copytree(staged, built / 'source')
    licenses = built / 'licenses'
    shutil.copytree(package / 'licenses', licenses / 'native-algorithms')
    for name in DISTRIBUTIONS:
        distribution = metadata.distribution(name)
        for relative in distribution.files or []:
            # Wheel license directories and the cv2 runtime notices.
            text = str(relative).lower().replace('\\', '/')
            if '.dist-info/' in text and any(word in Path(text).name for word in ('license', 'copying', 'notice', 'copyright')):
                path = Path(distribution.locate_file(relative))
                if path.is_file():
                    target = licenses / name / Path(relative).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
    ffmpeg_license = subprocess.run([str(bundled_ffmpeg), '-L'], capture_output=True, check=True)
    (licenses / 'FFmpeg-LICENSE.txt').write_bytes(ffmpeg_license.stdout + ffmpeg_license.stderr)
    ffmpeg_version = subprocess.run([str(bundled_ffmpeg), '-version'], capture_output=True, check=True).stdout.decode('utf-8', errors='replace')
    (licenses / 'FFmpeg-build.txt').write_text(ffmpeg_version, encoding='utf-8')
    for name in ('README.md', 'README_EN.md'):
        shutil.copy2(ROOT / 'tools' / ('release_' + name), built / name)
    (built / 'run_gui.bat').write_text('@echo off\ncd /d "%~dp0"\nstart "" "%~dp0BGI_Toolkit.exe"\n', encoding='ascii', newline='\r\n')
    (built / 'THIRD_PARTY_NOTICES.md').write_text('''# Third-party components

License texts and notices are preserved in `licenses` and the runtime's package metadata.
Native resource algorithms are adapted from GARbro (morkt, MIT) and EthornellTools (arcusmaximus, MIT).
Python: https://www.python.org/ ; PyQt6: https://www.riverbankcomputing.com/software/pyqt/ ; Qt: https://www.qt.io/
NumPy: https://numpy.org/ ; Numba and llvmlite: https://numba.pydata.org/ ; OpenCV: https://opencv.org/
Pillow: https://python-pillow.org/ ; darkdetect: https://github.com/albertosottile/darkdetect
FFmpeg is the existing imageio-ffmpeg Windows binary; version/configuration and license output are in `licenses/FFmpeg-build.txt` and `licenses/FFmpeg-LICENSE.txt`.
FFmpeg source: https://ffmpeg.org/releases/ ; binary provenance: https://github.com/imageio/imageio-binaries and https://www.gyan.dev/ffmpeg/builds/
PyInstaller: https://pyinstaller.org/ ; project native DLL sources and build scripts: `source/bgi_toolkit/native`.
''', encoding='utf-8')
    manifest = {'platform': 'Windows x64', 'python': sys.version, 'mapping_entries': len(public_mapping),
                'mapping_sha256': sha256(source_mapping),
                'dependencies': {name: metadata.version(name) for name in DISTRIBUTIONS},
                'files': {str(path.relative_to(built)).replace('\\', '/'): sha256(path)
                          for path in built.rglob('*') if path.is_file()}}
    (built / 'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert json.loads((built / '_internal/bgi_toolkit/asdis/resources/bss_mapping.json').read_text(encoding='utf-8')) == public_mapping
    output.parent.mkdir(parents=True, exist_ok=True)
    if not built.resolve().is_relative_to(work) or not output.resolve().is_relative_to(args.output.resolve()):
        raise RuntimeError('Release move would leave the intended build or output directory')
    shutil.move(str(built), str(output))
    print(f'Release ready: {output}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
