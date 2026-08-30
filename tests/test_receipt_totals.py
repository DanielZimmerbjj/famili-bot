from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from family_bot.models import ReceiptItem
from family_bot.services.receipt_ai import ExtractedItem
from family_bot.services.receipts import (
    ReceiptValidationError,
    allocate_receipt_total,
    extraction_decimal,
    is_seven_eleven_merchant,
    normalize_purchased_at,
    prepare_chargeable_items,
    prepare_receipt_items,
    receipt_adjustment_lines,
)


def item(name: str, total: str, name_ru: str | None = None) -> ExtractedItem:
    return ExtractedItem(
        raw_name=name,
        name_ru=name_ru or name,
        quantity=Decimal("1"),
        unit_price=Decimal(total),
        line_total=Decimal(total),
        category_key="groceries_household",
        subcategory_key="groceries",
        confidence=0.99,
    )


def test_allocates_global_discount_proportionally() -> None:
    allocations = allocate_receipt_total(
        [item("Мясо", "600"), item("Химия", "400")],
        total=Decimal("900"),
        discount=Decimal("100"),
        tax=Decimal("0"),
    )
    assert allocations == [Decimal("540.00000000"), Decimal("360.00000000")]
    assert sum(allocations) == Decimal("900")


def test_accepts_tax_in_final_total() -> None:
    allocations = allocate_receipt_total(
        [item("Еда", "100")],
        total=Decimal("107"),
        discount=Decimal("0"),
        tax=Decimal("7"),
    )
    assert allocations == [Decimal("107.00000000")]


def test_zero_value_promo_line_does_not_change_allocation() -> None:
    allocations = allocate_receipt_total(
        [item("Еда", "100"), item("Подарок по акции", "0")],
        total=Decimal("100"),
        discount=Decimal("0"),
        tax=Decimal("0"),
    )
    assert allocations == [Decimal("100.00000000"), Decimal("0E-8")]


def test_unknown_paid_item_falls_back_to_buffer_without_blocking_receipt() -> None:
    unknown = ExtractedItem(
        raw_name="เนสท์เล่ ลาเต้",
        name_ru="Кофе Nestle Latte",
        quantity=Decimal("1"),
        unit_price=Decimal("35"),
        line_total=Decimal("35"),
        category_key="unknown",
        subcategory_key=None,
        confidence=0.62,
    )
    freebie = ExtractedItem(
        raw_name="ของแถม",
        name_ru="Подарок по акции",
        quantity=Decimal("1"),
        unit_price=Decimal("0"),
        line_total=Decimal("0"),
        category_key="unknown",
        subcategory_key=None,
        confidence=0.4,
    )

    prepared, uncertain = prepare_chargeable_items(
        [unknown, freebie],
        categories={"buffer": object()},  # type: ignore[dict-item]
        subcategories={},
        confidence_threshold=0.75,
    )

    assert len(prepared) == 1
    assert prepared[0].category_key == "buffer"
    assert uncertain == ["Кофе Nestle Latte"]


def test_seven_eleven_receipt_uses_only_dedicated_category() -> None:
    groceries = item("Молоко", "35")
    meat = item("Сосиски", "65").model_copy(
        update={"category_key": "meat", "subcategory_key": None, "confidence": 0.4}
    )
    freebie = item("Подарок", "0")

    prepared, uncertain = prepare_receipt_items(
        [groceries, meat, freebie],
        "CP ALL, 7-Eleven",
        categories={"seven_eleven": object()},  # type: ignore[dict-item]
        subcategories={},
        confidence_threshold=0.75,
    )

    assert len(prepared) == 2
    assert {prepared_item.category_key for prepared_item in prepared} == {"seven_eleven"}
    assert all(prepared_item.subcategory_key is None for prepared_item in prepared)
    assert uncertain == []


def test_exact_124_baht_seven_eleven_receipt_posts_seven_paid_rows() -> None:
    source_items = [
        item("เนสท์เล่ ลาเต้", "15", "Кофе Nestle Latte"),
        item("แซนวิชครีมซองเห็ด", "35", "Сэндвич с грибным кремом"),
        item("Hกระเทียมปรุงรส", "15", "Снек со вкусом чеснока"),
        item("หมากฝรั่ง", "15", "Жевательная резинка"),
        item("เดลฟี่คุกกี้ช็อกโกแลต", "24", "Шоколадное печенье Delphi"),
        item("เดลฟี่คุกกี้ช็อกโกแลต", "24", "Шоколадное печенье Delphi"),
        item("H.เกลือ", "10", "Соль"),
        item("แสตมป์ 1 บาท", "0", "Акционная марка 1 бат"),
    ]

    prepared, uncertain = prepare_receipt_items(
        source_items,
        "CP ALL, 7-Eleven",
        categories={"seven_eleven": object()},  # type: ignore[dict-item]
        subcategories={},
        confidence_threshold=0.75,
    )
    allocations = allocate_receipt_total(
        prepared,
        total=Decimal("124"),
        discount=Decimal("14"),
        tax=Decimal("0"),
    )

    assert len(prepared) == 7
    assert uncertain == []
    assert sum(allocations) == Decimal("124.00000000")
    assert [prepared_item.line_total for prepared_item in prepared] == [
        Decimal("15"),
        Decimal("35"),
        Decimal("15"),
        Decimal("15"),
        Decimal("24"),
        Decimal("24"),
        Decimal("10"),
    ]
    assert allocations != [prepared_item.line_total for prepared_item in prepared]
    assert {prepared_item.category_key for prepared_item in prepared} == {"seven_eleven"}


def test_reads_receipt_adjustments_from_persisted_extraction_safely() -> None:
    extraction = {"discount": "14.00", "tax": "0", "broken": "not-a-number"}

    assert extraction_decimal(extraction, "discount") == Decimal("14.00")
    assert extraction_decimal(extraction, "tax") == Decimal("0")
    assert extraction_decimal(extraction, "broken") == Decimal("0")
    assert extraction_decimal(None, "discount") == Decimal("0")


def test_exact_receipt_shows_printed_subtotal_and_discount_separately() -> None:
    printed_prices = ("15", "35", "15", "15", "24", "24", "10")
    allocated_prices = ("13.48", "31.45", "13.48", "13.48", "21.57", "21.57", "8.99")
    stored_items = [
        ReceiptItem(
            raw_name=f"item {index}",
            printed_line_total=Decimal(printed),
            line_total=Decimal(allocated),
        )
        for index, (printed, allocated) in enumerate(
            zip(printed_prices, allocated_prices, strict=True), 1
        )
    ]

    assert receipt_adjustment_lines(
        stored_items,
        Decimal("124"),
        "THB",
        {"discount": "14", "tax": "0"},
    ) == [
        "Сумма товаров: <b>138 THB</b>",
        "Скидка по чеку: <b>−14 THB</b>",
    ]


@pytest.mark.parametrize(
    "merchant",
    ("7-Eleven", "7 Eleven Thailand", "7-11", "CP ALL Public Company", "เซเว่น อีเลฟเว่น"),
)
def test_recognizes_seven_eleven_merchant_variants(merchant: str) -> None:
    assert is_seven_eleven_merchant(merchant)


def test_rejects_unreconciled_total() -> None:
    with pytest.raises(ReceiptValidationError):
        allocate_receipt_total(
            [item("Еда", "100")],
            total=Decimal("150"),
            discount=Decimal("0"),
            tax=Decimal("0"),
        )


def test_invalid_ocr_date_falls_back_to_receipt_upload_time() -> None:
    timezone = ZoneInfo("Asia/Bangkok")
    received_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone)

    normalized = normalize_purchased_at(
        datetime(3008, 3, 30, 0, 0),
        received_at,
        timezone,
    )

    assert normalized == received_at


def test_thai_buddhist_year_is_converted_when_date_is_plausible() -> None:
    timezone = ZoneInfo("Asia/Bangkok")
    received_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone)

    normalized = normalize_purchased_at(
        datetime(2569, 8, 30, 10, 15),
        received_at,
        timezone,
    )

    assert normalized == datetime(2026, 8, 30, 10, 15, tzinfo=timezone)
