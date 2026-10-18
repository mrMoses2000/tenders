from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_procurement_db.py"
spec = importlib.util.spec_from_file_location("backup_procurement_db", SCRIPT)
assert spec is not None and spec.loader is not None
backup_procurement_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_procurement_db)
pg_environment = backup_procurement_db.pg_environment
_bounded_env_int = backup_procurement_db._bounded_env_int
_validated_session_dir = backup_procurement_db._validated_session_dir
configured_session_dir = backup_procurement_db.configured_session_dir
prune_old_backup_sets = backup_procurement_db.prune_old_backup_sets


def test_pg_environment_scopes_shared_database_to_application_schema() -> None:
    env, schema = pg_environment(
        "postgresql:///transcription_bot?host=/var/run/postgresql&search_path=procurement_bot"
    )
    assert schema == "procurement_bot"
    assert env["PGDATABASE"] == "transcription_bot"
    assert env["PGHOST"] == "/var/run/postgresql"


def test_pg_environment_keeps_password_out_of_command_arguments() -> None:
    env, schema = pg_environment(
        "postgresql://procurement:secret%20word@localhost:5433/shared?search_path=procurement_bot"
    )
    assert schema == "procurement_bot"
    assert env["PGUSER"] == "procurement"
    assert env["PGPASSWORD"] == "secret word"
    assert env["PGPORT"] == "5433"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql:///shared",
        "postgresql:///shared?search_path=public,procurement_bot",
        "postgresql:///shared?search_path=public",
        "postgresql:///shared?search_path=procurement_bot&application_name=x",
    ],
)
def test_pg_environment_rejects_unscoped_or_ambiguous_dump(dsn: str) -> None:
    with pytest.raises(ValueError):
        pg_environment(dsn)


@pytest.mark.parametrize("raw", ["0", "91", "nope"])
def test_backup_set_limit_rejects_unbounded_or_invalid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    monkeypatch.setenv("BACKUP_LIMIT", raw)
    with pytest.raises(ValueError):
        _bounded_env_int("BACKUP_LIMIT", 14, 2, 90)


def test_waha_sessions_are_excluded_until_an_absolute_path_is_configured(
    tmp_path: Path,
) -> None:
    assert configured_session_dir(None) is None
    with pytest.raises(ValueError, match="absolute"):
        configured_session_dir("relative/.sessions")
    with pytest.raises(RuntimeError, match="does not exist"):
        configured_session_dir(str(tmp_path / "missing" / ".sessions"))
    session = tmp_path / ".sessions"
    session.mkdir()
    (session / "state.json").write_text("private", encoding="utf-8")
    assert configured_session_dir(str(session)) == session


def test_session_dir_rejects_symlinks(tmp_path: Path) -> None:
    session = tmp_path / ".sessions"
    session.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (session / "escape").symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        _validated_session_dir(session)


def test_retention_prunes_only_valid_sets_and_keeps_two(tmp_path: Path) -> None:
    schema = "procurement_bot"
    sets: list[tuple[Path, Path, Path]] = []
    for day in range(1, 4):
        stamp = f"2026090{day}T000000Z"
        dump = tmp_path / f"{schema}-{stamp}-{day}.dump"
        assets = tmp_path / f"{schema}-{stamp}-{day}.assets.tar.gz"
        sidecar = dump.with_suffix(".json")
        dump.write_bytes(f"dump-{day}".encode())
        assets.write_bytes(f"assets-{day}".encode())
        sidecar.write_text(json.dumps({
            "created_at_utc": stamp,
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "assets_file": assets.name,
            "assets_sha256": hashlib.sha256(assets.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        sets.append((dump, assets, sidecar))

    # Orphaned artifacts are not mistaken for a complete set and are never pruned.
    orphan = tmp_path / f"{schema}-20260801T000000Z-0.dump"
    orphan.write_text("orphan", encoding="utf-8")

    assert prune_old_backup_sets(tmp_path, schema, max_sets=2) == 1
    assert not any(path.exists() for path in sets[0])
    assert all(path.exists() for path in sets[1] + sets[2])
    assert orphan.exists()


def test_retention_keeps_only_good_set_if_only_one_exists(tmp_path: Path) -> None:
    assert prune_old_backup_sets(tmp_path, "procurement_bot", max_sets=2) == 0
