from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

import procurement_bot.providers.supplier_image as image_module
from procurement_bot.errors import PermanentProviderError
from procurement_bot.providers.supplier_image import CodexSupplierImageReader


def _fake_codex(path: Path, *, result: str = "valid") -> tuple[Path, Path]:
    capture = path.parent / "captured-argv.json"
    script = f"""#!{sys.executable}
import json
import pathlib
import sys
args=sys.argv[1:]
capture=pathlib.Path({str(capture)!r})
capture.write_text(json.dumps(args))
root=pathlib.Path(args[args.index('-C') + 1])
schema=pathlib.Path(args[args.index('--output-schema') + 1])
result=pathlib.Path(args[args.index('-o') + 1])
capture.with_suffix('.details.json').write_text(json.dumps({{
    'root_mode': root.stat().st_mode & 0o777,
    'schema_in_private_root': schema.parent == root,
    'result_in_private_root': result.parent == root,
    'schema_exists': schema.is_file(),
}}))
payload={
        json.dumps(
            {
                "readable_text": "Лак 500 тг",
                "observations": "Белая этикетка",
                "language": "ru",
                "confidence": 0.97,
                "uncertain_numbers": False,
            }
        )
        if result == "valid"
        else json.dumps(
            {
                "readable_text": "x",
                "observations": "",
                "language": "en",
                "confidence": 2,
                "uncertain_numbers": False,
            }
        )
        if result == "invalid"
        else result!r
    }
result.write_text(payload)
"""
    path.write_text(script)
    path.chmod(0o700)
    return capture, capture.with_suffix(".details.json")


@pytest.mark.asyncio
async def test_native_image_reader_uses_private_ephemeral_no_browser_cli(tmp_path: Path) -> None:
    executable = tmp_path / "codex-image-reader"
    capture, details = _fake_codex(executable)
    photo = tmp_path / "supplier-photo.jpg"
    photo.write_bytes(b"private image bytes")
    reader = CodexSupplierImageReader(executable, timeout_seconds=3)

    reading = await reader.read(photo)

    args = json.loads(capture.read_text())
    process_details = json.loads(details.read_text())
    assert reading.readable_text == "Лак 500 тг"
    assert "--ignore-user-config" in args
    assert "--ephemeral" in args
    assert "-s" in args and args[args.index("-s") + 1] == "read-only"
    assert "--image" in args and str(photo.resolve()) in args
    assert process_details == {
        "root_mode": 0o700,
        "schema_in_private_root": True,
        "result_in_private_root": True,
        "schema_exists": True,
    }
    assert not any(token.lower() in {"--browser", "--chrome", "--mcp"} for token in args)
    private_root = Path(args[args.index("-C") + 1])
    assert not private_root.exists()


@pytest.mark.asyncio
async def test_native_image_reader_rejects_invalid_schema_output(tmp_path: Path) -> None:
    executable = tmp_path / "codex-image-reader"
    _fake_codex(executable, result="invalid")
    reader = CodexSupplierImageReader(executable, timeout_seconds=3)

    with pytest.raises(PermanentProviderError, match="result_invalid"):
        await reader.read(tmp_path / "photo.jpg")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True], ids=["timeout", "cancelled"])
async def test_image_reader_terminates_process_group_on_timeout_or_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cancel_task: bool,
) -> None:
    started = asyncio.Event()
    kill_calls: list[tuple[int, int]] = []

    class HangingProcess:
        pid = 82173
        returncode: int | None = None

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Future()
            return b"", b""

        async def wait(self) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    process = HangingProcess()
    spawn_calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    async def fake_spawn(*args: object, **kwargs: Any) -> HangingProcess:
        spawn_calls.append((args, kwargs))
        return process

    monkeypatch.setattr(image_module.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(
        image_module.os, "killpg", lambda pid, signum: kill_calls.append((pid, signum))
    )
    executable = tmp_path / "unused-codex"
    executable.write_text("unused")
    reader = CodexSupplierImageReader(executable, timeout_seconds=0.01 if not cancel_task else 5)
    task = asyncio.create_task(reader.read(tmp_path / "photo.jpg"))
    await started.wait()

    if cancel_task:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            await task

    assert kill_calls == [(process.pid, signal.SIGTERM)]
    assert process.returncode == -signal.SIGTERM
    assert spawn_calls[0][1]["start_new_session"] is True


def test_image_reader_requires_absolute_executable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        CodexSupplierImageReader(Path("codex"))
