"""Shared Telegram navigation attached to existing messages."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def navigation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сьогодні"), KeyboardButton(text="Завтра")],
            [KeyboardButton(text="Ліфт"), KeyboardButton(text="Стан моніторингу")],
            [KeyboardButton(text="Перевірити бота"), KeyboardButton(text="Допомога")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію або введіть /menu",
    )
