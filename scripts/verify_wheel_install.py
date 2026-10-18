"""Smoke-check runtime resources from an installed wheel in an isolated target."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_wheel_install.py WHEEL")

    wheel = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="procurement-wheel-smoke-") as temp_dir:
        target = Path(temp_dir) / "installed"
        # The wheel path is a CLI argument from this local verification run.
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--target",
                str(target),
                str(wheel),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        sys.path.insert(0, str(target))
        importlib.invalidate_caches()

        from procurement_bot.resources import resource_path

        package_root = Path(importlib.import_module("procurement_bot").__file__).parent
        assert target in package_root.parents
        migration_dir = resource_path("migrations")
        mini_app_dir = resource_path("mini_app")
        migrations = sorted(migration_dir.glob("*.sql"))
        assert migrations, "wheel contains no SQL migrations"
        assert (mini_app_dir / "index.html").is_file()
        assert (mini_app_dir / "app.css").is_file()
        assert (mini_app_dir / "app.js").is_file()
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert any(
                "_resources/migrations/" in name and name.endswith(".sql")
                for name in names
            )
            assert any("_resources/mini_app/index.html" in name for name in names)

        cli = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "procurement_bot.cli", "--help"],
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(target)},
        )
        assert cli.returncode == 0, cli.stderr
        print(
            f"Wheel smoke passed: {len(migrations)} migrations and Mini App assets are installed."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
