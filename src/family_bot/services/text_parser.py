from __future__ import annotations

from family_bot.constants import CATEGORY_TEXT_ALIASES


def match_category(text: str) -> str | None:
    normalized = " ".join(text.casefold().split())
    for alias in sorted(CATEGORY_TEXT_ALIASES, key=len, reverse=True):
        if alias in normalized:
            return CATEGORY_TEXT_ALIASES[alias]
    return None
