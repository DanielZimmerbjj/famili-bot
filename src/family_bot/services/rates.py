from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from xml.etree import ElementTree

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from family_bot.models import ExchangeRate
from family_bot.services.money import CurrencyError, RateQuote, kzt_quote

logger = logging.getLogger(__name__)

NBK_HISTORICAL_URL = "https://nationalbank.kz/rss/get_rates.cfm"


class RateUnavailableError(CurrencyError):
    pass


class NbkClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def fetch(self, currency: str, requested_date: date) -> RateQuote:
        currency = currency.upper()
        if currency == "KZT":
            return kzt_quote(requested_date)

        last_error: Exception | None = None
        for days_back in range(0, 8):
            rate_date = requested_date - timedelta(days=days_back)
            try:
                quote = await self._fetch_exact(currency, rate_date)
            except (httpx.HTTPError, ElementTree.ParseError) as exc:
                last_error = exc
                logger.warning("NBK rate fetch failed for %s on %s: %s", currency, rate_date, exc)
                continue
            if quote is not None:
                return quote

        message = f"NBK has no {currency} rate on or before {requested_date}"
        if last_error:
            message = f"{message}: {last_error}"
        raise RateUnavailableError(message)

    async def _fetch_exact(self, currency: str, rate_date: date) -> RateQuote | None:
        params = {"fdate": rate_date.strftime("%d.%m.%Y")}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(NBK_HISTORICAL_URL, params=params)
            response.raise_for_status()

        root = ElementTree.fromstring(response.content)
        published_text = root.findtext("date")
        published_date = (
            date.fromisoformat(published_text)
            if published_text and "-" in published_text
            else rate_date
        )
        for item in root.findall("item"):
            if (item.findtext("title") or "").strip().upper() != currency:
                continue
            rate = Decimal((item.findtext("description") or "0").strip().replace(",", "."))
            nominal = Decimal((item.findtext("quant") or "1").strip().replace(",", "."))
            if rate <= 0 or nominal <= 0:
                return None
            return RateQuote(
                currency=currency,
                rate_date=published_date,
                nominal=nominal,
                rate_kzt=rate,
                provider="NBK",
            )
        return None


class RateService:
    def __init__(self, client: NbkClient | None = None) -> None:
        self.client = client or NbkClient()
        self._effective_cache: dict[tuple[str, date], RateQuote] = {}

    async def get_quote(
        self, session: AsyncSession, currency: str, requested_date: date
    ) -> RateQuote:
        currency = currency.upper()
        if currency == "KZT":
            return kzt_quote(requested_date)

        manual = await session.scalar(
            select(ExchangeRate)
            .where(
                ExchangeRate.currency == currency,
                ExchangeRate.rate_date == requested_date,
                ExchangeRate.is_manual.is_(True),
            )
            .order_by(ExchangeRate.created_at.desc())
            .limit(1)
        )
        if manual is not None:
            return self._from_model(manual)

        cache_key = (currency, requested_date)
        cached = self._effective_cache.get(cache_key)
        if cached is not None:
            return cached

        exact_statement = select(ExchangeRate).where(
            ExchangeRate.currency == currency,
            ExchangeRate.rate_date == requested_date,
            ExchangeRate.provider == "NBK",
        )
        exact = await session.scalar(exact_statement)
        if exact is not None:
            quote = self._from_model(exact)
            self._effective_cache[cache_key] = quote
            return quote

        fallback_statement = (
            select(ExchangeRate)
            .where(
                ExchangeRate.currency == currency,
                ExchangeRate.rate_date <= requested_date,
                ExchangeRate.provider == "NBK",
            )
            .order_by(ExchangeRate.rate_date.desc())
            .limit(1)
        )
        fallback = await session.scalar(fallback_statement)
        try:
            fetched = await self.client.fetch(currency, requested_date)
        except RateUnavailableError:
            if fallback is not None and (requested_date - fallback.rate_date).days <= 7:
                quote = self._from_model(fallback)
                self._effective_cache[cache_key] = quote
                return quote
            raise
        statement = select(ExchangeRate).where(
            ExchangeRate.rate_date == fetched.rate_date,
            ExchangeRate.currency == fetched.currency,
            ExchangeRate.provider == fetched.provider,
        )
        existing = await session.scalar(statement)
        if existing is None:
            candidate = ExchangeRate(
                rate_date=fetched.rate_date,
                currency=fetched.currency,
                nominal=fetched.nominal,
                rate_kzt=fetched.rate_kzt,
                provider=fetched.provider,
                source_url=(f"{NBK_HISTORICAL_URL}?fdate={fetched.rate_date.strftime('%d.%m.%Y')}"),
            )
            try:
                async with session.begin_nested():
                    session.add(candidate)
                    await session.flush()
                existing = candidate
            except IntegrityError:
                existing = await session.scalar(statement)
                if existing is None:
                    raise
        quote = self._from_model(existing)
        self._effective_cache[cache_key] = quote
        return quote

    @staticmethod
    def _from_model(rate: ExchangeRate) -> RateQuote:
        return RateQuote(
            currency=rate.currency,
            rate_date=rate.rate_date,
            nominal=Decimal(rate.nominal),
            rate_kzt=Decimal(rate.rate_kzt),
            provider=rate.provider,
            rate_id=rate.id,
        )
