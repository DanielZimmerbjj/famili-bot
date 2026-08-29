from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
