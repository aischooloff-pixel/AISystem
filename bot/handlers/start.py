"""Обработчик ``/start``.

Блок 1: заглушка — приветствие с обязательным представлением AI-помощником.
Полная логика точки входа (deep links, определение источника, создание контакта,
маршрутизация, дедупликация) реализуется в Блоке 5.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot import texts
from bot.utils.logger import get_app_logger

logger = get_app_logger()

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Отвечает приветствием (Конституция, принцип 1: человек всегда понимает,
    что общается с автоматизированным помощником)."""
    user_id = message.from_user.id if message.from_user else "unknown"
    logger.info("Входящее /start от telegram_id=%s", user_id)
    try:
        await message.answer(texts.GREETING)
    except Exception:
        # Ни одна ошибка не роняет бота (ТЗ, Часть 7)
        logger.exception("Не удалось отправить приветствие telegram_id=%s", user_id)
