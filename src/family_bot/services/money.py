from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from family_bot.constants import CURRENCY_ALIASES

SCALE = Decimal("0.00000001")


class CurrencyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RateQuote:
    currency: str
    rate_date: date
    nominal: Decimal
    rate_kzt: Decimal
    provider: str = "NBK"
    rate_id: str | None = None

    def to_kzt(self, amount: Decimal) -> Decimal:
        if self.nominal <= 0 or self.rate_kzt <= 0:
            raise CurrencyError("Currency quote must have positive nominal and rate")
        return quantize(amount * self.rate_kzt / self.nominal)

    def from_kzt(self, amount_kzt: Decimal) -> Decimal:
        if self.nominal <= 0 or self.rate_kzt <= 0:
            raise CurrencyError("Currency quote must have positive nominal and rate")
        return quantize(amount_kzt * self.nominal / self.rate_kzt)


def quantize(value: Decimal | str | int | float) -> Decimal:
    return Decimal(str(value)).quantize(SCALE, rounding=ROUND_HALF_UP)


def normalize_currency(value: str) -> str:
    normalized = value.casefold().strip().rstrip(".,")
    if normalized in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[normalized]
    upper = value.upper().strip()
    if re.fullmatch(r"[A-Z]{3}", upper):
        return upper
    raise CurrencyError(f"Unknown currency: {value}")


def kzt_quote(on_date: date) -> RateQuote:
    return RateQuote(
        currency="KZT",
        rate_date=on_date,
        nominal=Decimal("1"),
        rate_kzt=Decimal("1"),
        provider="IDENTITY",
    )


def convert(amount: Decimal, source: RateQuote, target: RateQuote) -> Decimal:
    return target.from_kzt(source.to_kzt(amount))
