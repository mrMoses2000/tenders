from __future__ import annotations

import pytest


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
