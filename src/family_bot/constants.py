from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BudgetTemplate:
    key: str
    name: str
    icon: str
    limit_thb: Decimal
    subcategories: tuple[str, ...] = ()


BUDGET_TEMPLATES: tuple[BudgetTemplate, ...] = (
    BudgetTemplate("rent", "Квартира", "🏠", Decimal("13500")),
    BudgetTemplate("utilities", "Коммуналка", "💡", Decimal("3500")),
    BudgetTemplate("meat", "Мясо и основной закуп", "🥩", Decimal("5000")),
    BudgetTemplate(
        "groceries_household",
        "Продукты и бытовая химия",
        "🛒",
        Decimal("10000"),
        ("groceries", "household"),
    ),
    BudgetTemplate("fruit", "Фрукты", "🍉", Decimal("2000")),
    BudgetTemplate(
        "training",
        "Тренировки и такси до них",
        "🥋",
        Decimal("7200"),
        ("workout", "training_taxi"),
    ),
    BudgetTemplate("transport", "Остальное такси", "🚕", Decimal("2000")),
    BudgetTemplate("cafe", "Кафе", "🍽", Decimal("3000")),
    BudgetTemplate(
        "baby",
        "Питание и памперсы младенцу",
        "🍼",
        Decimal("1500"),
        ("baby_formula", "diapers", "baby_misc"),
    ),
    BudgetTemplate("pharmacy", "Аптека", "💊", Decimal("2000")),
    BudgetTemplate("kids", "Одежда и игрушки детям", "🧸", Decimal("1500")),
    BudgetTemplate("mobile", "Две SIM-карты", "📱", Decimal("400")),
    BudgetTemplate("wife_care", "Ногти и педикюр", "💅", Decimal("1000")),
    BudgetTemplate("haircut", "Стрижка взрослого", "💇", Decimal("500")),
    BudgetTemplate("child_haircut", "Стрижка ребенка", "✂️", Decimal("200")),
    BudgetTemplate("supplements", "Спортивные БАДы", "🏋️", Decimal("2000")),
    BudgetTemplate("buffer", "Мелкий непредвиденный запас", "🧯", Decimal("1800")),
)

EXPECTED_INCOMES: tuple[tuple[str, Decimal, str], ...] = (
    ("Казахстанская зарплата №1", Decimal("840000"), "KZT"),
    ("Российская зарплата", Decimal("660000"), "KZT"),
    ("Казахстанская зарплата №2", Decimal("150000"), "KZT"),
)

MANDATORY_MONTHLY_KZT = Decimal("200000")
CAR_TARGET_KZT = Decimal("1200000")
CAR_MONTHLY_TARGET_KZT = Decimal("600000")
BORDER_RUN_TARGET_KZT = Decimal("300000")
BORDER_RUN_MONTHLY_KZT = Decimal("50000")

CURRENCY_ALIASES: dict[str, str] = {
    "₸": "KZT",
    "тенге": "KZT",
    "тг": "KZT",
    "kzt": "KZT",
    "฿": "THB",
    "бат": "THB",
    "бата": "THB",
    "батов": "THB",
    "thb": "THB",
    "₫": "VND",
    "донг": "VND",
    "донга": "VND",
    "донгов": "VND",
    "vnd": "VND",
    "₽": "RUB",
    "рубль": "RUB",
    "рубля": "RUB",
    "рублей": "RUB",
    "rub": "RUB",
    "usd": "USD",
    "доллар": "USD",
    "доллара": "USD",
    "долларов": "USD",
}

CATEGORY_TEXT_ALIASES: dict[str, str] = {
    "квартира": "rent",
    "коммуналка": "utilities",
    "мясо": "meat",
    "продукты": "groceries_household",
    "бытовое": "groceries_household",
    "бытовая химия": "groceries_household",
    "фрукты": "fruit",
    "тренировка": "training",
    "тренировки": "training",
    "такси": "transport",
    "кафе": "cafe",
    "ребенок": "kids",
    "ребёнок": "kids",
    "младенец": "baby",
    "памперсы": "baby",
    "детское питание": "baby",
    "аптека": "pharmacy",
    "лекарства": "pharmacy",
    "симки": "mobile",
    "связь": "mobile",
    "ногти": "wife_care",
    "стрижка": "haircut",
    "бады": "supplements",
}
