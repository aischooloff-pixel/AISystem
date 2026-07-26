"""Интеграционные тесты: реальный Dispatcher + все роутеры + middleware.

Апдейты проходят весь конвейер aiogram (create_dispatcher → фильтры →
DI-инъекция config/bot/state) — ловят ошибки связки, невидимые юнит-тестам:
порядок роутеров, конфликтующие фильтры, имена инъектируемых параметров.
Наружу ничего не уходит: у бота поддельная сессия, Airtable/AI — фейки.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User

from bot import texts

from bot.services import ai as ai_module
from tests.test_block6_qualification import CRM, FakeAI, config  # noqa: F401

CLIENT_ID = 777001
ADMIN_ID = 999


class RecordingSession(BaseSession):
    """Сессия Telegram, которая ничего не шлёт, но всё записывает."""

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[int, str]] = []

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        if isinstance(method, SendMessage):
            self.sent.append((int(method.chat_id), method.text))
            return Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
            )
        return True  # answerCallbackQuery, editMessageText и пр.

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def close(self):
        pass


def make_update(user_id: int, text: str, update_id: int = 1) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Анна", username="anna_x")
    chat = Chat(id=user_id, type="private")
    message = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


@pytest.fixture
def bot_and_dp(config):
    from tests.conftest import get_shared_dispatcher

    dp = get_shared_dispatcher(config)
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    return bot, dp, session


async def test_full_start_flow_via_dispatcher(monkeypatch, bot_and_dp, config):
    """/start site проходит весь стек: middleware → фильтры → приветствие."""
    bot, dp, session = bot_and_dp
    CRM(monkeypatch, fields={"paused": False})

    # Новый клиент: find вернёт None
    from bot.services import airtable

    async def find_none(tid):
        return True, None

    monkeypatch.setattr(airtable, "find_contact_checked", find_none)

    await dp.feed_update(bot, make_update(CLIENT_ID, "/start site"))
    assert session.sent == [(CLIENT_ID, texts.GREETING)]


async def test_first_message_reaches_qualification_router(monkeypatch, bot_and_dp, config):
    """Сообщение клиента доходит до FSM-хендлера с корректной DI."""
    bot, dp, session = bot_and_dp
    CRM(monkeypatch)
    fake = FakeAI()
    fake.scenarios = [{"scenario": "B_problem", "confidence": 91, "reason": "проблема"}]
    monkeypatch.setattr(ai_module, "_service", fake)

    from aiogram.fsm.storage.base import StorageKey

    from bot.states import Dialog

    key = StorageKey(bot_id=bot.id, chat_id=CLIENT_ID, user_id=CLIENT_ID)
    await dp.storage.set_state(key, Dialog.waiting_first_message.state)

    await dp.feed_update(bot, make_update(CLIENT_ID, "У меня всё повторяется"))
    assert session.sent == [(CLIENT_ID, texts.QUESTION_2)]


async def test_paused_client_stopped_by_middleware(monkeypatch, bot_and_dp, config):
    """Middleware перехватывает переданного клиента ДО всех роутеров."""
    bot, dp, session = bot_and_dp
    crm = CRM(monkeypatch, fields={"paused": True, "name": "Анна"})
    fake = FakeAI()  # без ответов: любой вызов AI уронил бы тест
    monkeypatch.setattr(ai_module, "_service", fake)

    await dp.feed_update(bot, make_update(CLIENT_ID, "Есть новости?"))

    texts_sent = [text for _, text in session.sent]
    assert texts.ALREADY_WITH_YULIA in texts_sent  # клиенту — один раз
    assert any("написал(а)" in t for t in texts_sent)  # пересылка Юлии
    assert fake.qualify_calls == []  # AI не запускался


async def test_admin_command_via_dispatcher(monkeypatch, bot_and_dp, config):
    """/stats от Юлии работает; здесь же ловятся ошибки фильтра _AdminOrIdle."""
    bot, dp, session = bot_and_dp
    CRM(monkeypatch)

    from bot.services import airtable

    async def by_status(status):
        return [{"id": "r1", "fields": {"status": status}}] if status == "hot" else []

    monkeypatch.setattr(airtable, "get_contacts_by_status", by_status)

    await dp.feed_update(bot, make_update(ADMIN_ID, "/stats"))
    assert session.sent and "Всего контактов" in session.sent[0][1]


async def test_client_slash_message_mid_dialog_not_hijacked(monkeypatch, bot_and_dp, config):
    """Клиент в диалоге пишет «/stop» — это ответ FSM, а не admin-команда."""
    bot, dp, session = bot_and_dp
    CRM(monkeypatch)
    fake = FakeAI()
    from tests.test_block6_qualification import valid_qualification

    fake.qualifications = [valid_qualification(confidence=60)]
    monkeypatch.setattr(ai_module, "_service", fake)

    from aiogram.fsm.storage.base import StorageKey

    from bot.states import Dialog

    key = StorageKey(bot_id=bot.id, chat_id=CLIENT_ID, user_id=CLIENT_ID)
    await dp.storage.set_state(key, Dialog.b_question_2.state)

    await dp.feed_update(bot, make_update(CLIENT_ID, "/stop"))
    texts_sent = [text for _, text in session.sent]
    assert "Команда недоступна." not in texts_sent
    assert texts.QUESTION_3 in texts_sent  # обработано как ответ на вопрос


async def test_unknown_user_no_state_recovers(monkeypatch, bot_and_dp, config):
    """Клиент без /start и без состояния: восстановление, а не тишина."""
    bot, dp, session = bot_and_dp
    CRM(monkeypatch)
    fake = FakeAI()
    fake.scenarios = [{"scenario": "C_info", "confidence": 90, "reason": "вопрос"}]
    fake.answers = [{"answer": "Диагностика — это...", "needs_yulia": False, "reason": None}]
    monkeypatch.setattr(ai_module, "_service", fake)

    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, chat_id=CLIENT_ID, user_id=CLIENT_ID)
    await dp.storage.set_state(key, None)  # диспетчер общий — чистим состояние
    await dp.storage.set_data(key, {})

    await dp.feed_update(bot, make_update(CLIENT_ID, "Что такое диагностика?"))
    assert session.sent == [(CLIENT_ID, "Диагностика — это...")]
