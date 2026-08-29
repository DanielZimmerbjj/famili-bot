from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from family_bot.config import Settings
from family_bot.models import Household, ScheduledRun, utcnow
from family_bot.services.cycles import get_current_cycle
from family_bot.services.rates import RateService
from family_bot.services.reports import build_report
from family_bot.telegram.keyboards import cycle_close_keyboard, main_menu_keyboard

logger = logging.getLogger(__name__)


class ReportScheduler:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        rate_service: RateService,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bot = bot
        self.rate_service = rate_service
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        logger.info("Report scheduler started")
        while not self.stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Report scheduler tick failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=30)
        logger.info("Report scheduler stopped")

    async def stop(self) -> None:
        self.stop_event.set()

    async def _tick(self) -> None:
        now = datetime.now(self.settings.timezone)
        if (now.hour, now.minute) < (
            self.settings.daily_report_hour,
            self.settings.daily_report_minute,
        ):
            return
        async with self.session_factory() as session:
            households = (
                await session.scalars(
                    select(Household).where(
                        Household.active.is_(True), Household.telegram_chat_id.is_not(None)
                    )
                )
            ).all()
            for household in households:
                await self._send_once(session, household, now)

    async def _send_once(self, session: AsyncSession, household: Household, now: datetime) -> None:
        existing = await session.scalar(
            select(ScheduledRun).where(
                ScheduledRun.household_id == household.id,
                ScheduledRun.run_date == now.date(),
                ScheduledRun.run_type == "daily_report",
            )
        )
        if existing is not None and existing.status == "completed":
            return
        run = existing or ScheduledRun(
            household_id=household.id,
            run_date=now.date(),
            run_type="daily_report",
        )
        if existing is None:
            session.add(run)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return
        try:
            cycle = await get_current_cycle(
                session,
                household,
                now.date(),
                self.settings.financial_cycle_start_day,
            )
            text = await build_report(session, self.rate_service, household, cycle, now.date())
            await self.bot.send_message(
                household.telegram_chat_id,
                text,
                reply_markup=main_menu_keyboard(),
            )
            if now.date() >= cycle.end_date and cycle.status == "open":
                await self.bot.send_message(
                    household.telegram_chat_id,
                    "Финансовый месяц завершён. Куда направить остаток?",
                    reply_markup=cycle_close_keyboard(cycle.id),
                )
            run.status = "completed"
            run.completed_at = utcnow()
            run.error_message = None
            await session.commit()
        except Exception as exc:
            run.status = "failed"
            run.error_message = str(exc)[:2000]
            await session.commit()
            raise
