from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.agy import AgyExtractor
from procurement_bot.transcription import Transcript


class KazakhAudioTranscriber:
    """Verbatim Kazakh transcription through the isolated AGY CLI profile.

    This adapter follows the deployed audio-transcription service's proven
    attachment mechanism: the prompt names the absolute staged audio path and
    the CLI runs with that file's directory as its working project. The original
    media path is never exposed to the CLI.
    """

    MAX_OUTPUT_BYTES = 1_048_576
    FILTER_RESPONSE = "this request was blocked by gemini's filters"

    def __init__(
        self,
        executable: str = "agy",
        *,
        model: str = "gemini-3.8-flash-high",
        timeout_seconds: int = 240,
        home_dir: Path | None = None,
    ) -> None:
        if timeout_seconds < 30:
            raise ValueError("AGY transcription timeout must be at least 30 seconds")
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._profile = AgyExtractor(
            executable=executable,
            timeout_seconds=timeout_seconds,
            model=model,
            home_dir=home_dir,
        )
        self._semaphore = asyncio.Semaphore(1)

    async def transcribe(self, path: Path, language: str = "kk") -> Transcript:
        normalized = language.casefold().split("-", 1)[0]
        if normalized not in {"kk", "auto"}:
            raise PermanentProviderError("AGY supplier audio provider supports kk or auto")
        if not path.is_file() or path.stat().st_size <= 0:
            raise PermanentProviderError("audio file is missing or empty")
        async with self._semaphore:
            with tempfile.TemporaryDirectory(prefix="supplier-audio-agy-") as directory:
                root = Path(directory)
                runtime_home = self._profile._prepare_ephemeral_home(root)
                suffix = path.suffix if len(path.suffix) <= 12 else ".audio"
                audio_path = root / f"supplier-audio{suffix}"
                await asyncio.to_thread(shutil.copyfile, path, audio_path)
                audio_path.chmod(0o600)
                if normalized == "auto":
                    prompt = (
                        "Please accurately transcribe the speech in the audio file located at "
                        f"{audio_path.resolve()}. The source language is Russian or Kazakh. "
                        "Keep the transcript in its original language; do not translate, "
                        "summarize, explain, normalize, or guess unclear speech. If words or "
                        "numbers are unclear, mark them as [unclear]. Return exactly one JSON "
                        'object with keys "language" and "text". Set language to "ru", "kk", '
                        'or "unknown" only when the source language cannot be determined.'
                    )
                else:
                    prompt = (
                        "Please accurately transcribe the speech in the audio file located at "
                        f"{audio_path.resolve()}. Target language hint: Kazakh (kk). "
                        "Output strictly the verbatim spoken words in the original language. "
                        "Do not translate, summarize, explain, normalize, or guess unclear speech. "
                        "If any words or numbers are unclear, mark them as [unclear]."
                    )
                command = [
                    self.executable,
                    "--input-format",
                    "text",
                    "--model",
                    self.model,
                    "--output-format",
                    "text",
                    "--sandbox",
                    "--new-project",
                    "--disable-slash-commands",
                    "--print-timeout",
                    f"{max(1, self.timeout_seconds - 5)}s",
                ]
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=root,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=self._profile._environment(runtime_home),
                        start_new_session=True,
                        limit=65_536,
                    )
                except FileNotFoundError as exc:
                    raise PermanentProviderError("AGY CLI executable is unavailable") from exc
                assert process.stdin is not None
                assert process.stdout is not None and process.stderr is not None
                try:
                    _, stdout, _, _ = await asyncio.wait_for(
                        asyncio.gather(
                            self._profile._write_prompt(process.stdin, prompt),
                            self._read_bounded(process.stdout),
                            self._read_bounded(process.stderr),
                            process.wait(),
                        ),
                        timeout=self.timeout_seconds,
                    )
                except TimeoutError as exc:
                    await self._profile._terminate(process)
                    raise RetryableProviderError("AGY audio transcription timeout") from exc
                except _OutputLimitError as exc:
                    await self._profile._terminate(process)
                    raise PermanentProviderError("AGY transcription output exceeded 1 MiB") from exc
                except asyncio.CancelledError:
                    await self._profile._terminate(process)
                    raise
                if process.returncode != 0:
                    raise RetryableProviderError("AGY audio transcription failed")
                try:
                    output = stdout.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise RetryableProviderError("AGY returned non-UTF-8 transcript") from exc
                if output.startswith("```") and output.endswith("```"):
                    output = output.strip("`").removeprefix("json").removeprefix("text").strip()
                if self._is_provider_filtered(output):
                    raise PermanentProviderError("supplier_audio_provider_filtered")
                language_code = "kk"
                text = output
                if normalized == "auto":
                    try:
                        result = json.loads(output)
                    except json.JSONDecodeError:
                        raise PermanentProviderError("supplier_audio_result_invalid") from None
                    if not isinstance(result, dict):
                        raise PermanentProviderError("supplier_audio_result_invalid")
                    candidate_language = result.get("language")
                    candidate_text = result.get("text")
                    if (
                        not isinstance(candidate_language, str)
                        or candidate_language not in {"ru", "kk", "unknown"}
                        or not isinstance(candidate_text, str)
                        or not candidate_text.strip()
                    ):
                        raise PermanentProviderError("supplier_audio_result_invalid")
                    language_code = candidate_language
                    text = candidate_text.strip()
                if not text:
                    raise RetryableProviderError("AGY returned an empty transcript")
                return Transcript(
                    text=text,
                    language=language_code,
                    confidence=None,
                    provider=f"agy:{self.model}",
                )

    @classmethod
    def _is_provider_filtered(cls, output: str) -> bool:
        normalized = " ".join(output.casefold().replace("’", "'").split())
        return cls.FILTER_RESPONSE in normalized

    @classmethod
    async def _read_bounded(cls, stream: asyncio.StreamReader) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > cls.MAX_OUTPUT_BYTES:
                raise _OutputLimitError
            chunks.append(chunk)


class _OutputLimitError(Exception):
    pass
