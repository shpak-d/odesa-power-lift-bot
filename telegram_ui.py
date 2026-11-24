"""Inline navigation keeps the chat clean and allows editing the same post."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def navigation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сьогодні", callback_data="nav:today"),
                InlineKeyboardButton(text="Завтра", callback_data="nav:tomorrow"),
            ],
            [
                InlineKeyboardButton(text="Ліфт", callback_data="nav:lift"),
                InlineKeyboardButton(text="Стан моніторингу", callback_data="nav:status"),
            ],
            [
                InlineKeyboardButton(text="Перевірити бота", callback_data="nav:ping"),
                InlineKeyboardButton(text="Допомога", callback_data="nav:help"),
            ],
        ],
    )
