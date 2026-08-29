from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from family_bot.config import Settings, get_settings
from family_bot.database import Database
from family_bot.models import ProcessedUpdate
from family_bot.services.cycles import seed_default_household
from family_bot.services.rates import RateService
from family_bot.services.receipt_ai import ReceiptExtractor
from family_bot.services.receipts import ReceiptService, ReceiptWorker
from family_bot.services.scheduler import ReportScheduler
from family_bot.telegram.handlers import TelegramDependencies, build_router

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    database: Database
    bot: Bot
    dispatcher: Dispatcher
    worker: ReceiptWorker
    scheduler: ReportScheduler
    worker_task: asyncio.Task[None]
    scheduler_task: asyncio.Task[None]


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        if not settings.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        if not settings.openai_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        database = Database(settings)
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dispatcher = Dispatcher()
        rate_service = RateService()
        receipt_service = ReceiptService(settings)
        extractor = ReceiptExtractor(
            api_key=settings.openai_key,
            model=settings.openai_receipt_model,
            fallback_model=settings.openai_fallback_model,
            timeout=settings.openai_timeout_seconds,
        )
        worker = ReceiptWorker(
            settings,
            database.session_factory,
            bot,
            extractor,
            rate_service,
        )
        scheduler = ReportScheduler(
            settings,
            database.session_factory,
            bot,
            rate_service,
        )
        dispatcher.include_router(
            build_router(
                TelegramDependencies(
                    settings=settings,
                    session_factory=database.session_factory,
                    rate_service=rate_service,
                    receipt_service=receipt_service,
                )
            )
        )
        if settings.auto_seed:
            async with database.session_factory() as session, session.begin():
                await seed_default_household(session, settings)
        if settings.telegram_webhook_url:
            await bot.set_webhook(
                settings.telegram_webhook_url,
                secret_token=settings.webhook_secret or None,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        worker_task = asyncio.create_task(worker.run(), name="receipt-worker")
        scheduler_task = asyncio.create_task(scheduler.run(), name="report-scheduler")
        app.state.runtime = Runtime(
            settings=settings,
            database=database,
            bot=bot,
            dispatcher=dispatcher,
            worker=worker,
            scheduler=scheduler,
            worker_task=worker_task,
            scheduler_task=scheduler_task,
        )
        try:
            yield
        finally:
            await worker.stop()
            await scheduler.stop()
            for task in (worker_task, scheduler_task):
                with suppress(asyncio.CancelledError):
                    await task
            await dispatcher.emit_shutdown(bot=bot)
            await bot.session.close()
            await database.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        runtime: Runtime = request.app.state.runtime
        async with runtime.database.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        runtime: Runtime = request.app.state.runtime
        if runtime.settings.webhook_secret:
            supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if supplied != runtime.settings.webhook_secret:
                raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": runtime.bot})
        async with runtime.database.session_factory() as session:
            duplicate = await session.scalar(
                select(ProcessedUpdate.telegram_update_id).where(
                    ProcessedUpdate.telegram_update_id == update.update_id
                )
            )
            if duplicate is not None:
                return {"ok": True}
            await runtime.dispatcher.feed_update(runtime.bot, update)
            session.add(ProcessedUpdate(telegram_update_id=update.update_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
        return {"ok": True}

    return app


app = create_app()
