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


def test_interpreter_prompt_supports_receipt_merchant_total_and_named_item() -> None:
    interpreter = ExpenseInterpreter(
        api_key="test",
        intent_model="test-model",
        fallback_model="test-model",
        transcription_model="test-transcription",
        default_currency="THB",
    )
    prompt = interpreter._prompt(
        "прошлый чек был не 7-Eleven, а Big C, итог был 220 бат",
        {
            "seven_eleven": "7-Eleven",
            "groceries_household": "Продукты",
        },
        "Чек магазина 7-Eleven; итог 124 THB",
    )

    assert 'merchant="Big C"' in prompt
    assert "receipt_total" in prompt
    assert "receipt_currency" in prompt
    assert "target_item_name" in prompt
    assert "does not need confirmation" in prompt


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
    assert "kind=goal_update" in prompt
    assert "new_goal_name" in prompt
    assert "kind=report" in prompt
    assert "скинь отчет" in prompt
    assert '"240 бат 7/11"' in prompt
    assert "today/largest_category" in prompt
    assert "report_period" in prompt
    assert "report_focus" in prompt
    assert "A question about already stored expenses" in prompt
    assert "сколько я потратил вчера" in prompt
    assert "always kind=report" in prompt
    assert "Never turn an ordinary non-financial conversation into an operation" in prompt


def test_interpretation_supports_yesterday_report_period() -> None:
    result = ExpenseInterpretation(
        kind="report",
        report_period="yesterday",
        report_focus="summary",
        items=[],
        overall_confidence=0.99,
    )

    assert result.report_period == "yesterday"
