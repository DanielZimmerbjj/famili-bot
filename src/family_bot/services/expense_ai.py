from __future__ import annotations

from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator


class InterpretedExpenseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    amount: float | None = Field(default=None, gt=0)
    currency: str | None = None
    category_key: str | None = None
    goal_key: str | None = None
    goal_name: str | None = None
    new_goal_name: str | None = None
    target_amount: float | None = Field(default=None, gt=0)
    target_currency: str | None = None
    target_item_number: int | None = Field(default=None, ge=1)
    target_item_name: str | None = None
    confidence: float = Field(ge=0, le=1)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("target_currency")
    @classmethod
    def target_currency_upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None


class ExpenseInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "expense",
        "income",
        "goal_contribution",
        "goal_expense",
        "goal_target",
        "goal_update",
        "report",
        "correction",
        "other",
    ]
    merchant: str | None = None
    receipt_total: float | None = Field(default=None, gt=0)
    receipt_currency: str | None = None
    report_period: Literal["today", "yesterday", "current_cycle"] | None = None
    report_focus: (
        Literal[
            "full",
            "summary",
            "largest_category",
            "category_breakdown",
            "recent_expenses",
        ]
        | None
    ) = None
    target_entry_ref: str | None = None
    correction_scope: Literal["operation", "whole_receipt", "receipt_item"] | None = None
    assistant_reply: str | None = None
    items: list[InterpretedExpenseItem]
    overall_confidence: float = Field(ge=0, le=1)

    @field_validator("receipt_currency")
    @classmethod
    def receipt_currency_upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None


class ExpenseInterpreter:
    """Transcribe Telegram audio and turn natural language into budget operations."""

    def __init__(
        self,
        api_key: str,
        intent_model: str,
        fallback_model: str,
        transcription_model: str,
        default_currency: str,
        timeout: float = 90,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self.intent_model = intent_model
        self.fallback_model = fallback_model
        self.transcription_model = transcription_model
        self.default_currency = default_currency

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "voice.ogg",
        mime_type: str = "audio/ogg",
    ) -> str:
        if not audio:
            raise ValueError("Голосовое сообщение пустое")
        result = await self.client.audio.transcriptions.create(
            model=self.transcription_model,
            file=(filename, audio, mime_type),
            language="ru",
            prompt=(
                "Семейные расходы в Таиланде. Точно записывай названия магазинов, "
                "товаров, суммы и валюты: баты THB, тенге KZT, донги VND, рубли RUB."
            ),
        )
        text = result.text.strip()
        if not text:
            raise ValueError("Не удалось распознать речь")
        return text

    async def interpret(
        self,
        text: str,
        categories: dict[str, str],
        previous_context: str | None = None,
        goals: dict[str, str] | None = None,
    ) -> ExpenseInterpretation:
        prompt = self._prompt(text, categories, previous_context, goals)
        try:
            return await self._interpret_with_model(prompt, self.intent_model)
        except Exception:
            if self.fallback_model == self.intent_model:
                raise
            return await self._interpret_with_model(prompt, self.fallback_model)

    async def _interpret_with_model(self, prompt: str, model: str) -> ExpenseInterpretation:
        response = await self.client.responses.parse(
            model=model,
            input=[{"role": "user", "content": prompt}],
            text_format=ExpenseInterpretation,
            reasoning={"effort": "low"},
            store=False,
        )
        if response.output_parsed is None:
            raise ValueError("OpenAI returned no parsed expense")
        return response.output_parsed

    def _prompt(
        self,
        text: str,
        categories: dict[str, str],
        previous_context: str | None,
        goals: dict[str, str] | None = None,
    ) -> str:
        category_lines = "\n".join(f"- {key}: {name}" for key, name in categories.items())
        goal_lines = (
            "\n".join(f"- {key}: {name}" for key, name in (goals or {}).items())
            or "- car: Автомобиль\n- border_run: Бордерран"
        )
        return f"""
Act as the conversational financial planner for a private Russian-language family-budget
chat. Understand the whole message, use the recent-operation context and propose exactly one
typed action in the supplied JSON schema. The application, not you, validates and executes it.

Message: {text}

Rules:
- kind=expense for a new purchase; split explicitly priced purchases into separate items.
- kind=income when money was received, including salary.
- kind=goal_contribution when money was intentionally set aside into a savings goal.
- kind=goal_expense when a payment must be taken from an existing savings fund.
- kind=goal_target when the user establishes the total price of a savings goal without
  explicitly asking to edit an existing goal.
- kind=goal_update when the user explicitly asks to rename, replace or reprice an existing
  savings goal. This updates the same goal and must never create a new one.
- kind=report when the user asks to show, send or summarize the family finances, budget,
  expenses, income, balances, category limits or savings progress. A report is read-only.
- kind=correction when the user asks to change an already stored expense, receipt or income.
  Direct wording is not required: a complaint or question such as "почему ты записал это в
  7-Eleven, это были SIM-карты" is a correction, not a report or a new expense.
- kind=other only for a non-financial message or for an attempted new operation whose
  essential amount cannot be determined. A question about already stored expenses, income,
  balances, categories, receipts or savings is always kind=report even though it does not
  create an operation. Never turn an ordinary non-financial conversation into an operation.
- For a new expense, every item needs a positive amount, ISO 4217 currency, category_key
  and confidence. Description may be null only for a terse but otherwise complete purchase;
  the application will use the merchant or category as its description. If currency is
  omitted in an ordinary Thailand purchase, use {self.default_currency}.
- Terse entries are still valid expenses. For example, "240 бат 7/11" means one expense:
  merchant="7-Eleven", amount=240, currency="THB", category_key="seven_eleven".
- For income, every item needs description, positive amount, ISO 4217 currency and confidence.
  Salary without an explicit currency is KZT for this household.
- For an existing savings goal, return its goal_key. For a new goal such as a laptop,
  return goal_key=null and a concise goal_name in Russian. New goals are allowed.
- For goal_contribution and goal_expense, every item needs either an existing goal_key or
  a goal_name. If the same message states the full price, put it in target_amount and
  target_currency separately from the contribution amount and currency.
- For goal_target, return the goal key or name plus target_amount and target_currency.
  If the target currency is omitted, use KZT for this household.
- For goal_update, identify the old existing goal in goal_key (preferred) or goal_name.
  Put a replacement title such as "MacBook M6" in new_goal_name. Put a replacement price
  in target_amount and target_currency. Leave either replacement field null when unchanged.
  A phrase such as "измени цель MacBook M5 на MacBook M6, теперь он стоит миллион" is
  goal_update even though it contains words such as "измени" or "вместо".
- For report, return an empty items list and set report_period plus report_focus.
  Use current_cycle/full for an explicit full report such as "скинь отчет" or "покажи бюджет".
  Use today/summary for "сколько сегодня потратил", today/largest_category for
  "на что сегодня потратил больше всего", today/category_breakdown for
  "на что сегодня тратил", and recent_expenses for a request to list recent purchases.
  Use yesterday with the same requested focus for phrases such as "сколько я потратил вчера"
  or "на что вчера ушли деньги". The period word may appear anywhere in a colloquial sentence.
  Conversational wording such as "бро, расскажи" does not make a finance question other.
  Requests such as "сколько денег осталось" and "как у нас дела с накоплениями" use
  current_cycle/full.
- Report questions are read-only and safe: when their period and focus are clear, classify
  them confidently as report instead of rejecting them for having no transaction amount.
- For a correction, return only values that change; null means keep the previous value.
  Do not use correction for editing a savings goal; use goal_update.
- Recent operations are labelled T1, T2, etc. For a correction, resolve words such as
  "прошлый", "последний", a merchant, description, amount or operation type against that list
  and put the best matching label in target_entry_ref. T1 is the newest operation. Never copy
  an internal database id. If no operation can be identified reliably, leave target_entry_ref
  null and explain what detail is needed in assistant_reply.
- A correction may edit either an expense or an income. Use correction_scope=operation for a
  regular ledger entry, whole_receipt when the whole receipt needs a merchant, total or category
  change, and receipt_item only when one numbered or named line item is being corrected.
- A receipt correction does not need confirmation first: it edits the already-posted
  previous receipt in the database.
- For a correction of the receipt store/merchant, put the corrected store name in the
  top-level merchant field. Example: "прошлый чек был не 7-Eleven, а Big C" means
  merchant="Big C" and may have an empty items list.
- When the user corrects the final total of the whole receipt, put it in receipt_total and
  put its currency in receipt_currency. Do not invent a fake line item for the total.
- target_item_number is only for correcting a numbered receipt item. When the user names
  an existing item instead of its number, put that old/current name in target_item_name;
  description is the corrected replacement name. Do not set target_item_name merely to the
  old category or merchant name.
- category_key must be one of the allowed keys. The merchant describes where payment happened;
  it does not override the purpose of the payment. Explicit purpose always wins. In particular,
  a phone balance, mobile package or SIM-card top-up is category mobile even when it was paid at
  7-Eleven. Ordinary physical goods bought at 7-Eleven use seven_eleven when no more specific
  family-budget purpose is stated. Other supermarket drinks, milk and snacks are
  groceries_household unless the message explicitly says they were consumed in a cafe.
- Do not invent amounts, merchants, products, categories or currencies. If a correction says
  "for the same money", leave amount null.
- For kind=other, put a short helpful Russian conversational answer in assistant_reply. Ask one
  concise clarifying question when a requested financial action lacks a critical detail. Never
  claim that data was changed unless kind is a mutating typed action. For other kinds,
  assistant_reply may be null because the application builds the final confirmed response.

Allowed categories:
{category_lines}

Existing savings goals (a new goal name is also allowed):
{goal_lines}

Recent operations, newest first, when available:
{previous_context or "none"}
""".strip()
