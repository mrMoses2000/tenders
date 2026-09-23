from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Update
from aiohttp import web

from procurement_bot.config import Settings, get_settings
from procurement_bot.db import create_pool, run_migrations
from procurement_bot.dialogue import SearchScopePolicy
from procurement_bot.health import health_snapshot
from procurement_bot.intake import IntakeService
from procurement_bot.logging import configure_logging
from procurement_bot.outbox import TelegramOutboxWorker, WhatsAppOutboxWorker
from procurement_bot.providers.agy import AgyExtractor
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.assemblyai import AssemblyAITranscriber
from procurement_bot.providers.browser import StructuredBrowserProvider
from procurement_bot.providers.waha import WahaClient
from procurement_bot.research_execution import ResearchRunExecutor
from procurement_bot.storage import LocalContentAddressedStorage
from procurement_bot.telegram import TelegramIngress
from procurement_bot.transcription import DisabledTranscriber
from procurement_bot.waha_webhook_http import create_waha_webhook_app
from procurement_bot.worker import JobWorker

ROOT = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger(__name__)


class _PollingStopped(Exception):
    """Terminate worker siblings after aiogram handles SIGTERM normally."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="procurement-bot")
    parser.add_argument(
        "command",
        choices=("migrate", "health", "bot", "worker", "webhook", "run"),
        help="run migrations, inspect health, or start a process",
    )
    return parser


def _make_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_base_url:
        api = TelegramAPIServer.from_base(settings.telegram_api_base_url, is_local=True)
        session = AiohttpSession(api=api)
    return Bot(token=settings.require_telegram_token(), session=session)


async def _migrate(settings: Settings) -> None:
    pool = await create_pool(settings.postgres_dsn)
    try:
        applied = await run_migrations(pool, ROOT / "migrations")
        print(json.dumps({"applied": applied}, ensure_ascii=False))
    finally:
        await pool.close()


async def _health(settings: Settings) -> None:
    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=2)
    try:
        snapshot = await health_snapshot(pool)
        print(json.dumps(snapshot, ensure_ascii=False, default=str))
    finally:
        await pool.close()


async def _run_bot(settings: Settings, *, with_workers: bool) -> None:
    settings.prepare_directories()
    pool = await create_pool(
        settings.postgres_dsn,
        max_size=max(10, settings.worker_concurrency + 4),
    )
    bot = _make_bot(settings)
    dispatcher = Dispatcher()
    allowed_user_ids, bootstrap_usernames = settings.telegram_access_config()
    ingress = TelegramIngress(
        pool=pool,
        bot=bot,
        max_concurrency=settings.telegram_ingress_concurrency,
        allowed_user_ids=allowed_user_ids,
        bootstrap_usernames=bootstrap_usernames,
    )

    async def accept_update(_handler, update: Update, _data: dict[str, object]) -> bool:
        delay = 0.5
        while True:
            try:
                await ingress.accept(update)
                # The root middleware owns every update.  Do not call aiogram's
                # inner dispatcher after the durable ingress has handled it.
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                # Telegram getUpdates advances its offset after the handler
                # returns. Keep this handler pending until ingress is durable.
                LOGGER.exception("Telegram ingress failed; retrying before acknowledgement")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    # Dispatcher.update already contains aiogram's internal _listen_update
    # handler, which always matches first.  A second root handler is therefore
    # unreachable; the outer middleware is the supported interception point.
    dispatcher.update.outer_middleware.register(accept_update)

    async def poll_until_stopped() -> None:
        await dispatcher.start_polling(
            bot,
            polling_timeout=settings.poll_timeout_seconds,
            handle_as_tasks=False,
        )
        # A TaskGroup waits forever when one child returns normally while its
        # worker siblings are infinite.  The sentinel cancels those siblings.
        raise _PollingStopped

    try:
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(poll_until_stopped())
                if with_workers:
                    _start_worker_tasks(tasks, settings, pool, bot)
                    if settings.waha_webhook_enabled:
                        process = f"{socket.gethostname()}:{os.getpid()}"
                        tasks.create_task(
                            _serve_waha_webhook(
                                settings,
                                pool,
                                worker_id=f"{process}:waha-webhook",
                            )
                        )
        except* _PollingStopped:
            pass
    finally:
        await bot.session.close()
        await pool.close()


async def _run_workers(settings: Settings) -> None:
    settings.prepare_directories()
    pool = await create_pool(
        settings.postgres_dsn,
        max_size=max(10, settings.worker_concurrency + 4),
    )
    bot = _make_bot(settings)
    try:
        async with asyncio.TaskGroup() as tasks:
            _start_worker_tasks(tasks, settings, pool, bot)
    finally:
        await bot.session.close()
        await pool.close()


async def _serve_waha_webhook(
    settings: Settings,
    pool,
    *,
    worker_id: str,
) -> None:
    app = create_waha_webhook_app(
        pool=pool,
        shared_secret=settings.require_waha_webhook_secret(),
        worker_id=worker_id,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.waha_webhook_bind_host,
        port=settings.waha_webhook_port,
    )
    try:
        await site.start()
        LOGGER.info(
            "WAHA webhook ingress listening",
            extra={
                "host": settings.waha_webhook_bind_host,
                "port": settings.waha_webhook_port,
            },
        )
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def _run_webhook(settings: Settings) -> None:
    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=5)
    process = f"{socket.gethostname()}:{os.getpid()}"
    try:
        await _serve_waha_webhook(settings, pool, worker_id=f"{process}:waha-webhook")
    finally:
        await pool.close()


def _start_worker_tasks(
    tasks: asyncio.TaskGroup,
    settings: Settings,
    pool,
    bot: Bot,
) -> None:
    storage = LocalContentAddressedStorage(settings.media_root)
    agy_home = settings.prepare_isolated_agy_home()
    extractor = AgyExtractor(
        executable=settings.agy_executable,
        timeout_seconds=settings.agy_timeout_seconds,
        model=settings.agy_model,
        concurrency=settings.agy_concurrency,
        home_dir=agy_home,
    )
    intake = IntakeService(extractor, scope_policy=SearchScopePolicy.require_explicit())
    research_executor = _build_research_executor(settings, pool)
    if settings.voice_provider == "assemblyai":
        transcriber = AssemblyAITranscriber(
            settings.require_assemblyai_api_key(),
            timeout_seconds=settings.assemblyai_timeout_seconds,
        )
    else:
        transcriber = DisabledTranscriber()
    process = f"{socket.gethostname()}:{os.getpid()}"
    download_limit = settings.max_source_file_bytes
    if not settings.telegram_api_base_url:
        download_limit = min(download_limit, settings.telegram_cloud_download_limit_bytes)
    for index in range(settings.worker_concurrency):
        worker = JobWorker(
            pool=pool,
            bot=bot,
            storage=storage,
            transcriber=transcriber,
            intake=intake,
            worker_id=f"{process}:job:{index}",
            lease_seconds=settings.job_lease_seconds,
            max_file_bytes=download_limit,
            max_audio_duration_seconds=settings.max_audio_duration_seconds,
            research_executor=research_executor,
            locality_browser=(research_executor.browser if research_executor else None),
            results_root=settings.results_root,
            whatsapp_enabled=settings.waha_enabled,
            transcription_min_language_confidence=(
                settings.transcription_min_language_confidence
            ),
            waha_session=settings.waha_session,
        )
        tasks.create_task(worker.run_forever())
    outbox = TelegramOutboxWorker(
        pool=pool,
        bot=bot,
        worker_id=f"{process}:outbox",
        results_root=settings.results_root,
    )
    tasks.create_task(outbox.run_forever())
    if settings.waha_enabled:
        waha = WahaClient(
            base_url=settings.waha_base_url,
            api_key=settings.require_waha_api_key(),
            session=settings.waha_session,
            enabled=True,
            timeout_seconds=settings.waha_timeout_seconds,
        )
        whatsapp_outbox = WhatsAppOutboxWorker(
            pool=pool,
            client=waha,
            worker_id=f"{process}:whatsapp-outbox",
        )
        tasks.create_task(whatsapp_outbox.run_forever())


def _build_research_executor(
    settings: Settings,
    pool,
) -> ResearchRunExecutor | None:
    if not settings.browser_research_enabled:
        return None
    browser_home, server = settings.prepare_isolated_agy_browser_home()
    executable = Path(settings.agy_executable).expanduser()
    if not executable.is_absolute():
        resolved = shutil.which(settings.agy_executable)
        if resolved is None:
            raise ValueError(f"AGY executable was not found: {settings.agy_executable}")
        executable = Path(resolved)
    runtime_environment = {
        name: os.environ[name]
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        if name in os.environ
    }
    transport = AgyBrowserMcpExecutor(
        executable=executable.resolve(),
        home_dir=browser_home,
        mcp_server_name=settings.agy_browser_mcp_server_name.strip(),
        expected_mcp_server=server,
        model=settings.agy_model,
        timeout_seconds=settings.agy_browser_timeout_seconds,
        concurrency=settings.agy_browser_concurrency,
        runtime_environment=runtime_environment,
    )
    return ResearchRunExecutor(pool, StructuredBrowserProvider(transport))


async def _async_main(command: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if command == "migrate":
        await _migrate(settings)
    elif command == "health":
        await _health(settings)
    elif command == "bot":
        await _run_bot(settings, with_workers=False)
    elif command == "worker":
        await _run_workers(settings)
    elif command == "webhook":
        await _run_webhook(settings)
    else:
        await _run_bot(settings, with_workers=True)


def main() -> None:
    arguments = build_parser().parse_args()
    asyncio.run(_async_main(arguments.command))


if __name__ == "__main__":
    main()
