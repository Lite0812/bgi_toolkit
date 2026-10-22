"""Shared entry point for the portable GUI and console executables."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import sys
import tempfile


def main() -> int:
    multiprocessing.freeze_support()
    frozen = getattr(sys, 'frozen', False)
    if frozen:
        runtime = Path(sys._MEIPASS)
        os.environ['PATH'] = str(runtime / 'ffmpeg') + os.pathsep + os.environ.get('PATH', '')
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if sys.stdout is None or sys.stderr is None:
        logs = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'BGI Toolkit' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        stream = (logs / 'startup.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    arguments = sys.argv[1:]
    if arguments[:1] == ['--self-test']:
        from release_smoke import run
        return run(Path(arguments[1]) if len(arguments) > 1 else None)
    if not arguments and frozen and Path(sys.executable).stem == 'BGI_Toolkit':
        arguments = ['gui']
    from bgi_toolkit.cli import main as cli_main
    return cli_main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
