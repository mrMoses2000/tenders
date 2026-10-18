"""Preflight or attach a reviewed JSON request to an existing manual case.

Default mode is read-only. `--apply` only creates the canonical parse version and
ready projections; it does not create a research run, contact suppliers, or send messages.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.intake import ParsedProcurementRequest
from procurement_bot.operator_case_import import (
    apply_operator_import,
    preflight_operator_import,
    source_manifest_sha256,
)


def _read_envelope(path: Path) -> tuple[dict[str, Any], ParsedProcurementRequest, str]:
    raw = path.read_bytes()
    try:
        envelope = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("normalized request file must be valid UTF-8 JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"source_artifacts", "request"}:
        raise ValueError("file must contain exactly source_artifacts and request")
    if not isinstance(envelope["source_artifacts"], list) or not envelope["source_artifacts"]:
        raise ValueError("source_artifacts must be a non-empty list")
    source_manifest_sha256(envelope["source_artifacts"])
    return (
        envelope,
        ParsedProcurementRequest.model_validate(envelope["request"]),
        hashlib.sha256(raw).hexdigest(),
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    envelope, request, request_file_hash = _read_envelope(args.normalized_request_file)
    if request_file_hash != args.expected_request_file_sha256:
        raise ValueError("normalized request file hash differs from reviewed hash")
    if len(args.source_file) != len(args.expected_source_sha256):
        raise ValueError("pass one --expected-source-sha256 for each --source-file")
    actual_artifacts = []
    for path, expected in zip(args.source_file, args.expected_source_sha256, strict=True):
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("--expected-source-sha256 must be lowercase SHA-256")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"source artifact hash mismatch: {path.name}")
        actual_artifacts.append({"reference": path.name, "sha256": digest})
    if actual_artifacts != envelope["source_artifacts"]:
        raise ValueError("source artifact file names/order/hashes differ from reviewed envelope")
    source_hash = source_manifest_sha256(actual_artifacts)
    source_reference = "; ".join(
        f"{artifact['reference']}#sha256={artifact['sha256']}" for artifact in actual_artifacts
    )

    pool = await create_pool(get_settings().postgres_dsn, min_size=1, max_size=1)
    try:
        if args.apply:
            result = await apply_operator_import(
                pool,
                case_id=args.case_id,
                owner_id=args.owner_id,
                request=request,
                expected_request_sha256=args.expected_request_sha256,
                expected_projection_sha256=args.expected_projection_sha256,
                source_sha256=source_hash,
                source_artifacts=actual_artifacts,
                source_reference=source_reference,
            )
            return {"mode": "apply", **result}
        result = await preflight_operator_import(
            pool,
            case_id=args.case_id,
            owner_id=args.owner_id,
            request=request,
            expected_request_sha256=args.expected_request_sha256,
            expected_projection_sha256=args.expected_projection_sha256,
        )
        return {
            "mode": "dry-run",
            "normalized_request_file_sha256": request_file_hash,
            "source_sha256": source_hash,
            "source_artifacts": actual_artifacts,
            **result,
        }
    finally:
        await pool.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True, type=UUID)
    parser.add_argument("--owner-id", required=True, type=UUID)
    parser.add_argument("--normalized-request-file", required=True, type=Path)
    parser.add_argument("--source-file", type=Path, action="append", required=True)
    parser.add_argument("--expected-source-sha256", action="append", required=True)
    parser.add_argument("--expected-request-file-sha256", required=True)
    parser.add_argument("--expected-request-sha256", required=True)
    parser.add_argument("--expected-projection-sha256", required=True)
    parser.add_argument(
        "--apply", action="store_true", help="perform the validated canonical import"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    for name in (
        "expected_request_file_sha256",
        "expected_request_sha256",
        "expected_projection_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", getattr(args, name)):
            raise SystemExit(f"--{name.replace('_', '-')} must be lowercase SHA-256")
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
