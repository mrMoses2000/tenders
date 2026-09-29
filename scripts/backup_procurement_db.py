"""Back up only the procurement schema in a shared PostgreSQL database.

Use the same .env as the bot. Never put the database password in process args
or include unrelated schemas from the shared transcription_bot database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from procurement_bot.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "var" / "backups"
WAHA_SESSION_ENV = "PROCUREMENT_WAHA_SESSION_DIR"
SCHEMA_PATTERN = re.compile(r"[a-z_][a-z0-9_]*\Z")
DEFAULT_MAX_BACKUP_SETS = 14
MIN_MAX_BACKUP_SETS = 2
MAX_MAX_BACKUP_SETS = 90
DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validated_session_dir(path: Path) -> Path:
    """Return a WAHA session directory, rejecting unreadable paths and symlinks."""
    if not os.path.lexists(path):
        raise RuntimeError("Configured WAHA session directory does not exist")
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("Configured WAHA session path is not a regular directory")
    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(
        path, followlinks=False, onerror=raise_walk_error,
    ):
        current_path = Path(current)
        if any((current_path / name).is_symlink() for name in (*directories, *files)):
            raise RuntimeError("Configured WAHA session directory contains a symlink")
    return path


def configured_session_dir(raw_path: str | None) -> Path | None:
    """Use only an explicitly configured, absolute and accessible session path."""
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError(f"{WAHA_SESSION_ENV} must be an absolute path")
    return _validated_session_dir(path)


def _complete_backup_sets(directory: Path, schema: str) -> list[tuple[datetime, Path, Path, Path]]:
    """Return only complete sets whose sidecar names and checksums validate."""
    complete: list[tuple[datetime, Path, Path, Path]] = []
    for sidecar in directory.glob(f"{schema}-*.json"):
        dump = sidecar.with_suffix(".dump")
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            timestamp = datetime.strptime(metadata["created_at_utc"], "%Y%m%dT%H%M%SZ")
            assets_name = metadata["assets_file"]
            if Path(assets_name).name != assets_name or not assets_name.endswith(".assets.tar.gz"):
                continue
            assets = directory / assets_name
            if not dump.is_file() or not assets.is_file():
                continue
            if (_sha256(dump) != metadata["sha256"]
                    or _sha256(assets) != metadata["assets_sha256"]):
                continue
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        complete.append((timestamp, dump, assets, sidecar))
    return sorted(complete, key=lambda item: item[0], reverse=True)


def prune_old_backup_sets(directory: Path, schema: str, max_sets: int) -> int:
    """Remove oldest checksum-valid sets, always retaining at least two sets."""
    sets = _complete_backup_sets(directory, schema)
    keep_count = max(MIN_MAX_BACKUP_SETS, max_sets)
    removed = 0
    for _timestamp, dump, assets, sidecar in sets[keep_count:]:
        # A set is pruned only after a newer, fully written set exists.
        dump.unlink()
        assets.unlink()
        sidecar.unlink()
        removed += 1
    return removed


def pg_environment(dsn: str) -> tuple[dict[str, str], str]:
    """Translate the app's asyncpg DSN into libpq environment variables."""
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.path.lstrip("/"):
        raise ValueError("POSTGRES_DSN must identify a PostgreSQL database")
    options = parse_qs(parsed.query, strict_parsing=True)
    schema_values = options.pop("search_path", [])
    if (len(schema_values) != 1 or schema_values[0] in {"public", "pg_catalog"}
            or not SCHEMA_PATTERN.fullmatch(schema_values[0])):
        raise ValueError("POSTGRES_DSN must select exactly one safe application schema")
    allowed = {"host", "port", "sslmode"}
    if set(options) - allowed or any(len(values) != 1 for values in options.values()):
        raise ValueError("Unsupported POSTGRES_DSN connection options")
    env = os.environ.copy()
    env["PGDATABASE"] = unquote(parsed.path.lstrip("/"))
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    for option, variable in (("host", "PGHOST"), ("port", "PGPORT"),
                             ("sslmode", "PGSSLMODE")):
        if option in options:
            env[variable] = options[option][0]
    return env, schema_values[0]


def main() -> int:
    os.umask(0o077)
    env, schema = pg_environment(get_settings().postgres_dsn)
    max_sets = _bounded_env_int(
        "PROCUREMENT_BACKUP_MAX_SETS", DEFAULT_MAX_BACKUP_SETS,
        MIN_MAX_BACKUP_SETS, MAX_MAX_BACKUP_SETS,
    )
    min_free_bytes = _bounded_env_int(
        "PROCUREMENT_BACKUP_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES, 0,
        100 * 1024 * 1024 * 1024,
    )
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    pruned_before = prune_old_backup_sets(BACKUP_DIR, schema, max_sets)
    free_bytes = shutil.disk_usage(BACKUP_DIR).free
    if free_bytes < min_free_bytes:
        raise RuntimeError(
            f"Insufficient free space for backup: {free_bytes} bytes available; "
            f"configured minimum is {min_free_bytes}"
        )
    session_dir = configured_session_dir(os.environ.get(WAHA_SESSION_ENV))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{schema}-", suffix=".dump.tmp",
                                                   dir=BACKUP_DIR)
    os.close(descriptor)
    temporary = Path(temporary_name)
    descriptor, assets_temporary_name = tempfile.mkstemp(
        prefix=f".{schema}-", suffix=".assets.tmp", dir=BACKUP_DIR,
    )
    os.close(descriptor)
    assets_temporary = Path(assets_temporary_name)
    destination: Path | None = None
    assets_destination: Path | None = None
    sidecar: Path | None = None
    sidecar_temporary: Path | None = None
    try:
        dump = subprocess.run(  # noqa: S603 - fixed binary and validated schema
            ["/usr/bin/pg_dump", "--format=custom", "--no-owner", "--no-acl",
             f"--schema={schema}", f"--file={temporary}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, timeout=600, check=False,
        )
        if dump.returncode:
            raise RuntimeError(f"pg_dump failed with exit status {dump.returncode}")
        listed = subprocess.run(  # noqa: S603 - fixed binary and private archive
            ["/usr/bin/pg_restore", "--list", str(temporary)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=60, check=False,
        )
        if listed.returncode or f"TABLE {schema} " not in listed.stdout:
            raise RuntimeError("pg_restore could not read application tables from the archive")
        for directory_name in ("media", "results"):
            if not (ROOT / "var" / directory_name).is_dir():
                raise RuntimeError(f"Missing application file directory: var/{directory_name}")
        with tarfile.open(assets_temporary, "w:gz", dereference=False) as archive:
            for directory_name in ("media", "results"):
                archive.add(ROOT / "var" / directory_name, arcname=directory_name)
            if session_dir is not None:
                # WAHA auth state is sensitive; the archive inherits private
                # permissions from mkstemp and the process umask.
                archive.add(session_dir, arcname="waha-sessions")
        with tarfile.open(assets_temporary, "r:gz") as archive:
            members = archive.getnames()
            if not all(directory_name in members for directory_name in ("media", "results")):
                raise RuntimeError("Application file archive is incomplete")
            sessions_included = any(
                member == "waha-sessions"
                or member.startswith("waha-sessions/")
                for member in members
            )
            if sessions_included != (session_dir is not None):
                raise RuntimeError("WAHA session coverage does not match the archive contents")
        digest = _sha256(temporary)
        assets_digest = _sha256(assets_temporary)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = BACKUP_DIR / f"{schema}-{timestamp}-{os.getpid()}.dump"
        assets_destination = BACKUP_DIR / f"{schema}-{timestamp}-{os.getpid()}.assets.tar.gz"
        _fsync_file(temporary)
        _fsync_file(assets_temporary)
        temporary.replace(destination)
        assets_temporary.replace(assets_destination)
        metadata = {"created_at_utc": timestamp, "schema": schema,
                    "sha256": digest, "bytes": destination.stat().st_size,
                    "assets_file": assets_destination.name,
                    "assets_sha256": assets_digest,
                    "assets_bytes": assets_destination.stat().st_size,
                    "waha_sessions": (
                        "included_live" if sessions_included else "excluded_not_configured"
                    ),
                    "max_backup_sets": max_sets,
                    "minimum_free_bytes": min_free_bytes}
        sidecar = destination.with_suffix(".json")
        sidecar_temporary = BACKUP_DIR / f".{sidecar.name}.tmp"
        with sidecar_temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        sidecar_temporary.chmod(0o600)
        sidecar_temporary.replace(sidecar)
        directory_fd = os.open(BACKUP_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        try:
            removed = prune_old_backup_sets(BACKUP_DIR, schema, max_sets)
        except OSError as exc:
            print(f"WARNING: backup completed but retention cleanup failed: {type(exc).__name__}")
            return 1
        session_status = (
            "configured WAHA session included (live copy; consistency is not guaranteed)"
            if sessions_included else "WAHA session excluded; session directory is not configured"
        )
        complete_count = len(_complete_backup_sets(BACKUP_DIR, schema))
        print(f"Backup ready: {destination.name} ({metadata['bytes']} bytes); "
              f"{assets_destination.name} ({metadata['assets_bytes']} bytes); "
              f"{session_status}; retained complete sets={complete_count}; "
              f"pruned={pruned_before + removed}")
        return 0
    finally:
        temporary.unlink(missing_ok=True)
        assets_temporary.unlink(missing_ok=True)
        if sidecar_temporary is not None:
            sidecar_temporary.unlink(missing_ok=True)
        if sidecar is not None and not sidecar.exists():
            destination.unlink(missing_ok=True)
            assets_destination.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
