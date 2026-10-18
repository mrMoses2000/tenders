"""One-time audited reconciliation of four historically rewritten SQL files.

Run without arguments to inspect; pass --apply only after reviewing the output.
The original recorded SHA-256 values are preserved in an audit table.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

from procurement_bot.config import Settings
from procurement_bot.db import create_pool

ROOT = Path(__file__).resolve().parents[1]
OLD_HASHES = {
    "005_report_artifacts.sql": "c82f83c271f94c230a7bc18f67a9f9d9c3e7b9f3bd83656a6786463ea381b86b",
    "009_supplier_languages.sql": (
        "398e973afc89a60c32e64a84da49eb22aad5d84be0c56243b0076ac6a629871b"
    ),
    "010_purchase_evidence_requests.sql": (
        "50d6880c73428d6fccf67972356b4a01d29c5c31e19dbd44fd086cddfd686b85"
    ),
    "012_dialogue_turns.sql": "e9d720fdc961b8207e9174b9ff6e0730b00cdf8a9326855f6b92f1a019dd14cc",
}
REQUIRED_COLUMNS = {
    "report_artifacts": {
        "id", "case_id", "research_run_id", "version", "artifact_kind",
        "storage_path", "sha256", "byte_size", "summary", "idempotency_key",
        "is_current", "created_at",
    },
    "supplier_conversations": {"preferred_language"},
    "supplier_conversation_messages": {"language_code"},
    "purchase_evidence_requests": {
        "id", "case_id", "request_item_id", "owner_user_id",
        "requested_in_message_id", "incoming_attachment_id", "source_artifact_id",
        "status", "raw_statement", "created_at", "updated_at", "completed_at",
    },
    "dialogue_turns": {
        "message_id", "user_id", "case_id", "intent", "decision_source",
        "reason_code", "input_kind", "context_version", "open_clarification_id",
        "status", "response_kind", "created_at", "processed_at",
    },
}
REQUIRED_INDEXES = {
    "report_artifacts_one_current_idx", "report_artifacts_case_idx",
    "purchase_evidence_one_waiting_per_owner_idx", "purchase_evidence_case_status_idx",
    "dialogue_turns_user_created_idx", "dialogue_turns_case_created_idx",
}
REQUIRED_CONSTRAINTS = {
    "supplier_conversations_preferred_language_check",
    "supplier_conversation_messages_language_check",
}


async def _verify_catalog(connection) -> None:
    for table, required in REQUIRED_COLUMNS.items():
        rows = await connection.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=$1""",
            table,
        )
        missing = required - {row["column_name"] for row in rows}
        if missing:
            raise RuntimeError(f"{table} is missing columns: {sorted(missing)}")
    rows = await connection.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"
    )
    missing_indexes = REQUIRED_INDEXES - {row["indexname"] for row in rows}
    if missing_indexes:
        raise RuntimeError(f"missing indexes: {sorted(missing_indexes)}")
    rows = await connection.fetch(
        """SELECT conname FROM pg_constraint
           WHERE connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())"""
    )
    missing_constraints = REQUIRED_CONSTRAINTS - {row["conname"] for row in rows}
    if missing_constraints:
        raise RuntimeError(f"missing constraints: {sorted(missing_constraints)}")


async def main(apply: bool) -> None:
    pool = await create_pool(Settings().postgres_dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as connection:
            await connection.execute("SELECT pg_advisory_lock($1)", 718_639_041)
            try:
                async with connection.transaction():
                    await _verify_catalog(connection)
                    changes = []
                    for name, old_hash in OLD_HASHES.items():
                        source = (ROOT / "migrations" / name).read_bytes()
                        new_hash = hashlib.sha256(source).hexdigest()
                        recorded = await connection.fetchval(
                            "SELECT sha256 FROM schema_migrations WHERE name=$1", name,
                        )
                        if recorded not in {old_hash, new_hash}:
                            raise RuntimeError(f"unexpected recorded hash for {name}: {recorded}")
                        if recorded == old_hash:
                            changes.append((name, old_hash, new_hash))
                        print(f"{name}: {recorded} -> {new_hash}")
                    if not apply:
                        print(f"dry run: {len(changes)} migration hashes need reconciliation")
                        return
                    await connection.execute(
                        """CREATE TABLE IF NOT EXISTS schema_migration_reconciliations (
                               name TEXT NOT NULL,
                               old_sha256 TEXT NOT NULL,
                               new_sha256 TEXT NOT NULL,
                               reason TEXT NOT NULL,
                               reconciled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                               PRIMARY KEY(name,old_sha256,new_sha256)
                           )"""
                    )
                    for name, old_hash, new_hash in changes:
                        await connection.execute(
                            """INSERT INTO schema_migration_reconciliations(
                                   name,old_sha256,new_sha256,reason
                               ) VALUES ($1,$2,$3,$4)
                               ON CONFLICT DO NOTHING""",
                            name, old_hash, new_hash,
                            "Current schema objects verified; historical source file unavailable",
                        )
                        result = await connection.execute(
                            """UPDATE schema_migrations SET sha256=$3
                               WHERE name=$1 AND sha256=$2""",
                            name, old_hash, new_hash,
                        )
                        if result != "UPDATE 1":
                            raise RuntimeError(f"concurrent migration journal change: {name}")
                    print(f"reconciled {len(changes)} hashes with audit records")
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1)", 718_639_041)
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
