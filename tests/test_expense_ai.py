from family_bot.services.expense_ai import (
    ExpenseInterpretation,
    ExpenseInterpreter,
    InterpretedExpenseItem,
)
from family_bot.telegram.handlers import is_correction_text


def test_expense_interpretation_normalizes_currency() -> None:
    result = ExpenseInterpretation(
        kind="expense",
        items=[
            InterpretedExpenseItem(
                description="Coca-Cola",
                amount=35,
                currency="thb",
                category_key="groceries_household",
                confidence=0.97,
            )
        ],
        overall_confidence=0.96,
    )
    assert result.items[0].currency == "THB"


def test_interpreter_prompt_keeps_same_amount_for_correction() -> None:
    interpreter = ExpenseInterpreter(
        api_key="test",
        intent_model="test-model",
        fallback_model="test-model",
        transcription_model="test-transcription",
        default_currency="THB",
    )
    prompt = interpreter._prompt(
        "нет, это было молоко за те же деньги",
        {"groceries_household": "Продукты"},
        "Расход: Fanta; 35 THB",
    )
    assert "leave amount null" in prompt
    assert "groceries_household" in prompt


def test_correction_markers() -> None:
    assert is_correction_text("Нет, это было молоко")
    assert is_correction_text("На самом деле сумма 40 бат")
    assert not is_correction_text("Купил молоко за 40 бат")
