"""Inline-клавиатуры для Юлии: карточка клиента и комментарии (Блоки 7–8)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ADMIN_CALLBACK_PREFIX = "adm:"
COMMENT_CALLBACK_PREFIX = "cmt:"


def client_card_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Кнопки карточки клиента (ТЗ, Блок 7): Беру / Прогрев / Нецелевой / Пауза."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Беру", callback_data=f"adm:take:{telegram_id}"),
                InlineKeyboardButton(text="🔄 Прогрев", callback_data=f"adm:nurture:{telegram_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="⛔ Нецелевой", callback_data=f"adm:reject:{telegram_id}"
                ),
                InlineKeyboardButton(text="⏸ Пауза", callback_data=f"adm:pause:{telegram_id}"),
            ],
        ]
    )


def comment_keyboard(record_id: str) -> InlineKeyboardMarkup:
    """Кнопки уведомления о комментарии (ТЗ, Блок 8):
    Опубликовать / Изменить / Пропустить. ``record_id`` — запись в Comments."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"cmt:pub:{record_id}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"cmt:edit:{record_id}"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"cmt:skip:{record_id}"),
            ]
        ]
    )
