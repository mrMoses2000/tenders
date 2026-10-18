from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import procurement_bot.providers.supplier_audio as supplier_audio
from procurement_bot.errors import PermanentProviderError
from procurement_bot.providers.supplier_audio import KazakhAudioTranscriber


class _Stdin:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _Process:
    def __init__(self, stdout: bytes) -> None:
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = 0
        self.pid = 123

    async def wait(self) -> int:
        return 0


def _stub_audio_process(
    provider: KazakhAudioTranscriber,
    process: _Process,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider._profile,
        "_prepare_ephemeral_home",
        lambda root: root / "runtime-home",
    )
    monkeypatch.setattr(
        provider._profile,
        "_environment",
        lambda runtime_home: {"HOME": str(runtime_home)},
    )

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(supplier_audio.asyncio, "create_subprocess_exec", fake_create)


async def test_transcribes_kazakh_with_staged_audio_and_no_invented_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "supplier.ogg"
    original.write_bytes(b"voice")
    process = _Process("Бағасы бір мың теңге.\n".encode())
    captured: dict[str, object] = {}
    provider = KazakhAudioTranscriber(home_dir=tmp_path / "isolated-home")
    monkeypatch.setattr(
        provider._profile,
        "_prepare_ephemeral_home",
        lambda root: root / "runtime-home",
    )
    monkeypatch.setattr(
        provider._profile,
        "_environment",
        lambda runtime_home: {"HOME": str(runtime_home)},
    )

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        staged = Path(kwargs["cwd"]) / "supplier-audio.ogg"
        captured["staged_contents"] = staged.read_bytes()
        return process

    monkeypatch.setattr(supplier_audio.asyncio, "create_subprocess_exec", fake_create)
    transcript = await provider.transcribe(original)

    assert transcript.text == "Бағасы бір мың теңге."
    assert transcript.language == "kk"
    assert transcript.confidence is None
    assert transcript.speech_confidence is None
    assert transcript.low_confidence_words == ()
    assert captured["staged_contents"] == b"voice"
    command = captured["args"]
    assert isinstance(command, tuple)
    assert "--sandbox" in command
    assert "--new-project" in command
    assert original.as_posix() not in process.stdin.data.decode()
    assert str(captured["cwd"]) in process.stdin.data.decode()


async def test_kazakh_provider_rejects_other_languages_and_missing_audio(tmp_path: Path) -> None:
    provider = KazakhAudioTranscriber(home_dir=tmp_path / "isolated-home")
    with pytest.raises(PermanentProviderError, match="supports kk or auto"):
        await provider.transcribe(tmp_path / "missing.ogg", language="ru")
    with pytest.raises(PermanentProviderError, match="missing or empty"):
        await provider.transcribe(tmp_path / "missing.ogg")


async def test_auto_kazakh_fallback_preserves_detected_language_without_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "supplier.ogg"
    audio.write_bytes(b"voice")
    process = _Process('{"language":"ru","text":"Стоимость 500 тенге."}'.encode())
    provider = KazakhAudioTranscriber(home_dir=tmp_path / "isolated-home")
    monkeypatch.setattr(
        provider._profile,
        "_prepare_ephemeral_home",
        lambda root: root / "runtime-home",
    )
    monkeypatch.setattr(
        provider._profile,
        "_environment",
        lambda runtime_home: {"HOME": str(runtime_home)},
    )

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(supplier_audio.asyncio, "create_subprocess_exec", fake_create)
    transcript = await provider.transcribe(audio, language="auto")

    assert transcript.language == "ru"
    assert transcript.text == "Стоимость 500 тенге."
    assert transcript.confidence is None
    assert transcript.speech_confidence is None


@pytest.mark.parametrize("language", ["auto", "kk"])
async def test_rejects_known_provider_filter_response_even_when_cli_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    audio = tmp_path / "supplier.ogg"
    audio.write_bytes(b"voice")
    process = _Process(
        (
            "THIS REQUEST WAS BLOCKED BY GEMINI’S FILTERS. "
            "They can occasionally trigger by mistake."
        ).encode()
    )
    provider = KazakhAudioTranscriber(home_dir=tmp_path / "isolated-home")
    _stub_audio_process(provider, process, monkeypatch)

    with pytest.raises(PermanentProviderError, match="supplier_audio_provider_filtered"):
        await provider.transcribe(audio, language=language)


async def test_auto_rejects_malformed_json_instead_of_returning_it_as_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "supplier.ogg"
    audio.write_bytes(b"voice")
    process = _Process(b"AGY returned a wrapper message instead of the requested JSON.")
    provider = KazakhAudioTranscriber(home_dir=tmp_path / "isolated-home")
    _stub_audio_process(provider, process, monkeypatch)

    with pytest.raises(PermanentProviderError, match="supplier_audio_result_invalid"):
        await provider.transcribe(audio, language="auto")


@pytest.mark.parametrize(
    "payload",
    [
        '{"language":"en","text":"Price 500."}',
        '{"language":"ru","text":"  "}',
        '{"language":[],"text":"Цена 500."}',
        '{"text":"Цена 500."}',
    ],
)
async def test_auto_rejects_invalid_json_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    audio = tmp_path / "supplier.ogg"
    audio.write_bytes(b"voice")
    process = _Process(payload.encode())
    provider = KazakhAudioTranscriber(home_dir=tmp_path / "isolated-home")
    _stub_audio_process(provider, process, monkeypatch)

    with pytest.raises(PermanentProviderError, match="supplier_audio_result_invalid"):
        await provider.transcribe(audio, language="auto")
