from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from family_bot.constants import CATEGORY_TEXT_ALIASES
from family_bot.services.money import CurrencyError, normalize_currency

AMOUNT_CURRENCY_RE = re.compile(
    r"(?P<amount>\d[\d\s]*(?:[.,]\d+)?)\s*(?P<currency>[A-Za-zА-Яа-яЁё₸฿₫₽]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedIntent:
    kind: str
    amount: Decimal
    currency: str
    category_key: str | None = None
    goal_key: str | None = None
    description: str = ""


def parse_text_intent(text: str) -> ParsedIntent | None:
    normalized = " ".join(text.casefold().strip().split())
    match = AMOUNT_CURRENCY_RE.search(normalized)
    if match is None:
        return None
    try:
        amount = Decimal(match.group("amount").replace(" ", "").replace(",", "."))
        currency = normalize_currency(match.group("currency"))
    except (InvalidOperation, CurrencyError):
        return None
    if amount <= 0:
        return None

    if "зарплат" in normalized or normalized.startswith(("доход", "получил", "получена")):
        return ParsedIntent("income", amount, currency, description=text.strip())
    if any(word in normalized for word in ("отложил", "накоплен", "перевел", "перевёл")):
        if "машин" in normalized or "автомоб" in normalized:
            return ParsedIntent("goal", amount, currency, goal_key="car", description=text.strip())
        if "бордер" in normalized:
            return ParsedIntent(
                "goal", amount, currency, goal_key="border_run", description=text.strip()
            )
    if "бордер" in normalized and any(word in normalized for word in ("билет", "оплат", "купил")):
        return ParsedIntent(
            "goal_expense",
            amount,
            currency,
            goal_key="border_run",
            description=text.strip(),
        )

    category_key = match_category(normalized[: match.start()]) or match_category(normalized)
    if category_key:
        return ParsedIntent(
            "expense",
            amount,
            currency,
            category_key=category_key,
            description=text.strip(),
        )
    return None


def match_category(text: str) -> str | None:
    for alias in sorted(CATEGORY_TEXT_ALIASES, key=len, reverse=True):
        if alias in text:
            return CATEGORY_TEXT_ALIASES[alias]
    return None
