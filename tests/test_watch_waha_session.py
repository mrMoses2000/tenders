from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import watch_waha_session as watch


def _settings(base_url: str = "http://127.0.0.1:3000") -> SimpleNamespace:
    return SimpleNamespace(waha_base_url=base_url, waha_session="default")


def test_watchdog_restarts_failed_session_once_per_cooldown(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_request(_settings, method, suffix):
        calls.append((method, suffix))
        return {"status": "FAILED" if method == "GET" else "STARTING"}

    monkeypatch.setattr(watch, "_request", fake_request)
    state = tmp_path / "state.json"
    assert watch.watch_once(_settings(), state_file=state, now=1000) == "RESTART_REQUESTED"
    assert watch.watch_once(_settings(), state_file=state, now=1100) == "FAILED_COOLDOWN"
    assert calls == [
        ("GET", "/api/sessions/default"),
        ("POST", "/api/sessions/default/restart"),
        ("GET", "/api/sessions/default"),
    ]
    assert state.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("status", ["WORKING", "STARTING", "SCAN_QR_CODE", "STOPPED"])
def test_watchdog_leaves_nonfailed_states_untouched(monkeypatch, tmp_path, status) -> None:
    calls = []

    def fake_request(_settings, method, suffix):
        calls.append((method, suffix))
        return {"status": status}

    monkeypatch.setattr(watch, "_request", fake_request)
    assert watch.watch_once(_settings(), state_file=tmp_path / "state.json") == status
    assert calls == [("GET", "/api/sessions/default")]


def test_watchdog_api_key_cannot_leave_loopback() -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        watch._local_base_url(_settings("http://remote.example"))
