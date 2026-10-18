"""Resolve runtime data from a checkout or an installed wheel."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

_RESOURCE_NAMES = frozenset({"migrations", "mini_app"})


def resource_path(name: str) -> Path:
    """Return a filesystem path to a bundled runtime resource directory.

    Source checkouts keep resources at the repository root. Wheels carry copies
    under ``procurement_bot/_resources`` so the CLI and Mini App work after
    installation outside the source tree.
    """

    if name not in _RESOURCE_NAMES:
        raise ValueError(f"Unsupported application resource: {name}")

    repository_root = Path(__file__).resolve().parents[2]
    is_checkout = (repository_root / "pyproject.toml").is_file() and (
        repository_root / "src" / "procurement_bot"
    ).is_dir()
    checkout_path = repository_root / name
    if is_checkout and checkout_path.is_dir():
        return checkout_path

    package_path = Path(str(files("procurement_bot").joinpath("_resources", name)))
    if package_path.is_dir():
        return package_path
    raise FileNotFoundError(f"Application resource directory is missing: {name}")
