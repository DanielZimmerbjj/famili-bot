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
from family_bot.services.expense_ai import ExpenseInterpreter
from family_bot.services.rates import RateService
from family_bot.services.receipt_ai import ReceiptExtractor
from family_bot.services.receipts import (
    ReceiptService,
    ReceiptWorker,
    backfill_seven_eleven_receipts,
)
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
    polling_task: asyncio.Task[None] | None


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
        expense_interpreter = ExpenseInterpreter(
            api_key=settings.openai_key,
            intent_model=settings.openai_intent_model,
            fallback_model=settings.openai_fallback_model,
            transcription_model=settings.openai_transcription_model,
            default_currency=settings.default_spending_currency,
            timeout=settings.openai_timeout_seconds,
        )
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
                    expense_interpreter=expense_interpreter,
                )
            )
        )
        if settings.auto_seed:
            async with database.session_factory() as session, session.begin():
                household = await seed_default_household(session, settings)
                if household is not None:
                    migrated_items = await backfill_seven_eleven_receipts(session, household)
                    if migrated_items:
                        logger.info(
                            "Moved %s existing 7-Eleven receipt items to the dedicated envelope",
                            migrated_items,
                        )
        polling_task: asyncio.Task[None] | None = None
        if settings.telegram_delivery_mode == "webhook":
            if not settings.telegram_webhook_url:
                raise RuntimeError("TELEGRAM_WEBHOOK_URL is required in webhook mode")
            await bot.set_webhook(
                settings.telegram_webhook_url,
                secret_token=settings.webhook_secret or None,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
            await dispatcher.emit_startup(bot=bot)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            polling_task = asyncio.create_task(
                dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    handle_signals=False,
                    close_bot_session=False,
                ),
                name="telegram-polling",
            )
            logger.info("Telegram long polling started")
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
            polling_task=polling_task,
        )
        try:
            yield
        finally:
            if polling_task is not None:
                with suppress(RuntimeError):
                    await dispatcher.stop_polling()
                with suppress(asyncio.CancelledError):
                    await polling_task
            else:
                await dispatcher.emit_shutdown(bot=bot)
            await worker.stop()
            await scheduler.stop()
            for task in (worker_task, scheduler_task):
                with suppress(asyncio.CancelledError):
                    await task
            await bot.session.close()
            await database.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        runtime: Runtime = request.app.state.runtime
        if runtime.polling_task is not None and runtime.polling_task.done():
            raise HTTPException(status_code=503, detail="Telegram polling stopped")
        if runtime.worker_task.done():
            raise HTTPException(status_code=503, detail="Receipt worker stopped")
        if runtime.scheduler_task.done():
            raise HTTPException(status_code=503, detail="Report scheduler stopped")
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
