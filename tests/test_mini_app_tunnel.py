from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_mini_app_tunnel.py"
spec = importlib.util.spec_from_file_location("run_mini_app_tunnel", SCRIPT)
assert spec is not None and spec.loader is not None
run_mini_app_tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_mini_app_tunnel)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True


class ImmediateEvent:
    def wait(self, _seconds: int) -> bool:
        return False


def test_monitor_restarts_after_three_consecutive_failures(monkeypatch) -> None:
    process = FakeProcess()
    results = iter((False, True, False, False, False))
    monkeypatch.setattr(run_mini_app_tunnel, "check_public_health", lambda _url: next(results))

    run_mini_app_tunnel.monitor_health(  # type: ignore[arg-type]
        process, "https://example.trycloudflare.com", ImmediateEvent(),  # type: ignore[arg-type]
    )

    assert process.terminated


def test_monitor_stays_running_while_health_is_good(monkeypatch) -> None:
    process = FakeProcess()
    checks = 0

    def healthy(_url: str) -> bool:
        nonlocal checks
        checks += 1
        return True

    class FiniteEvent:
        def wait(self, _seconds: int) -> bool:
            return checks == 3

    monkeypatch.setattr(run_mini_app_tunnel, "check_public_health", healthy)

    run_mini_app_tunnel.monitor_health(  # type: ignore[arg-type]
        process, "https://example.trycloudflare.com", FiniteEvent(),  # type: ignore[arg-type]
    )

    assert checks == 3
    assert not process.terminated


def test_public_health_uses_working_https_before_direct_dns(monkeypatch) -> None:
    monkeypatch.setattr(run_mini_app_tunnel, "_curl_health", lambda _url: True)

    def no_direct_dns(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("direct DNS should not be called")

    monkeypatch.setattr(run_mini_app_tunnel.subprocess, "run", no_direct_dns)

    assert run_mini_app_tunnel.check_public_health(
        "https://example.trycloudflare.com"
    )


def test_curl_health_requires_exact_json_status(monkeypatch) -> None:
    monkeypatch.setattr(
        run_mini_app_tunnel.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"status":"error"}', stderr=""
        ),
    )

    assert not run_mini_app_tunnel._curl_health(
        "https://example.trycloudflare.com/api/health"
    )
