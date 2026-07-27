"""Тесты Блока 5: /start, deep links, источник, маршрутизация.

Критерии готовности: все deep links определяют источник; новый контакт в
Contacts + касание в Touches; повторный /start не создаёт дубль; переданный
Юлии клиент не запускает автоматику; без deep link — кнопки выбора;
приветствие содержит представление AI-помощником.
"""

from __future__ import annotations

import pytest
from aiogram.enums import ContentType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from bot import texts
from bot.handlers import start as start_handler
from bot.keyboards.user import SOURCE_BUTTONS
from bot.services import airtable
from bot.states import Dialog

TID = 424242


class FakeMessage:
    """Минимальный Message: текст, автор, журнал отправленных ответов."""

    def __init__(
        self,
        user: User | None,
        text: str | None = "/start",
        caption: str | None = None,
        content_type: str = "text",
    ) -> None:
        self.from_user = user
        self.text = text
        self.caption = caption
        # Именно enum, как у настоящего Message: у str-заглушки не было .value,
        # и «<ContentType.VOICE>» в CRM проходил мимо тестов
        self.content_type = ContentType(content_type)
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.sent.append((text, kwargs))


class FakeCallback:
    def __init__(self, user: User, data: str, message: FakeMessage) -> None:
        self.from_user = user
        self.data = data
        self.message = message
        self.answered = False
        self.markup_removed = False
        message.edit_reply_markup = self._edit_reply_markup  # type: ignore[attr-defined]

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True

    async def _edit_reply_markup(self, reply_markup=None) -> None:
        self.markup_removed = reply_markup is None


class AirtableCalls:
    """Подменяет функции airtable, записывая вызовы."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, existing: dict | None = None) -> None:
        self.existing = existing
        self.lookup_ok = True  # False = Airtable недоступен при поиске
        self.upserts: list[tuple[int, dict]] = []
        self.touches: list[tuple[int, str, str, dict]] = []
        self.upsert_result: dict | None = {"id": "rec1", "fields": {}}

        async def find_contact(telegram_id: int):
            return self.existing if self.lookup_ok else None

        async def find_contact_checked(telegram_id: int):
            return (True, self.existing) if self.lookup_ok else (False, None)

        async def upsert_contact(telegram_id: int, data: dict):
            self.upserts.append((telegram_id, data))
            return self.upsert_result

        async def add_touch(telegram_id: int, type: str, description: str, **kwargs):
            self.touches.append((telegram_id, type, description, kwargs))
            return {"id": "recT"}

        monkeypatch.setattr(airtable, "find_contact", find_contact)
        monkeypatch.setattr(airtable, "find_contact_checked", find_contact_checked)
        monkeypatch.setattr(airtable, "upsert_contact", upsert_contact)
        monkeypatch.setattr(airtable, "add_touch", add_touch)


def make_user(username: str | None = "anna_example") -> User:
    return User(id=TID, is_bot=False, first_name="Анна", last_name=None, username=username)


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TID, user_id=TID))


# ── Deep links ──


@pytest.mark.parametrize(
    "source",
    [
        "telegram_channel",
        "telegram_comment",
        "facebook",
        "vk",
        "site",
        "referral",
        "professional_group",
        "mastermind",
        "other",
        "qr",  # из полного справочника («Карта пути», п. 2)
    ],
)
async def test_deep_link_sets_source(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Критерий: все deep links корректно определяют источник."""
    calls = AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(), f"/start {source}")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert len(calls.upserts) == 1
    tid, data = calls.upserts[0]
    assert tid == TID
    assert data["source"] == source
    assert data["name"] == "Анна"
    assert data["username"] == "@anna_example"
    assert data["first_action"] == "/start"
    # Касание первого обращения
    assert calls.touches[0][1] == "dm_start"
    assert calls.touches[0][2] == "Первое обращение в бот"
    assert calls.touches[0][3]["source"] == source
    # Приветствие с представлением AI-помощником, FSM ждёт первое сообщение
    assert message.sent[0][0] == texts.GREETING
    assert "AI-помощник" in message.sent[0][0]
    assert await state.get_state() == Dialog.waiting_first_message.state


async def test_no_deep_link_shows_source_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Критерий: без deep link показываются кнопки выбора; контакт ещё
    не создаётся (порядок шагов 4.2 → 4.3 из ТЗ)."""
    calls = AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(), "/start")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert calls.upserts == []
    text, kwargs = message.sent[0]
    assert text == texts.SOURCE_QUESTION
    keyboard = kwargs["reply_markup"].inline_keyboard
    labels = [row[0].text for row in keyboard]
    assert labels == [label for label, _ in SOURCE_BUTTONS]
    assert labels == ["Telegram-канал", "Facebook / VK", "Рекомендация", "Сайт", "Другое"]
    assert await state.get_state() == Dialog.choosing_source.state


async def test_unknown_start_param_falls_back_to_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(), "/start abrakadabra")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert calls.upserts == []
    assert await state.get_state() == Dialog.choosing_source.state
    assert "abrakadabra" in (await state.get_data())["source_detail"]


# ── Кнопки выбора источника ──


async def test_source_button_creates_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = AirtableCalls(monkeypatch, existing=None)
    user = make_user()
    origin = FakeMessage(user, "вопрос")
    callback = FakeCallback(user, "src:site", origin)
    state = make_state()
    await state.set_state(Dialog.choosing_source)

    await start_handler.source_chosen(callback, state)

    assert calls.upserts[0][1]["source"] == "site"
    assert callback.answered and callback.markup_removed
    assert origin.sent[0][0] == texts.GREETING
    assert await state.get_state() == Dialog.waiting_first_message.state


async def test_facebook_vk_button_records_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кнопка «Facebook / VK» одна (по ТЗ) — соцсеть фиксируется в source_detail."""
    calls = AirtableCalls(monkeypatch, existing=None)
    user = make_user()
    callback = FakeCallback(user, "src:facebook", FakeMessage(user))
    state = make_state()
    await state.set_state(Dialog.choosing_source)

    await start_handler.source_chosen(callback, state)

    data = calls.upserts[0][1]
    assert data["source"] == "facebook"
    assert "Facebook / VK" in data["source_detail"]


async def test_text_instead_of_buttons_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Клиент написал текст вместо выбора кнопки — диалог продолжается,
    источник telegram_dm, текст сохранён в касании."""
    calls = AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(), "У меня повторяется одна и та же ситуация")
    state = make_state()
    await state.set_state(Dialog.choosing_source)

    await start_handler.message_instead_of_source(message, state)

    assert calls.upserts[0][1]["source"] == "telegram_dm"
    assert calls.touches[0][3]["raw_content"] == "У меня повторяется одна и та же ситуация"
    assert message.sent[0][0] == texts.GREETING
    assert await state.get_state() == Dialog.waiting_first_message.state


async def test_voice_instead_of_buttons_not_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Голосовое/фото/стикер вместо кнопок — клиент не получает тишину:
    контакт создаётся, тип содержимого фиксируется в касании."""
    calls = AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(), text=None, content_type="voice")
    state = make_state()
    await state.set_state(Dialog.choosing_source)

    await start_handler.message_instead_of_source(message, state)

    assert calls.upserts[0][1]["source"] == "telegram_dm"
    assert calls.touches[0][3]["raw_content"] == "<voice>"
    assert message.sent[0][0] == texts.GREETING
    assert await state.get_state() == Dialog.waiting_first_message.state


# ── Существующий контакт ──


async def test_repeat_start_updates_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий: повторный /start не создаёт дубль — только обновление
    и касание; источник первого касания не переопределяется."""
    existing = {"id": "rec1", "fields": {"telegram_id": TID, "assigned_to": "ai"}}
    calls = AirtableCalls(monkeypatch, existing=existing)
    message = FakeMessage(make_user(), "/start telegram_channel")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert len(calls.upserts) == 1
    _, data = calls.upserts[0]
    assert "source" not in data, "источник первого касания нельзя переопределять"
    assert calls.touches[0][2] == "Повторный /start"
    # Состояния не было → приветствие и ожидание первого сообщения
    assert message.sent[0][0] == texts.GREETING
    assert await state.get_state() == Dialog.waiting_first_message.state


async def test_repeat_start_mid_dialog_keeps_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Живой диалог не сбрасывается повторным /start (шаг 3.2: продолжить
    с текущего состояния FSM)."""
    existing = {"id": "rec1", "fields": {"telegram_id": TID}}
    AirtableCalls(monkeypatch, existing=existing)
    message = FakeMessage(make_user(), "/start")
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    await start_handler.cmd_start(message, state)

    assert await state.get_state() == Dialog.waiting_first_message.state
    assert message.sent[0][0] == texts.CONTINUE_DIALOG


@pytest.mark.parametrize(
    "fields",
    [
        {"assigned_to": "yulia"},
        {"paused": True},
        {"assigned_to": "yulia", "paused": True},
    ],
)
async def test_client_with_yulia_gets_no_automation(
    monkeypatch: pytest.MonkeyPatch, fields: dict
) -> None:
    """Критерий: переданный Юлии клиент не запускает автоматику."""
    existing = {"id": "rec1", "fields": {"telegram_id": TID, **fields}}
    calls = AirtableCalls(monkeypatch, existing=existing)
    message = FakeMessage(make_user(), "/start")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert message.sent == [(texts.ALREADY_WITH_YULIA, {})]
    assert calls.upserts == []  # никаких обновлений маршрута
    assert calls.touches[0][1] == "dm_start"  # но касание сохранено
    assert await state.get_state() is None  # FSM не запущен


async def test_stale_button_after_restart_registers_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Кнопка источника нажата после рестарта бота (FSM пуст): клиент не
    остаётся со спиннером — регистрируется по фактическому состоянию CRM."""
    calls = AirtableCalls(monkeypatch, existing=None)
    user = make_user()
    callback = FakeCallback(user, "src:referral", FakeMessage(user))
    state = make_state()  # состояния нет — как после рестарта

    await start_handler.source_chosen(callback, state)

    assert callback.answered
    assert calls.upserts[0][1]["source"] == "referral"
    assert await state.get_state() == Dialog.waiting_first_message.state


async def test_button_repress_after_registration_no_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторное нажатие кнопки уже зарегистрированным клиентом не создаёт
    дубль и не переопределяет источник."""
    existing = {"id": "rec1", "fields": {"telegram_id": TID, "source": "site"}}
    calls = AirtableCalls(monkeypatch, existing=existing)
    user = make_user()
    origin = FakeMessage(user)
    callback = FakeCallback(user, "src:referral", origin)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    await start_handler.source_chosen(callback, state)

    assert len(calls.upserts) == 1
    assert "source" not in calls.upserts[0][1]
    assert origin.sent[0][0] == texts.CONTINUE_DIALOG


async def test_button_press_by_client_with_yulia(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нажатие кнопки клиентом, переданным Юлии, не запускает автоматику."""
    existing = {"id": "rec1", "fields": {"telegram_id": TID, "assigned_to": "yulia"}}
    calls = AirtableCalls(monkeypatch, existing=existing)
    user = make_user()
    origin = FakeMessage(user)
    callback = FakeCallback(user, "src:site", origin)

    await start_handler.source_chosen(callback, make_state())

    assert origin.sent == [(texts.ALREADY_WITH_YULIA, {})]
    assert calls.upserts == []


# ── Устойчивость ──


async def test_lookup_failure_does_not_start_automation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой поиска в Airtable: нельзя отличить нового клиента от переданного
    Юлии — автоматика не запускается, клиент получает TECH_ERROR."""
    calls = AirtableCalls(monkeypatch, existing=None)
    calls.lookup_ok = False
    message = FakeMessage(make_user(), "/start site")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert message.sent == [(texts.TECH_ERROR, {})]
    assert calls.upserts == [] and calls.touches == []
    assert await state.get_state() is None


async def test_reply_safe_with_inaccessible_message_like_object() -> None:
    """_reply_safe не падает на объекте без from_user (InaccessibleMessage)."""

    class Inaccessible:
        async def answer(self, text: str, **kwargs):
            raise RuntimeError("нельзя ответить")

    await start_handler._reply_safe(Inaccessible(), "текст")  # не должно бросить


async def test_airtable_down_greeting_still_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой Airtable не блокирует диалог: приветствие уходит в любом случае."""
    calls = AirtableCalls(monkeypatch, existing=None)
    calls.upsert_result = None  # CRM недоступна
    message = FakeMessage(make_user(), "/start site")
    state = make_state()

    await start_handler.cmd_start(message, state)

    assert message.sent[0][0] == texts.GREETING
    assert await state.get_state() == Dialog.waiting_first_message.state


async def test_send_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка отправки в Telegram логируется, но не роняет обработчик."""
    AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(), "/start site")

    async def broken_answer(text: str, **kwargs):
        raise RuntimeError("Telegram недоступен")

    message.answer = broken_answer  # type: ignore[method-assign]
    await start_handler.cmd_start(message, make_state())  # не должно бросить


async def test_user_without_username(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = AirtableCalls(monkeypatch, existing=None)
    message = FakeMessage(make_user(username=None), "/start site")

    await start_handler.cmd_start(message, make_state())

    assert "username" not in calls.upserts[0][1]
