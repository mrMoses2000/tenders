from __future__ import annotations

from pathlib import Path

import pytest

from procurement_bot.db import run_migrations
from procurement_bot.resources import resource_path


def test_source_checkout_resources_are_available() -> None:
    migrations = resource_path("migrations")
    mini_app = resource_path("mini_app")

    assert migrations.is_dir()
    assert (migrations / "001_initial.sql").is_file()
    assert mini_app.is_dir()
    assert {"index.html", "app.css", "app.js"} <= {
        path.name for path in mini_app.iterdir()
    }


def test_resource_name_is_allowlisted() -> None:
    try:
        resource_path("../secrets")
    except ValueError as exc:
        assert "Unsupported" in str(exc)
    else:
        raise AssertionError("unlisted resources must be refused")


def test_resource_paths_are_path_objects() -> None:
    assert isinstance(resource_path("migrations"), Path)


@pytest.mark.asyncio
async def test_migration_runner_rejects_an_empty_resource_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="No numbered migrations"):
        await run_migrations(None, tmp_path)  # type: ignore[arg-type]
