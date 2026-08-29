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
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите расход, доход или отправьте чек…",
    )


def cycle_close_keyboard(cycle_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚙 Добавить к машине",
                    callback_data=f"cycle:rollover:{cycle_id}",
                ),
                InlineKeyboardButton(
                    text="🧰 Оставить резервом",
                    callback_data=f"cycle:keep:{cycle_id}",
                ),
            ]
        ]
    )
