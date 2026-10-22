"""Build a wheel in dist without leaving build/ or egg-info in the workspace."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="构建安装包，中间文件使用系统临时目录。")
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="安装包输出目录（默认 dist）")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bgi_toolkit_build_") as folder:
        stage = Path(folder)
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(ROOT / name, stage / name)
        shutil.copytree(ROOT / "bgi_toolkit", stage / "bgi_toolkit",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        environment = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(stage), "--no-deps", "--no-build-isolation",
             "--no-index", "--wheel-dir", str(output)],
            cwd=stage, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
