from __future__ import annotations

import asyncio
import os

import pytest

from scripts.postgres_test_safety import (
    configured_postgres_target,
    verify_postgres_test_connection,
)


@pytest.fixture(scope="session", autouse=True)
def verify_test_postgres_target() -> None:
    """Fail closed on unsafe TEST_POSTGRES_DSN before integration tests write."""

    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        return
    target = configured_postgres_target(dsn)
    asyncio.run(verify_postgres_test_connection(dsn, target))


@pytest.fixture(autouse=True)
def isolate_codex_chrome_lock(tmp_path, monkeypatch):
    """Keep browser-provider tests off the live user's shared Chrome lock."""

    from procurement_bot.providers.codex_chrome import CodexChromeExecutor

    original_init = CodexChromeExecutor.__init__
    test_lock_path = tmp_path / "codex-chrome-owner.lock"

    def init_with_test_lock(self, *args, **kwargs):
        kwargs.setdefault("chrome_lock_path", test_lock_path)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(CodexChromeExecutor, "__init__", init_with_test_lock)
