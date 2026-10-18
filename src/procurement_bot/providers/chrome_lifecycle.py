from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from procurement_bot.errors import RetryableProviderError

_SCRIPT_NAMES = ("chrome-is-running.js", "open-chrome-window.js")
_MAX_OUTPUT_BYTES = 16_384


@dataclass
class ChromeLifecycleCounters:
    cleanup_requested: int = 0
    recovery_attempted: int = 0
    launcher_started: int = 0


class ChromeLifecycle:
    """Use only the installed Chrome plugin's supported presence and launch scripts."""

    def __init__(
        self,
        plugin_root: Path | None,
        *,
        node_executable: Path | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("Chrome lifecycle timeout must be between 1 and 60 seconds")
        self.timeout_seconds = timeout_seconds
        self.node_executable = Path(node_executable) if node_executable is not None else None
        if self.node_executable is not None and (
            not self.node_executable.is_absolute()
            or not self.node_executable.is_file()
            or not os.access(self.node_executable, os.X_OK)
        ):
            raise ValueError("Chrome lifecycle Node executable must be absolute and executable")
        self.plugin_root: Path | None = None
        self.check_script: Path | None = None
        self.launch_script: Path | None = None
        if plugin_root is None:
            return
        root = Path(plugin_root)
        if not root.is_absolute():
            raise ValueError("Chrome plugin root must be an absolute path")
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Chrome plugin root does not exist") from exc
        if not resolved_root.is_dir():
            raise ValueError("Chrome plugin root must be a directory")
        paths = [resolved_root / "scripts" / name for name in _SCRIPT_NAMES]
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise ValueError("Chrome plugin lifecycle scripts are missing") from exc
            if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
                raise ValueError("Chrome plugin lifecycle script path is invalid")
        self.plugin_root = resolved_root
        self.check_script, self.launch_script = paths

    @property
    def enabled(self) -> bool:
        return self.plugin_root is not None

    async def ensure_running(self, counters: ChromeLifecycleCounters) -> None:
        """Preflight Chrome and launch the selected profile only when it is absent."""

        if not self.enabled:
            return
        running = await self._is_running()
        if not running:
            counters.recovery_attempted += 1
            await self._launch(counters)

    async def recover_after_disconnect(self, counters: ChromeLifecycleCounters) -> None:
        """Perform one bounded recovery after a classified bridge disconnect."""

        if not self.enabled:
            return
        counters.recovery_attempted += 1
        if not await self._is_running():
            await self._launch(counters)

    async def _is_running(self) -> bool:
        assert self.check_script is not None
        output, returncode = await self._run(
            self.check_script,
            ("--browser", "chrome", "--check", "--json"),
            allowed_returncodes={0, 1},
        )
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RetryableProviderError("Chrome lifecycle check returned invalid data") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("running"), bool):
            raise RetryableProviderError("Chrome lifecycle check returned invalid data")
        if returncode == 0 and payload["running"]:
            return True
        if returncode == 1 and not payload["running"]:
            return False
        raise RetryableProviderError("Chrome lifecycle check failed")

    async def _launch(self, counters: ChromeLifecycleCounters) -> None:
        assert self.launch_script is not None
        counters.launcher_started += 1
        output, _ = await self._run(
            self.launch_script,
            ("--browser", "chrome", "--json"),
            allowed_returncodes={0},
        )
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RetryableProviderError("Chrome lifecycle launcher returned invalid data") from exc
        if not isinstance(payload, dict) or payload.get("dryRun") is not False:
            raise RetryableProviderError("Chrome lifecycle launcher did not open Chrome")

    async def _run(
        self,
        script: Path,
        arguments: tuple[str, ...],
        *,
        allowed_returncodes: set[int],
    ) -> tuple[str, int]:
        node = (
            str(self.node_executable)
            if self.node_executable is not None
            else shutil.which("node")
        )
        if node is None:
            raise RetryableProviderError("Chrome lifecycle runtime is unavailable")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                node,
                str(script),
                *arguments,
                cwd=self.plugin_root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdout is not None
            output_task = asyncio.create_task(self._read_bounded(process.stdout))
            stdout = await asyncio.wait_for(output_task, timeout=self.timeout_seconds)
            await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(self._stop(process))
            raise
        except (OSError, TimeoutError, ValueError) as exc:
            if process is not None:
                await asyncio.shield(self._stop(process))
            if isinstance(exc, ValueError):
                raise RetryableProviderError(
                    "Chrome lifecycle command returned oversized data"
                ) from exc
            raise RetryableProviderError("Chrome lifecycle command failed") from exc
        if process.returncode not in allowed_returncodes:
            raise RetryableProviderError("Chrome lifecycle command failed")
        if len(stdout) > _MAX_OUTPUT_BYTES:
            raise RetryableProviderError("Chrome lifecycle command returned oversized data")
        return stdout.decode("utf-8", errors="strict"), int(process.returncode or 0)

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
        output = bytearray()
        while chunk := await stream.read(4_096):
            output.extend(chunk)
            if len(output) > _MAX_OUTPUT_BYTES:
                raise ValueError("oversized lifecycle output")
        return bytes(output)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
