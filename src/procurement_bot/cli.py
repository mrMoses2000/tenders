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
from aiogram.types import MenuButtonWebApp, Update, WebAppInfo
from aiohttp import web

from procurement_bot.config import Settings, get_settings
from procurement_bot.configured_extractor import ConfiguredAgyExtractor
from procurement_bot.db import create_pool, run_migrations
from procurement_bot.dialogue import SearchScopePolicy
from procurement_bot.health import health_snapshot
from procurement_bot.intake import IntakeService
from procurement_bot.logging import configure_logging
from procurement_bot.mini_app import create_mini_app, validate_public_url
from procurement_bot.outbox import TelegramOutboxWorker, WhatsAppOutboxWorker
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.assemblyai import AssemblyAITranscriber
from procurement_bot.providers.browser import BrowserResearchPort, StructuredBrowserProvider
from procurement_bot.providers.codex_chrome import CodexChromeExecutor
from procurement_bot.providers.configured_agy_browser import ConfiguredAgyBrowserExecutor
from procurement_bot.providers.olx_fallback import OlxFallbackProvider
from procurement_bot.providers.supplier_audio import KazakhAudioTranscriber
from procurement_bot.providers.supplier_image import CodexSupplierImageReader
from procurement_bot.providers.two_gis_fallback import TwoGisFallbackProvider
from procurement_bot.providers.waha import WahaClient
from procurement_bot.research_execution import ResearchRunExecutor
from procurement_bot.resources import resource_path
from procurement_bot.storage import LocalContentAddressedStorage
from procurement_bot.telegram import TelegramIngress
from procurement_bot.transcription import DisabledTranscriber
from procurement_bot.waha_webhook_http import create_waha_webhook_app
from procurement_bot.worker import JobWorker

LOGGER = logging.getLogger(__name__)


class _PollingStopped(Exception):
    """Terminate worker siblings after aiogram handles SIGTERM normally."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="procurement-bot")
    parser.add_argument(
        "command",
        choices=("migrate", "health", "bot", "worker", "webhook", "miniapp", "run"),
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
        applied = await run_migrations(pool, resource_path("migrations"))
        print(json.dumps({"applied": applied}, ensure_ascii=False))
    finally:
        await pool.close()


async def _health(settings: Settings) -> None:
    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=2)
    try:
        snapshot = await health_snapshot(pool, settings=settings)
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
        public_url = validate_public_url(settings.mini_app_public_url)
        if public_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Заявки", web_app=WebAppInfo(url=public_url))
            )
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
        known_sessions=(settings.waha_session,),
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


async def _run_mini_app(settings: Settings) -> None:
    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=5)
    app = create_mini_app(pool, settings)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host=settings.mini_app_bind_host, port=settings.mini_app_port)
        await site.start()
        LOGGER.info("Mini App listening", extra={"port": settings.mini_app_port})
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await pool.close()


_BROWSER_HEAVY_JOB_KINDS = (
    "parallel_research_task",
    "enrich_2gis_contact",
    "resolve_locality",
)


def _excluded_job_kinds_for_worker(settings: Settings, index: int) -> tuple[str, ...]:
    """Reserve only the configured browser budget for browser-heavy jobs."""

    browser_research_enabled = (
        settings.parallel_research_enabled or settings.staged_research_enabled
    )
    if not browser_research_enabled or settings.worker_concurrency <= 1:
        return ()
    heavy_workers = min(settings.browser_cli_concurrency, settings.worker_concurrency)
    return _BROWSER_HEAVY_JOB_KINDS if index >= heavy_workers else ()


def _start_worker_tasks(
    tasks: asyncio.TaskGroup,
    settings: Settings,
    pool,
    bot: Bot,
) -> None:
    storage = LocalContentAddressedStorage(settings.media_root)
    # Profile provisioning is deferred until an AGY-backed job is claimed so
    # missing credentials cannot take the worker process down at startup.
    agy_home = settings.agy_home
    extractor = ConfiguredAgyExtractor(settings)
    intake = IntakeService(extractor, scope_policy=SearchScopePolicy.require_explicit())
    research_executor = _build_research_executor(settings, pool, profile_extractor=extractor)
    if settings.voice_provider == "assemblyai":
        transcriber = AssemblyAITranscriber(
            settings.require_assemblyai_api_key(),
            timeout_seconds=settings.assemblyai_timeout_seconds,
        )
    else:
        transcriber = DisabledTranscriber()
    process = f"{socket.gethostname()}:{os.getpid()}"
    waha = (
        WahaClient(
            base_url=settings.waha_base_url,
            api_key=settings.require_waha_api_key(),
            session=settings.waha_session,
            enabled=True,
            outbound_enabled=settings.waha_outbound_enabled,
            timeout_seconds=settings.waha_timeout_seconds,
        )
        if settings.waha_enabled
        else None
    )
    download_limit = settings.max_source_file_bytes
    image_reader = (
        CodexSupplierImageReader(
            settings.codex_2gis_executable, model=settings.codex_2gis_model,
            timeout_seconds=settings.supplier_image_timeout_seconds,
        ) if settings.supplier_media_enabled else None
    )
    kazakh_transcriber = (
        KazakhAudioTranscriber(
            executable=settings.agy_executable, model=settings.agy_model,
            timeout_seconds=settings.agy_timeout_seconds, home_dir=agy_home,
        ) if settings.supplier_media_enabled else None
    )
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
            semantic_supplier_reply_enabled=settings.semantic_supplier_reply_enabled,
            research_auto_prepare_outreach=settings.research_auto_prepare_outreach,
            transcription_min_language_confidence=(settings.transcription_min_language_confidence),
            waha_session=settings.waha_session,
            waha_client=waha,
            excluded_job_kinds=_excluded_job_kinds_for_worker(settings, index),
            supplier_image_reader=image_reader,
            supplier_kazakh_transcriber=kazakh_transcriber,
            supplier_media_max_bytes=settings.supplier_media_max_bytes,
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
        assert waha is not None
        whatsapp_outbox = WhatsAppOutboxWorker(
            pool=pool,
            client=waha,
            worker_id=f"{process}:whatsapp-outbox",
        )
        tasks.create_task(whatsapp_outbox.run_forever())


def _build_research_executor(
    settings: Settings,
    pool,
    *,
    profile_extractor=None,
) -> ResearchRunExecutor | ParallelResearchRunExecutor | None:
    if not settings.browser_research_enabled:
        return None
    server = settings.require_browser_mcp_server()
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
    resource_budget = asyncio.Semaphore(settings.browser_cli_concurrency)
    transport = ConfiguredAgyBrowserExecutor(
        settings,
        executable=executable.resolve(),
        mcp_server_name=settings.agy_browser_mcp_server_name.strip(),
        expected_mcp_server=server,
        transport_factory=AgyBrowserMcpExecutor,
        model=settings.agy_browser_model.strip() or settings.agy_model,
        effort=settings.agy_browser_effort,
        timeout_seconds=settings.agy_browser_timeout_seconds,
        concurrency=settings.agy_browser_concurrency,
        runtime_environment=runtime_environment,
        resource_budget=resource_budget,
        diagnostics_root=settings.browser_diagnostics_root,
        batch_timeout_seconds=settings.browser_batch_timeout_seconds,
        captcha_cooldown_seconds=(
            settings.browser_source_cooldown_seconds
            if settings.staged_research_enabled
            else 60
        ),
    )
    browser: BrowserResearchPort = StructuredBrowserProvider(transport)
    codex_transport = None
    codex_needed = (
        settings.codex_2gis_fallback_enabled
        or settings.codex_olx_fallback_enabled
        or (
            settings.staged_research_enabled
            and settings.staged_research_primary == "codex"
        )
    )
    if codex_needed:
        codex = settings.codex_2gis_executable.expanduser()
        if not codex.is_absolute() or not codex.is_file() or not os.access(codex, os.X_OK):
            raise ValueError(f"Codex Chrome executable was not found: {codex}")
        codex_transport = CodexChromeExecutor(
            executable=codex,
            model=settings.codex_2gis_model,
            reasoning_effort=settings.codex_2gis_reasoning_effort,
            timeout_seconds=settings.codex_2gis_timeout_seconds,
            resource_budget=resource_budget,
            diagnostics_root=settings.browser_diagnostics_root,
            batch_timeout_seconds=settings.browser_batch_timeout_seconds,
            resource_wait_seconds=settings.browser_resource_wait_seconds,
            retain_failure_trace=settings.browser_failure_trace_enabled,
            chrome_plugin_root=settings.codex_chrome_plugin_root,
            chrome_node_executable=settings.codex_chrome_node_executable,
        )
        fallback = StructuredBrowserProvider(codex_transport)
        if settings.codex_2gis_fallback_enabled:
            browser = TwoGisFallbackProvider(
                browser,
                fallback,
                web_fallback_enabled=settings.parallel_research_enabled,
            )
        if settings.codex_olx_fallback_enabled:
            browser = OlxFallbackProvider(browser, fallback)
    if settings.staged_research_enabled:
        from procurement_bot.providers.staged_research import StagedResearchProvider

        staged_primary = (
            codex_transport
            if settings.staged_research_primary == "codex"
            else transport
        )
        staged_fallback = (
            None
            if settings.staged_research_primary == "codex"
            else codex_transport
        )
        browser = StagedResearchProvider(
            pool,
            browser,
            staged_primary,
            staged_fallback,
            separate_contacts=True,
        )
    if settings.staged_research_enabled and profile_extractor is None:
        profile_extractor = ConfiguredAgyExtractor(settings)
    if settings.parallel_research_enabled:
        return ParallelResearchRunExecutor(
            pool,
            browser,
            max_active_groups=settings.research_active_groups,
            target_suppliers=settings.research_target_suppliers,
            max_batches=settings.research_max_batches,
            profile_extractor=profile_extractor if settings.staged_research_enabled else None,
            card_candidate_limit=1 if settings.staged_research_enabled else None,
        )
    return ResearchRunExecutor(pool, browser)


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
    elif command == "miniapp":
        await _run_mini_app(settings)
    else:
        await _run_bot(settings, with_workers=True)


def main() -> None:
    arguments = build_parser().parse_args()
    asyncio.run(_async_main(arguments.command))


if __name__ == "__main__":
    main()
