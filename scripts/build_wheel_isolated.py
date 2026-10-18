"""Build a wheel from the current source without writing to the checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_INPUTS = (
    "README.md",
    "MANIFEST.in",
    "pyproject.toml",
    "setup.py",
    "src",
    "migrations",
    "mini_app",
)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_wheel_isolated.py WHEEL_OUTPUT_DIRECTORY")
    repository = Path(__file__).resolve().parents[1]
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="procurement-wheel-build-") as temp_dir:
        staged_project = Path(temp_dir) / "project"
        build_tools = Path(temp_dir) / "build-tools"
        sdist_output = Path(temp_dir) / "sdist"
        build_tools.mkdir()
        sdist_output.mkdir()
        staged_project.mkdir()
        for relative_path in PROJECT_INPUTS:
            source = repository / relative_path
            destination = staged_project / relative_path
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--target",
                str(build_tools),
                "build==1.3.0",
            ],
            check=True,
        )
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "build",
                "--sdist",
                "--outdir",
                str(sdist_output),
                str(staged_project),
            ],
            check=True,
            env={**os.environ, "PYTHONPATH": str(build_tools)},
        )
        sdists = list(sdist_output.glob("*.tar.gz"))
        if len(sdists) != 1:
            raise RuntimeError(
                "Expected one source distribution, found "
                f"{len(sdists)}: {[path.name for path in sdist_output.iterdir()]}"
            )
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(output),
                str(sdists[0]),
            ],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
