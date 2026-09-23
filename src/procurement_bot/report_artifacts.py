from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg


class ReportArtifactConflict(RuntimeError):
    """An idempotency key was reused for different report bytes or provenance."""


@dataclass(frozen=True, slots=True)
class StoredReportArtifact:
    id: UUID
    case_id: UUID
    version: int
    storage_path: str
    sha256: str
    byte_size: int
    created: bool


def _canonical_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        raise TypeError("report summary must be an object")
    if any(not isinstance(key, str) for key in summary):
        raise TypeError("report summary keys must be strings")
    try:
        encoded = json.dumps(
            dict(summary),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("report summary must contain finite JSON values") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("report summary must be an object")
    return decoded


def store_report_html(results_root: Path, html: str) -> tuple[str, str, int]:
    """Atomically store private UTF-8 HTML under a content-addressed key."""

    if not html.strip():
        raise ValueError("report HTML must be non-empty")
    body = html.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    root = results_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    directory = root / "reports" / digest[:2]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / f"{digest}.html"
    if target.exists():
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ReportArtifactConflict("content-addressed report path contains other bytes")
        target.chmod(0o600)
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".report-", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(target)
            target.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
    storage_path = target.relative_to(root).as_posix()
    return storage_path, digest, len(body)


async def persist_report_artifact(
    connection: asyncpg.Connection,
    *,
    results_root: Path,
    case_id: UUID,
    research_run_id: UUID | None,
    html: str,
    summary: Mapping[str, Any],
    idempotency_key: str,
) -> StoredReportArtifact:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must be non-empty")
    normalized_summary = _canonical_summary(summary)
    storage_path, digest, byte_size = store_report_html(results_root, html)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"report-artifact:{case_id}",
        )
        existing = await connection.fetchrow(
            "SELECT * FROM report_artifacts WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            expected = {
                "case_id": case_id,
                "research_run_id": research_run_id,
                "storage_path": storage_path,
                "sha256": digest,
                "byte_size": byte_size,
                "summary": normalized_summary,
            }
            changed = [key for key, value in expected.items() if existing[key] != value]
            if changed:
                raise ReportArtifactConflict(
                    "report idempotency key drift: " + ", ".join(sorted(changed))
                )
            return StoredReportArtifact(
                id=existing["id"],
                case_id=existing["case_id"],
                version=existing["version"],
                storage_path=existing["storage_path"],
                sha256=existing["sha256"],
                byte_size=existing["byte_size"],
                created=False,
            )
        version = await connection.fetchval(
            "SELECT COALESCE(MAX(version),0)+1 FROM report_artifacts WHERE case_id=$1",
            case_id,
        )
        await connection.execute(
            "UPDATE report_artifacts SET is_current=FALSE WHERE case_id=$1 AND is_current",
            case_id,
        )
        artifact_id = uuid4()
        await connection.execute(
            """
            INSERT INTO report_artifacts(
                id,case_id,research_run_id,version,storage_path,sha256,byte_size,
                summary,idempotency_key,is_current
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,TRUE)
            """,
            artifact_id,
            case_id,
            research_run_id,
            version,
            storage_path,
            digest,
            byte_size,
            normalized_summary,
            idempotency_key,
        )
    return StoredReportArtifact(
        id=artifact_id,
        case_id=case_id,
        version=version,
        storage_path=storage_path,
        sha256=digest,
        byte_size=byte_size,
        created=True,
    )


def resolve_report_path(results_root: Path, storage_path: str) -> Path:
    root = results_root.expanduser().resolve()
    candidate = (root / storage_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("report storage path escapes RESULTS_ROOT")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate
