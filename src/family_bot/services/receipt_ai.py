from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_name: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal | None = Field(gt=0)
    line_total: Decimal = Field(gt=0)
    category_key: str
    subcategory_key: str | None
    confidence: float = Field(ge=0, le=1)


class ReceiptExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str
    merchant: str | None
    purchased_at: datetime | None
    currency: str
    subtotal: Decimal | None
    discount: Decimal = Field(ge=0)
    tax: Decimal = Field(ge=0)
    total: Decimal = Field(gt=0)
    items: list[ExtractedItem]
    warnings: list[str]
    overall_confidence: float = Field(ge=0, le=1)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        return value.strip().upper()


class ReceiptExtractor:
    schema_version = "1.0"

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str,
        timeout: float = 90,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self.model = model
        self.fallback_model = fallback_model

    async def extract(
        self,
        images: list[tuple[bytes, str]],
        categories: dict[str, tuple[str, ...]],
        known_aliases: dict[str, tuple[str, str | None]] | None = None,
    ) -> tuple[ReceiptExtraction, str]:
        if not images:
            raise ValueError("Receipt has no images")
        prompt = self._prompt(categories, known_aliases or {})
        try:
            return await self._extract_with_model(images, prompt, self.model), self.model
        except Exception:
            if self.fallback_model == self.model:
                raise
            return (
                await self._extract_with_model(images, prompt, self.fallback_model),
                self.fallback_model,
            )

    async def _extract_with_model(
        self,
        images: list[tuple[bytes, str]],
        prompt: str,
        model: str,
    ) -> ReceiptExtraction:
        content: list[dict[str, object]] = [{"type": "input_text", "text": prompt}]
        for image_bytes, mime_type in images:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                    "detail": "original",
                }
            )
        response = await self.client.responses.parse(
            model=model,
            input=[{"role": "user", "content": content}],
            text_format=ReceiptExtraction,
            reasoning={"effort": "low"},
            store=False,
        )
        if response.output_parsed is None:
            raise ValueError("OpenAI returned no parsed receipt")
        return response.output_parsed

    @staticmethod
    def _prompt(
        categories: dict[str, tuple[str, ...]],
        aliases: dict[str, tuple[str, str | None]],
    ) -> str:
        category_lines = "\n".join(
            f"- {key}; allowed subcategories: {', '.join(subs) if subs else 'null'}"
            for key, subs in categories.items()
        )
        alias_lines = "\n".join(
            f"- {name} => {category}/{subcategory or 'null'}"
            for name, (category, subcategory) in aliases.items()
        )
        return f"""
Extract every visible line item from this purchase receipt into the supplied JSON schema.

Rules:
- Preserve the receipt currency as a three-letter ISO 4217 code. Recognize THB, KZT, VND,
  RUB and other fiat currencies. Do not treat every $ symbol as USD without country evidence.
- Never convert money and never calculate a budget balance.
- Use decimal strings exactly as printed. For VND, preserve the full dong amount.
- Return discount and tax as non-negative receipt-level amounts; use 0 when absent.
- Use null when merchant, date, subtotal, or unit price is not visible. Do not guess.
- Set document_type to receipt only when this is a receipt; otherwise use not_receipt.
- Extract all items, discounts and the final total. Do a second scan for missed lines.
- category_key must be one of the keys below, or unknown.
- subcategory_key must belong to its selected category, or be null.
- confidence reflects visual certainty, not plausibility.

Allowed categories:
{category_lines}

Known family corrections (use only for the exact or clearly equivalent item):
{alias_lines or "- none"}
""".strip()
