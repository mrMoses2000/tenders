"""Run a bounded, synthetic-only supplier media provider smoke test.

This script never reads the database or contacts suppliers. It makes direct provider
calls to the configured native image reader and AssemblyAI using generated files only.
It does not print provider CLI output, credentials, transcript text, or local paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from procurement_bot.config import get_settings
from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.assemblyai import AssemblyAITranscriber
from procurement_bot.providers.supplier_audio import KazakhAudioTranscriber
from procurement_bot.providers.supplier_image import CodexSupplierImageReader
from procurement_bot.transcription import transcript_review_reason

POLL_BUDGET_SECONDS = 120
SUBMIT_BUDGET_SECONDS = 45
POLL_REQUEST_BUDGET_SECONDS = 20
POLL_INTERVAL_SECONDS = 3
SYNTHETIC_RUSSIAN_TEXT = "Цена 1250 тенге за мешок."
IMAGE_LABEL_LINES = (
    "Этикетка: ТЕСТОВЫЙ ОБРАЗЕЦ",
    "Клей плиточный",
    "Цена: 1250 тенге за мешок",
    "Вес: 25 кг",
    "Артикул: RU-2571",
)
EXPECTED_IMAGE_TOKENS = ("1250", "25", "кг")
EXPECTED_AUDIO_TOKENS = ("тенге", "мешок")


def _expected_price_present(text: str) -> bool:
    return bool(re.search(r"(?<!\d)1\s?250(?!\d)|тысяч[ауы]\s+двести\s+пятьдесят", text))


def _font_file() -> Path | None:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    )
    return next((path for path in candidates if path.is_file()), None)


def _make_synthetic_png(destination: Path) -> str:
    """Create a Cyrillic label using installed PyMuPDF and a system font."""

    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("pymupdf_unavailable") from exc
    font_file = _font_file()
    if font_file is None:
        raise RuntimeError("cyrillic_font_unavailable")

    document = fitz.open()
    try:
        page = document.new_page(width=900, height=620)
        page.draw_rect(fitz.Rect(20, 20, 880, 600), color=(0.15, 0.2, 0.25), width=3)
        page.insert_font(fontname="ProbeSans", fontfile=str(font_file))
        for index, line in enumerate(IMAGE_LABEL_LINES):
            page.insert_text(
                (60, 105 + 90 * index),
                line,
                fontname="ProbeSans",
                fontsize=31 if index == 0 else 28,
                color=(0.05, 0.1, 0.15),
            )
        page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(destination)
        destination.chmod(0o600)
    finally:
        document.close()
    return f"pymupdf:{fitz.VersionBind}"


async def _make_synthetic_wav(destination: Path) -> tuple[str | None, str | None]:
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if executable is None:
        return None, "tts_unavailable"
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-v",
            "ru",
            "-s",
            "140",
            "-w",
            str(destination),
            SYNTHETIC_RUSSIAN_TEXT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=20)
        except TimeoutError:
            process.kill()
            await process.wait()
            return Path(executable).name, "tts_timeout"
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
    except TimeoutError:
        return Path(executable).name, "tts_timeout"
    except OSError:
        return Path(executable).name, "tts_failed"
    if returncode != 0 or not destination.is_file() or destination.stat().st_size < 44:
        return Path(executable).name, "tts_failed"
    destination.chmod(0o600)
    return Path(executable).name, None


async def _run_image_probe(settings, image_path: Path) -> dict[str, Any]:
    executable = settings.codex_2gis_executable
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        return {"state": "missing_executable", "provider": "codex-image"}
    if settings.codex_2gis_model != "gpt-6-luna":
        return {
            "state": "unexpected_model",
            "provider": "codex-image",
            "model_matches_expected": False,
        }
    reader = CodexSupplierImageReader(
        executable,
        model=settings.codex_2gis_model,
        timeout_seconds=settings.supplier_image_timeout_seconds,
    )
    try:
        reading = await reader.read(image_path)
    except (PermanentProviderError, RetryableProviderError, TimeoutError, OSError) as exc:
        return {"state": "provider_error", "error_type": type(exc).__name__}
    normalized = reading.readable_text.casefold()
    token_matches = {token: token.casefold() in normalized for token in EXPECTED_IMAGE_TOKENS}
    return {
        "state": "completed",
        "provider": "codex-image",
        "model": settings.codex_2gis_model,
        "reasoning_effort": "high",
        "language": reading.language,
        "confidence": reading.confidence,
        "uncertain_numbers": reading.uncertain_numbers,
        "expected_token_matches": token_matches,
        "text_match": all(token_matches.values()),
    }


async def _run_assembly_probe(settings, audio_path: Path) -> dict[str, Any]:
    try:
        api_key = settings.require_assemblyai_api_key()
    except (OSError, ValueError) as exc:
        return {"state": "configuration_error", "error_type": type(exc).__name__}

    transcriber = AssemblyAITranscriber(
        api_key,
        timeout_seconds=max(30, min(settings.assemblyai_timeout_seconds, POLL_BUDGET_SECONDS)),
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )
    started = time.monotonic()
    submit_attempted = True
    try:
        transcript_id = await asyncio.wait_for(
            transcriber.submit(audio_path, language="auto"), timeout=SUBMIT_BUDGET_SECONDS
        )
    except TimeoutError:
        return {
            "state": "submit_timeout",
            "provider": "assemblyai",
            "submit_attempted": submit_attempted,
            "polls": 0,
        }
    except (PermanentProviderError, RetryableProviderError, OSError) as exc:
        return {
            "state": "submit_error",
            "provider": "assemblyai",
            "submit_attempted": submit_attempted,
            "error_type": type(exc).__name__,
            "polls": 0,
        }

    polls = 0
    deadline = started + POLL_BUDGET_SECONDS
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            transcript = await asyncio.wait_for(
                transcriber.poll(transcript_id),
                timeout=min(POLL_REQUEST_BUDGET_SECONDS, remaining),
            )
        except TimeoutError:
            return {
                "state": "poll_timeout",
                "provider": "assemblyai",
                "submit_attempted": submit_attempted,
                "polls": polls,
            }
        except (PermanentProviderError, RetryableProviderError, OSError) as exc:
            return {
                "state": "poll_error",
                "provider": "assemblyai",
                "submit_attempted": submit_attempted,
                "error_type": type(exc).__name__,
                "polls": polls + 1,
            }
        polls += 1
        if transcript is not None:
            normalized = transcript.text.casefold()
            matches = {token: token.casefold() in normalized for token in EXPECTED_AUDIO_TOKENS}
            number_match = _expected_price_present(normalized)
            review_reason = transcript_review_reason(
                transcript,
                minimum_language_confidence=settings.transcription_min_language_confidence,
            )
            return {
                "state": "completed",
                "provider": "assemblyai",
                "submit_attempted": submit_attempted,
                "polls": polls,
                "language": transcript.language,
                "language_confidence": transcript.confidence,
                "speech_confidence": transcript.speech_confidence,
                "expected_token_matches": matches,
                "number_match": number_match,
                "text_match": all(matches.values()) and number_match,
                "requires_clarification": bool(
                    review_reason
                    or transcript.speech_confidence is None
                    or transcript.speech_confidence < 0.75
                    or transcript.low_confidence_words
                ),
            }
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    return {
        "state": "poll_budget_elapsed",
        "provider": "assemblyai",
        "submit_attempted": submit_attempted,
        "polls": polls,
    }


async def _run_agy_probe(settings, audio_path: Path) -> dict[str, Any]:
    executable = shutil.which(settings.agy_executable)
    if executable is None:
        return {"state": "missing_executable", "provider": "agy"}
    transcriber = KazakhAudioTranscriber(
        executable=executable,
        model=settings.agy_model,
        timeout_seconds=max(30, min(settings.agy_timeout_seconds, POLL_BUDGET_SECONDS)),
        home_dir=settings.agy_home,
    )
    try:
        transcript = await asyncio.wait_for(
            transcriber.transcribe(audio_path, language="auto"),
            timeout=POLL_BUDGET_SECONDS,
        )
    except TimeoutError:
        return {"state": "timeout", "provider": "agy"}
    except (PermanentProviderError, RetryableProviderError, OSError) as exc:
        return {"state": "provider_error", "provider": "agy", "error_type": type(exc).__name__}
    normalized = transcript.text.casefold()
    matches = {token: token.casefold() in normalized for token in EXPECTED_AUDIO_TOKENS}
    number_match = _expected_price_present(normalized)
    return {
        "state": "completed",
        "provider": "agy",
        "language": transcript.language,
        "expected_token_matches": matches,
        "number_match": number_match,
        "text_match": all(matches.values()) and number_match,
        # A Russian synthetic sample checks attachment transport, not Kazakh accuracy.
        "kazakh_accuracy_verified": False,
        "requires_clarification": bool(
            transcript_review_reason(
                transcript,
                minimum_language_confidence=settings.transcription_min_language_confidence,
            )
            or transcript.speech_confidence is None
            or transcript.speech_confidence < 0.75
            or transcript.low_confidence_words
        ),
    }


async def run_probe(*, agy_audio: bool = False) -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {
        "operation": "synthetic_supplier_media_provider_probe",
        "database_access": False,
        "waha_access": False,
        "whatsapp_messages_sent": 0,
        "image": {"state": "not_run"},
        "assemblyai": {"state": "not_run", "provider": "assemblyai"},
    }
    if agy_audio:
        result["agy"] = {"state": "not_run", "provider": "agy"}

    with tempfile.TemporaryDirectory(prefix="supplier-media-probe-") as temp:
        root = Path(temp)
        image_path = root / "synthetic-russian-label.png"
        try:
            result["synthetic_image_generator"] = _make_synthetic_png(image_path)
            result["image"] = await _run_image_probe(settings, image_path)
        except (RuntimeError, OSError) as exc:
            result["image"] = {"state": "input_error", "error_type": type(exc).__name__}

        wav_path = root / "synthetic-russian-price.wav"
        tts_name, tts_error = await _make_synthetic_wav(wav_path)
        result["tts"] = {"state": tts_error or "ready", "engine": tts_name}
        if tts_error:
            result["assemblyai"] = {"state": "skipped_no_synthetic_audio", "provider": "assemblyai"}
            if agy_audio:
                result["agy"] = {"state": "skipped_no_synthetic_audio", "provider": "agy"}
        else:
            result["assemblyai"] = await _run_assembly_probe(settings, wav_path)
            if agy_audio:
                result["agy"] = await _run_agy_probe(settings, wav_path)
    result["temporary_files_cleaned"] = not root.exists()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agy-audio",
        action="store_true",
        help="also check AGY's audio attachment path using the same synthetic Russian WAV",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(run_probe(agy_audio=args.agy_audio))
    except Exception as exc:  # Never include exception messages or provider output.
        result = {
            "operation": "synthetic_supplier_media_provider_probe",
            "state": "failed",
            "error_type": type(exc).__name__,
            "database_access": False,
            "waha_access": False,
            "whatsapp_messages_sent": 0,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
