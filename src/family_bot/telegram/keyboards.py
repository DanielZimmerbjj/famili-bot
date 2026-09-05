from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🧾 Сегодня")],
            [KeyboardButton(text="🎯 Накопления"), KeyboardButton(text="📊 График")],
            [KeyboardButton(text="💵 Доходы"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="🔒 Закрыть месяц")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите расход, доход или отправьте чек…",
    )


def _callback_token(value: str) -> str:
    return value.replace("-", "")[:8]


def cycle_close_keyboard(
    cycle_id: str,
    goals: list[tuple[str, str, str]],
    *,
    has_remainder: bool = True,
) -> InlineKeyboardMarkup:
    cycle_token = _callback_token(cycle_id)
    rows: list[list[InlineKeyboardButton]] = []
    if has_remainder:
        rows.extend(
            [
                InlineKeyboardButton(
                    text=f"{icon} Переложить всё в «{name}»",
                    callback_data=(
                        f"cycle:goal:{cycle_token}:{_callback_token(goal_id)}"
                    ),
                )
            ]
            for goal_id, name, icon in goals
        )
    keep_label = (
        "🧰 Оставить остаток резервом"
        if has_remainder
        else "✅ Закрыть месяц без переноса"
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=keep_label,
                callback_data=f"cycle:keep:{cycle_token}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ Не закрывать",
                callback_data=f"cycle:cancel:{cycle_token}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
