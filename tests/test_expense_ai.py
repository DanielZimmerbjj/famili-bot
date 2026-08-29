from family_bot.services.expense_ai import (
    ExpenseInterpretation,
    ExpenseInterpreter,
    InterpretedExpenseItem,
)


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


def test_interpretation_supports_income_and_savings() -> None:
    income = ExpenseInterpretation(
        kind="income",
        items=[
            InterpretedExpenseItem(
                description="Зарплата",
                amount=840000,
                currency="kzt",
                confidence=0.99,
            )
        ],
        overall_confidence=0.99,
    )
    savings = ExpenseInterpretation(
        kind="goal_contribution",
        items=[
            InterpretedExpenseItem(
                description="На машину",
                amount=600000,
                currency="KZT",
                goal_key="car",
                confidence=0.99,
            )
        ],
        overall_confidence=0.99,
    )
    assert income.items[0].currency == "KZT"
    assert savings.items[0].goal_key == "car"


def test_interpreter_prompt_delegates_all_financial_kinds_to_ai() -> None:
    interpreter = ExpenseInterpreter(
        api_key="test",
        intent_model="test-model",
        fallback_model="test-model",
        transcription_model="test-transcription",
        default_currency="THB",
    )
    prompt = interpreter._prompt(
        "получил зарплату 840000 тенге",
        {"groceries_household": "Продукты"},
        None,
        {"car": "Автомобиль", "border_run": "Бордерран"},
    )
    assert "kind=income" in prompt
    assert "kind=goal_contribution" in prompt
    assert "kind=goal_expense" in prompt
    assert "kind=report" in prompt
    assert "скинь отчет" in prompt
    assert "Never turn an ordinary conversation into a financial operation" in prompt
