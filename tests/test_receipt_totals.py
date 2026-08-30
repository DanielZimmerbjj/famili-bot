from decimal import Decimal

import pytest

from family_bot.services.receipt_ai import ExtractedItem
from family_bot.services.receipts import (
    ReceiptValidationError,
    allocate_receipt_total,
    prepare_chargeable_items,
)


def item(name: str, total: str) -> ExtractedItem:
    return ExtractedItem(
        raw_name=name,
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
        quantity=Decimal("1"),
        unit_price=Decimal("35"),
        line_total=Decimal("35"),
        category_key="unknown",
        subcategory_key=None,
        confidence=0.62,
    )
    freebie = ExtractedItem(
        raw_name="ของแถม",
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
    assert uncertain == ["เนสท์เล่ ลาเต้"]


def test_rejects_unreconciled_total() -> None:
    with pytest.raises(ReceiptValidationError):
        allocate_receipt_total(
            [item("Еда", "100")],
            total=Decimal("150"),
            discount=Decimal("0"),
            tax=Decimal("0"),
        )
