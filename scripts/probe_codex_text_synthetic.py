"""One authorized generic model call; no .env, DB, business payload or auth commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from procurement_bot.config import Settings
from procurement_bot.configured_extractor import ConfiguredStructuredExtractor


class Greeting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    word: Literal["hello"]


def metadata(path: Path):
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


async def main() -> None:
    settings = Settings(_env_file=None, structured_text_provider="codex")
    selected = ConfiguredStructuredExtractor(settings)
    fixture = "Return the literal greeting hello."
    context = {"fixture": "generic-greeting-v1"}
    context_bytes = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    fixture_hash = hashlib.sha256(fixture.encode()).hexdigest()
    auth_path = settings.codex_text_codex_home / "auth.json"
    before = metadata(auth_path)
    started = time.monotonic()
    outcome = {
        "model": settings.codex_text_model,
        "effort": settings.codex_text_reasoning_effort,
        "timeout_seconds": settings.codex_text_timeout_seconds,
        "fixture_sha256": fixture_hash,
        "schema_and_binding_valid": False,
    }
    try:
        result = await selected.extract(
            fixture,
            Greeting,
            context=context,
            spec_sha256=fixture_hash,
            context_sha256=hashlib.sha256(context_bytes).hexdigest(),
        )
        outcome["schema_and_binding_valid"] = result.word == "hello"
    except Exception as exc:
        # Exceptions from this adapter have fixed codes; never print CLI data.
        outcome["diagnostic"] = getattr(exc, "code", type(exc).__name__)
    finally:
        outcome["elapsed_seconds"] = round(time.monotonic() - started, 3)
        outcome["auth_file_metadata_unchanged"] = before == metadata(auth_path)
    print(json.dumps(outcome, sort_keys=True))
    if not outcome["schema_and_binding_valid"] or not outcome["auth_file_metadata_unchanged"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
