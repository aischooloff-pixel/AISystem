"""Middleware остановки квалификации после передачи (Блок 7).

ТЗ Юлии, п. 6: «После передачи автоматическая коммуникация с человеком
останавливается, пока я вручную не выберу новый маршрут». Останавливается
именно КВАЛИФИКАЦИЯ: бот больше не ведёт человека по воронке, не меняет
статус и не передаёт его повторно.

Решением Юлии от 2026-07-29 бот при этом остаётся справочным ассистентом:
на организационный вопрос («что такое диагностика», «сколько длится»,
«что взять с собой») он отвечает по базе знаний, пока Юлия не подключилась.
До этого любой вопрос после передачи упирался в «Юлия уже знает о вашем
обращении» и разговор выглядел оборванным.

Выполняется до всех хендлеров. Для клиента с ``paused`` или
``assigned_to=yulia``: сообщение сохраняется (история + касание), Юлия
уведомляется, клиент получает ответ по базе знаний, обработка прерывается —
квалификация не запускается.
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
from bot.services.ai import get_ai
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
        await self._reply_as_assistant(event, contact, text, state)
        logger.info(
            "pause_check: сообщение клиента %s сохранено, квалификация не запущена",
            event.from_user.id,
        )
        return None  # квалификация не запускается

    async def _reply_as_assistant(self, event: Message, contact: dict, text: str, state) -> None:
        """Ответ справочного ассистента переданному клиенту.

        Отвечает по базе знаний и НИЧЕГО больше: статус не меняется,
        квалификация не запускается, передача не повторяется. Сообщение
        «Юлия уже знает о вашем обращении» остаётся, но только один раз
        и только когда отвечать по существу нечего.
        """
        answer_text = texts.ALREADY_WITH_YULIA
        try:
            history = _history_text(contact)
            answer = await get_ai().answer_info(text, history=history)
        except Exception:
            logger.exception("Справочный ответ переданному клиенту не получен")
            answer = None

        if answer is not None and not answer.get("needs_yulia") and answer.get("answer"):
            # Ответ нашёлся в базе знаний — человек получает его сразу,
            # не дожидаясь Юлии
            answer_text = answer["answer"]
        else:
            # Ответа нет: либо это не вопрос, либо базы знаний не хватило.
            # Передавать повторно нечего — клиент уже у Юлии.
            already_notified = False
            if state is not None:
                state_data = await state.get_data()
                already_notified = bool(state_data.get("paused_notice_sent"))
            if already_notified:
                answer_text = texts.INFO_PASSED_TO_YULIA
            elif state is not None:
                await state.update_data(paused_notice_sent=True)

        try:
            await event.answer(answer_text)
        except Exception:
            logger.exception("Не удалось ответить переданному клиенту")
        await self._store_message(contact, answer_text, role="bot")

    @staticmethod
    async def _store_message(contact: dict, text: str, role: str = "client") -> None:
        history = _parse_history(contact)
        history.append(
            {
                "role": role,
                "text": text,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        serialized = json.dumps(history, ensure_ascii=False)
        # Локальную копию тоже обновляем: в одном апдейте пишутся и реплика
        # клиента, и ответ бота, и вторая запись не должна затереть первую
        contact.setdefault("fields", {})["conversation_history"] = serialized
        await airtable.update_contact(contact["id"], {"conversation_history": serialized})


def _parse_history(contact: dict) -> list[dict]:
    raw = contact.get("fields", {}).get("conversation_history") or "[]"
    try:
        history = json.loads(raw)
        return history if isinstance(history, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _history_text(contact: dict) -> str:
    """Последние реплики для контекста справочного ответа."""
    return "\n".join(
        f"{turn.get('role')}: {turn.get('text')}" for turn in _parse_history(contact)[-10:]
    )
