from __future__ import annotations

import base64
import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
)

logger = logging.getLogger(__name__)


def normalize_decimal_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip().replace(" ", "")
    if "," not in normalized:
        return normalized
    if "." in normalized:
        return normalized.replace(",", "")
    whole, separator, fraction = normalized.rpartition(",")
    if separator and 0 < len(fraction) <= 2:
        return f"{whole}.{fraction}"
    return normalized.replace(",", "")

# Pydantic's default JSON schema for Decimal contains a negative-lookahead regex.
# OpenAI Structured Outputs deliberately supports only a safe regex subset, so that
# schema is rejected before the model can inspect the receipt. Keep values as strings
# in the wire schema and let Pydantic validate and convert them back to Decimal exactly.
PositiveDecimal = Annotated[
    Decimal,
    BeforeValidator(normalize_decimal_text),
    Field(gt=0),
    WithJsonSchema({"type": "string"}),
]
NonNegativeDecimal = Annotated[
    Decimal,
    BeforeValidator(normalize_decimal_text),
    Field(ge=0),
    WithJsonSchema({"type": "string"}),
]


class ReceiptExtractionError(RuntimeError):
    """OpenAI extraction failed after all configured models were attempted."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_name: str = Field(description="Item name exactly as printed on the receipt")
    name_ru: str = Field(
        description=(
            "Short natural Russian translation of the item name for the user; "
            "keep internationally recognizable brand names"
        )
    )
    quantity: PositiveDecimal
    # Receipts can contain gifts, promo rows, or fully discounted items with a
    # printed value of 0.00. They are valid receipt lines, just not expenses.
    unit_price: NonNegativeDecimal | None
    line_total: NonNegativeDecimal
    category_key: str
    subcategory_key: str | None
    confidence: float = Field(ge=0, le=1)


class ReceiptExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str
    merchant: str | None
    purchased_at: datetime | None
    currency: str
    subtotal: NonNegativeDecimal | None
    discount: NonNegativeDecimal
    tax: NonNegativeDecimal
    total: PositiveDecimal
    items: list[ExtractedItem]
    warnings: list[str]
    overall_confidence: float = Field(ge=0, le=1)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        return value.strip().upper()


class ReceiptExtractor:
    schema_version = "1.1"

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
        attempts: list[tuple[str, Exception]] = []
        models = list(dict.fromkeys((self.model, self.fallback_model)))
        for model in models:
            try:
                return await self._extract_with_model(images, prompt, model), model
            except Exception as exc:
                attempts.append((model, exc))
                logger.warning(
                    "Receipt extraction with model %s failed: %s",
                    model,
                    self._describe_error(exc),
                )

        details = "; ".join(
            f"{model}: {self._describe_error(error)}" for model, error in attempts
        )
        raise ReceiptExtractionError(
            details or "OpenAI receipt extraction failed",
            retryable=any(self._is_retryable(error) for _, error in attempts),
        ) from attempts[-1][1]

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
    def _is_retryable(error: Exception) -> bool:
        if isinstance(error, (APIConnectionError, APITimeoutError)):
            return True
        if isinstance(error, APIStatusError):
            return error.status_code == 429 or error.status_code >= 500
        # A malformed model response can be different on a later attempt.
        return not isinstance(error, (TypeError, ValueError))

    @staticmethod
    def _describe_error(error: Exception) -> str:
        if isinstance(error, APIStatusError):
            body = getattr(error, "body", None)
            if isinstance(body, dict):
                payload = body.get("error", body)
                if isinstance(payload, dict):
                    message = str(payload.get("message") or type(error).__name__)
                    code = payload.get("code")
                    param = payload.get("param")
                    suffix = ", ".join(
                        str(value) for value in (code, param) if value not in (None, "")
                    )
                    return f"HTTP {error.status_code}: {message}" + (
                        f" ({suffix})" if suffix else ""
                    )
            return f"HTTP {error.status_code}: {type(error).__name__}"
        return f"{type(error).__name__}: {error}"

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
- raw_name must contain the item text exactly as printed, in its original language.
- name_ru must be a short, natural Russian translation understandable to a Russian speaker.
  Translate Thai, Vietnamese and every other non-Russian item name by meaning, not by
  transliteration. Keep internationally recognizable brands such as Coca-Cola or Nestle.
  Do not copy Thai or Vietnamese script into name_ru. If part of a name is unreadable,
  describe the recognizable product type in Russian and add "(неразборчиво)".
- category_key must be one of the keys below, or unknown.
- subcategory_key must belong to its selected category, or be null.
- confidence reflects visual certainty, not plausibility.

Allowed categories:
{category_lines}

Known family corrections (use only for the exact or clearly equivalent item):
{alias_lines or "- none"}
""".strip()
