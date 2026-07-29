"""Тесты Блока 7: кнопки карточки, admin-команды, middleware pause_check.

Критерии: все 4 кнопки работают и обновляют CRM; после нажатия сообщение
редактируется; admin-команды недоступны обычным пользователям; middleware
полностью останавливает автоматику; сообщения переданных клиентов
сохраняются и пересылаются Юлии.
"""

from __future__ import annotations

import json

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from bot import texts
from bot.handlers import admin as admin_handlers
from bot.handlers import callbacks as cb_handlers
from bot.middlewares.pause_check import PauseCheckMiddleware
from bot.services import airtable
from tests.test_block6_qualification import CRM, FakeBot, config  # noqa: F401

ADMIN_ID = 999
CLIENT_ID = 777001


class FakeMessage:
    def __init__(self, user_id: int, text: str, chat_type: str = "private"):
        self.from_user = User(id=user_id, is_bot=False, first_name="X")
        self.text = text
        self.caption = None
        self.content_type = "text"
        self.chat = type("Chat", (), {"type": chat_type})()
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeCardMessage:
    def __init__(self):
        self.text = "КАРТОЧКА"
        self.edited: list[tuple[str, dict]] = []

    async def edit_text(self, text, **kwargs):
        self.edited.append((text, kwargs))


class FakeCallback:
    def __init__(self, user_id: int, data: str):
        self.from_user = User(id=user_id, is_bot=False, first_name="Юлия")
        self.data = data
        self.message = FakeCardMessage()
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def make_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CLIENT_ID, user_id=CLIENT_ID)
    )


# ── Кнопки карточки ──


async def test_take_button(monkeypatch, config):
    crm = CRM(monkeypatch, fields={"status": "hot"})
    bot = FakeBot()
    callback = FakeCallback(ADMIN_ID, f"adm:take:{CLIENT_ID}")

    await cb_handlers.admin_card_action(callback, bot, config)

    fields = crm.contact["fields"]
    assert fields["assigned_to"] == "yulia" and fields["paused"] is True
    assert fields["handoff_date"]
    assert any("Связаться с" in t for t in crm.tasks)
    assert bot.sent == [(CLIENT_ID, texts.YULIA_WILL_CONTACT)]
    # Сообщение отредактировано: отметка с датой, кнопки убраны
    text, kwargs = callback.message.edited[0]
    assert "✅ Взято в работу ·" in text
    assert kwargs["reply_markup"] is None


async def test_nurture_button(monkeypatch, config):
    crm = CRM(monkeypatch, fields={"status": "hot"})
    bot = FakeBot()
    callback = FakeCallback(ADMIN_ID, f"adm:nurture:{CLIENT_ID}")

    await cb_handlers.admin_card_action(callback, bot, config)

    fields = crm.contact["fields"]
    assert fields["status"] == "warm"
    assert fields["paused"] is False and fields["assigned_to"] == "ai"
    # Запись в истории с by=yulia
    history = json.loads(fields["status_history"])
    assert history[-1]["by"] == "yulia"
    assert bot.sent == []


async def test_reject_button(monkeypatch, config):
    crm = CRM(monkeypatch, fields={"status": "warm"})
    bot = FakeBot()
    callback = FakeCallback(ADMIN_ID, f"adm:reject:{CLIENT_ID}")

    await cb_handlers.admin_card_action(callback, bot, config)

    fields = crm.contact["fields"]
    assert fields["status"] == "non_target"
    assert fields["paused"] is True and fields["result"] == "declined"
    assert bot.sent == [(CLIENT_ID, texts.NON_TARGET_CLOSING)]


async def test_pause_button_sends_nothing_to_client(monkeypatch, config):
    crm = CRM(monkeypatch)
    bot = FakeBot()
    callback = FakeCallback(ADMIN_ID, f"adm:pause:{CLIENT_ID}")

    await cb_handlers.admin_card_action(callback, bot, config)

    assert crm.contact["fields"]["paused"] is True
    assert bot.sent == []  # клиенту ничего не отправляется (ТЗ)


async def test_buttons_rejected_for_non_admin(monkeypatch, config):
    crm = CRM(monkeypatch)
    bot = FakeBot()
    callback = FakeCallback(12345, f"adm:take:{CLIENT_ID}")

    await cb_handlers.admin_card_action(callback, bot, config)

    assert callback.answers == ["Недоступно"]
    assert crm.updates == [] and crm.tasks == []
    assert callback.message.edited == []


# ── Admin-команды ──


async def test_admin_command_denied_for_regular_user(monkeypatch, config):
    CRM(monkeypatch)
    message = FakeMessage(12345, "/stats")
    await admin_handlers.cmd_stats(message, config)
    assert message.sent == ["Команда недоступна."]


async def test_status_command(monkeypatch, config):
    crm = CRM(monkeypatch, fields={"status": "warm"})
    message = FakeMessage(ADMIN_ID, f"/status {CLIENT_ID} hot")
    await admin_handlers.cmd_status(message, config)
    assert crm.contact["fields"]["status"] == "hot"
    assert crm.status_changes[0][3] == "yulia"
    assert "warm → hot" in message.sent[0]


async def test_status_command_validates_value(monkeypatch, config):
    CRM(monkeypatch)
    message = FakeMessage(ADMIN_ID, f"/status {CLIENT_ID} vip")
    await admin_handlers.cmd_status(message, config)
    assert "Формат" in message.sent[0]


async def test_pause_resume_commands(monkeypatch, config):
    crm = CRM(monkeypatch)
    await admin_handlers.cmd_pause(FakeMessage(ADMIN_ID, f"/pause {CLIENT_ID}"), config)
    assert crm.contact["fields"]["paused"] is True
    await admin_handlers.cmd_resume(FakeMessage(ADMIN_ID, f"/resume {CLIENT_ID}"), config)
    assert crm.contact["fields"]["paused"] is False


async def test_note_command_appends(monkeypatch, config):
    crm = CRM(monkeypatch, fields={"notes": "старая заметка"})
    message = FakeMessage(ADMIN_ID, f"/note {CLIENT_ID} перезвонить в среду")
    await admin_handlers.cmd_note(message, config)
    notes = crm.contact["fields"]["notes"]
    assert "старая заметка" in notes and "перезвонить в среду" in notes


async def test_info_command_builds_card(monkeypatch, config):
    CRM(
        monkeypatch,
        fields={
            "status": "warm",
            "request_summary": "Повторяющиеся конфликты",
            "ai_confidence": 90,
        },
    )
    message = FakeMessage(ADMIN_ID, f"/info {CLIENT_ID}")
    await admin_handlers.cmd_info(message, config)
    card = message.sent[0]
    assert "Повторяющиеся конфликты" in card
    assert "ИСТОРИЯ КАСАНИЙ" in card


async def test_contact_not_found_reported(monkeypatch, config):
    crm = CRM(monkeypatch)

    async def find_none(tid):
        return True, None

    monkeypatch.setattr(airtable, "find_contact_checked", find_none)
    message = FakeMessage(ADMIN_ID, "/pause 555")
    await admin_handlers.cmd_pause(message, config)
    assert "не найден" in message.sent[0]


# ── Middleware pause_check ──


async def make_middleware_call(monkeypatch, config, crm_fields, user_id=CLIENT_ID):
    from bot.middlewares import pause_check as pause_module

    monkeypatch.setattr(pause_module, "Message", FakeMessage)
    crm = CRM(monkeypatch, fields=crm_fields)
    middleware = PauseCheckMiddleware(config)
    bot = FakeBot()
    message = FakeMessage(user_id, "Здравствуйте, есть новости?")
    state = make_state()
    handler_called = []

    async def handler(event, data):
        handler_called.append(True)
        return "handled"

    result = await middleware(handler, message, {"bot": bot, "state": state})
    return crm, bot, message, state, handler_called, result


async def test_middleware_stops_automation_for_paused(monkeypatch, config):
    """Критерий: middleware останавливает квалификацию; сообщение сохраняется
    и пересылается Юлии.

    AI-сервис здесь не поднят — справочный ответ получить неоткуда, и бот
    честно отвечает «Юлия уже знает». Заодно проверяется, что недоступность
    модели не роняет обработку сообщения переданного клиента.
    """
    crm, bot, message, state, handler_called, result = await make_middleware_call(
        monkeypatch, config, {"paused": True, "name": "Анна"}
    )
    assert handler_called == []  # квалификация не запускалась
    assert message.sent == [texts.ALREADY_WITH_YULIA]
    history = json.loads(crm.contact["fields"]["conversation_history"])
    assert [turn["text"] for turn in history] == [
        "Здравствуйте, есть новости?",
        texts.ALREADY_WITH_YULIA,
    ], "переписка после передачи пишется не целиком"
    assert any(t[0] == "dm_start" for t in crm.touches)
    assert bot.sent and "Анна написал(а)" in bot.sent[0][1]


async def test_middleware_replies_only_once(monkeypatch, config):
    from bot.middlewares import pause_check as pause_module

    monkeypatch.setattr(pause_module, "Message", FakeMessage)
    crm = CRM(monkeypatch, fields={"assigned_to": "yulia"})
    middleware = PauseCheckMiddleware(config)
    bot = FakeBot()
    state = make_state()

    async def handler(event, data):
        return "handled"

    m1 = FakeMessage(CLIENT_ID, "Первое")
    await middleware(handler, m1, {"bot": bot, "state": state})
    m2 = FakeMessage(CLIENT_ID, "Второе")
    await middleware(handler, m2, {"bot": bot, "state": state})

    # «Юлия уже знает» — один раз; дальше подтверждаем приём дополнений,
    # но передачу не повторяем (решение Юлии 2026-07-29)
    assert m1.sent == [texts.ALREADY_WITH_YULIA]
    assert m2.sent == [texts.INFO_PASSED_TO_YULIA]
    assert texts.HANDOFF_MESSAGE not in m2.sent
    # Оба сообщения сохранены вместе с ответами и пересланы Юлии
    history = json.loads(crm.contact["fields"]["conversation_history"])
    assert [turn["role"] for turn in history] == ["client", "bot", "client", "bot"]
    assert len(bot.sent) == 2


async def test_middleware_passes_active_clients(monkeypatch, config):
    _, _, message, _, handler_called, result = await make_middleware_call(
        monkeypatch, config, {"paused": False, "assigned_to": "ai"}
    )
    assert handler_called == [True]
    assert result == "handled"
    assert message.sent == []


async def test_middleware_passes_admin(monkeypatch, config):
    _, _, message, _, handler_called, _ = await make_middleware_call(
        monkeypatch, config, {"paused": True}, user_id=ADMIN_ID
    )
    assert handler_called == [True]
    assert message.sent == []
