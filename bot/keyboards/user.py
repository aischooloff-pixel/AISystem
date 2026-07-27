"""Inline-клавиатуры для клиента (Блок 5).

Кнопки выбора источника — ровно те, что в ТЗ (Блок 5, шаг 4.2):
[Telegram-канал] [Facebook / VK] [Рекомендация] [Сайт] [Другое].
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SOURCE_CALLBACK_PREFIX = "src:"

# Подпись кнопки → значение справочника source (модель данных, ТЗ Часть 3).
# «Facebook / VK» — одна кнопка по ТЗ; пишем facebook, соцсеть уточняется
# в source_detail (решение зафиксировано в logs.txt).
SOURCE_BUTTONS: tuple[tuple[str, str], ...] = (
    ("Telegram-канал", "telegram_channel"),
    ("Facebook / VK", "facebook"),
    ("Рекомендация", "referral"),
    ("Сайт", "site"),
    ("Другое", "other"),
)


def source_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора источника для /start без deep link."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{SOURCE_CALLBACK_PREFIX}{value}")]
            for label, value in SOURCE_BUTTONS
        ]
    )


# Согласие на запись встречи — последний шаг анкеты (ТЗ, Блок 11): [Да] [Нет]
CONSENT_CALLBACK_PREFIX = "consent:"


def consent_keyboard() -> InlineKeyboardMarkup:
    """Кнопки согласия на запись встречи."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=f"{CONSENT_CALLBACK_PREFIX}yes"),
                InlineKeyboardButton(text="Нет", callback_data=f"{CONSENT_CALLBACK_PREFIX}no"),
            ]
        ]
    )
