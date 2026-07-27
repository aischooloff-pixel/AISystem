"""Middleware полной остановки автоматики (Блок 7).

ТЗ Юлии, п. 6: «После передачи автоматическая коммуникация с человеком
останавливается, пока я вручную не выберу новый маршрут».

Выполняется до всех хендлеров. Для клиента с ``paused`` или
``assigned_to=yulia``: сообщение сохраняется (история + касание), Юлия
уведомляется, клиент один раз получает «Юлия уже знает...», обработка
прерывается — AI не запускается.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from bot import texts
from bot.config import Config
from bot.services import airtable
from bot.services.notifier import notify_yulia
from bot.states import QUESTIONNAIRE_STATE_PREFIX
from bot.utils.helpers import automation_stopped
from bot.utils.logger import get_app_logger

logger = get_app_logger()


class PauseCheckMiddleware(BaseMiddleware):
    """Останавливает автоматику для переданных Юлии / поставленных на паузу."""

    def __init__(self, config: Config) -> None:
        self.config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        # Только личные сообщения клиентов; Юлия и группы проходят насквозь
        if event.chat.type != "private" or event.from_user is None:
            return await handler(event, data)
        if event.from_user.id == self.config.telegram_admin_id:
            return await handler(event, data)

        # Анкета «Точка сбоя» (Блок 11) — исключение: её запускает сама Юлия
        # уже после передачи клиента, когда автоматика остановлена. Без этого
        # ответы на вопросы анкеты проглатывались бы как обычные сообщения
        # переданного клиента. Диалог с AI при этом НЕ возобновляется:
        # хендлер анкеты только собирает ответы.
        state = data.get("state")
        if state is not None:
            current_state = await state.get_state()
            if current_state and current_state.startswith(QUESTIONNAIRE_STATE_PREFIX):
                return await handler(event, data)

        ok, contact = await airtable.find_contact_checked(event.from_user.id)
        if not ok or contact is None:
            # Сбой Airtable или новый клиент — обычная обработка
            # (хендлеры сами защищаются от сбоев)
            return await handler(event, data)
        fields = contact.get("fields", {})
        if not automation_stopped(fields):
            return await handler(event, data)

        # Клиент у Юлии: сообщение не теряется, AI не отвечает
        text = event.text or event.caption or f"<{event.content_type.value}>"
        await self._store_message(contact, text)
        await airtable.add_touch(
            event.from_user.id,
            "dm_start",
            "Сообщение от переданного клиента (автоматика остановлена)",
            raw_content=text[:1000],
        )
        bot = data.get("bot")
        if bot is not None:
            name = fields.get("name") or event.from_user.full_name
            await notify_yulia(
                bot, self.config.telegram_admin_id, f"{name} написал(а): {text[:1000]}"
            )
        # «Ответить один раз»: повторные сообщения сохраняем молча
        already_notified = False
        if state is not None:
            state_data = await state.get_data()
            already_notified = bool(state_data.get("paused_notice_sent"))
        if not already_notified:
            try:
                await event.answer(texts.ALREADY_WITH_YULIA)
            except Exception:
                logger.exception("Не удалось ответить переданному клиенту")
            if state is not None:
                await state.update_data(paused_notice_sent=True)
        logger.info(
            "pause_check: сообщение клиента %s сохранено, автоматика не запущена",
            event.from_user.id,
        )
        return None  # обработка прервана

    @staticmethod
    async def _store_message(contact: dict, text: str) -> None:
        raw = contact.get("fields", {}).get("conversation_history") or "[]"
        try:
            history = json.loads(raw)
            if not isinstance(history, list):
                history = []
        except (json.JSONDecodeError, TypeError):
            history = []
        history.append(
            {
                "role": "client",
                "text": text,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        await airtable.update_contact(
            contact["id"], {"conversation_history": json.dumps(history, ensure_ascii=False)}
        )
