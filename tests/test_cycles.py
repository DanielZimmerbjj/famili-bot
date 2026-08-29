from datetime import date

from family_bot.services.cycles import cycle_dates


def test_cycle_after_salary_day() -> None:
    assert cycle_dates(date(2026, 8, 29), 5) == (
        date(2026, 8, 5),
        date(2026, 9, 4),
    )


def test_cycle_before_salary_day() -> None:
    assert cycle_dates(date(2026, 9, 2), 5) == (
        date(2026, 8, 5),
        date(2026, 9, 4),
    )


def test_first_day_cycle_supported() -> None:
    assert cycle_dates(date(2026, 9, 1), 1) == (
        date(2026, 9, 1),
        date(2026, 9, 30),
    )
