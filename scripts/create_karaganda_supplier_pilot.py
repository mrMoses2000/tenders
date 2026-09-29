"""Create a separate four-line NIS Karaganda supplier-search pilot.

Dry-run is the default. The source case and its history are never updated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

SOURCE_CASE_ID = UUID("b1c03088-5ee9-4d5c-8dcd-4f9c7a181619")
SOURCE_LINES = (34, 37, 39, 42)
PURPOSE = "almaty1_supplier_pilot_20260924"
TITLE = "Пилот поставщиков — НИШ Караганда — Алматы-1"


async def create(*, apply: bool) -> dict[str, object]:
    from procurement_bot.config import get_settings
    from procurement_bot.db import create_pool

    pool = await create_pool(get_settings().postgres_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", PURPOSE,
            )
            source = await connection.fetchrow(
                """
                SELECT id,owner_user_id,customer_id,title
                FROM procurement_cases WHERE id=$1
                """,
                SOURCE_CASE_ID,
            )
            if source is None or source["customer_id"] is None:
                raise ValueError("Karaganda source case or customer is absent")
            rows = await connection.fetch(
                """
                SELECT id,line_number,name,specification_text,quantity,unit,analogs_allowed
                FROM request_items
                WHERE case_id=$1 AND line_number=ANY($2::integer[])
                ORDER BY line_number
                """,
                SOURCE_CASE_ID, list(SOURCE_LINES),
            )
            if tuple(row["line_number"] for row in rows) != SOURCE_LINES:
                raise ValueError("one or more selected source positions are absent")
            report = {
                "source_case_id": str(SOURCE_CASE_ID),
                "purpose": PURPOSE,
                "title": TITLE,
                "search_city": "Алматы",
                "search_area": "Алматы-1",
                "positions": [
                    {"source_line": row["line_number"], "name": row["name"],
                     "quantity": str(row["quantity"]) if row["quantity"] else None,
                     "unit": row["unit"]}
                    for row in rows
                ],
            }
            if not apply:
                return report
            derivation = await connection.fetchrow(
                """
                SELECT id,derived_case_id FROM case_derivations
                WHERE source_case_id=$1 AND purpose=$2
                """,
                SOURCE_CASE_ID, PURPOSE,
            )
            if derivation is None:
                derived_case_id = await connection.fetchval(
                    """
                    INSERT INTO procurement_cases(
                        owner_user_id,customer_id,title,city,search_area_text,
                        status,source_kind
                    ) VALUES ($1,$2,$3,'Алматы','Алматы-1','ready','manual')
                    RETURNING id
                    """,
                    source["owner_user_id"], source["customer_id"], TITLE,
                )
                derivation = await connection.fetchrow(
                    """
                    INSERT INTO case_derivations(source_case_id,derived_case_id,purpose)
                    VALUES ($1,$2,$3) RETURNING id,derived_case_id
                    """,
                    SOURCE_CASE_ID, derived_case_id, PURPOSE,
                )
                for new_line, row in enumerate(rows, 1):
                    derived_item_id = await connection.fetchval(
                        """
                        INSERT INTO request_items(
                            case_id,line_number,name,specification_text,quantity,
                            unit,analogs_allowed,status
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,'ready') RETURNING id
                        """,
                        derived_case_id, new_line, row["name"],
                        row["specification_text"], row["quantity"], row["unit"],
                        row["analogs_allowed"],
                    )
                    await connection.execute(
                        """
                        INSERT INTO item_derivations(
                            derived_item_id,source_item_id,case_derivation_id
                        ) VALUES ($1,$2,$3)
                        """,
                        derived_item_id, row["id"], derivation["id"],
                    )
            else:
                actual = await connection.fetch(
                    """
                    SELECT i.line_number,i.name,d.source_item_id
                    FROM item_derivations d
                    JOIN request_items i ON i.id=d.derived_item_id
                    WHERE d.case_derivation_id=$1 ORDER BY i.line_number
                    """,
                    derivation["id"],
                )
                expected = [(index, row["name"], row["id"])
                            for index, row in enumerate(rows, 1)]
                if [(row["line_number"], row["name"], row["source_item_id"])
                    for row in actual] != expected:
                    raise ValueError("existing pilot derivation differs from source items")
            report["case_id"] = str(derivation["derived_case_id"])
            return report
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write pilot case")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(create(apply=args.apply)), ensure_ascii=False))


if __name__ == "__main__":
    main()
