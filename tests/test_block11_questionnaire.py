"""Тесты Блока 11: анкета перед диагностикой «Точка сбоя».

Критерии готовности из ТЗ: все 7 вопросов + согласие на запись; ответы
сохраняются в Diagnostics; отчёт формируется и отправляется Юлии;
AI не интерпретирует ответы.

Отдельно проверяется интеграция с Блоком 7: анкету заполняет уже переданный
Юлии клиент, для которого автоматика остановлена, — pause_check обязан
пропустить именно ответы анкеты и ничего больше.
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
from bot.handlers import questionnaire as form
from bot.middlewares.pause_check import PauseCheckMiddleware
from bot.prompts.questionnaire_analyzer import build_questionnaire_prompt
from bot.services import airtable
from bot.services.notifier import build_pre_meeting_report
from bot.states import Questionnaire
from bot.utils.validators import validate_questionnaire_analysis
from tests.test_block6_qualification import CRM, config  # noqa: F401

ADMIN_ID = 999
CLIENT_ID = 777001

ANSWERS = [
    "Повторяется одна и та же ситуация в отношениях",
    "Полгода назад развёлся, и всё повторилось снова",
    "Ощущение тупика",
    "Работал с психологом, читал книги",
    "Понять, почему это повторяется",
    "Устал жить по кругу",
    "Готов начать работать",
]

ANALYSIS = {
    "main_request": "Повторяющийся сценарий в отношениях",
    "summary": "Клиент описывает повторяющуюся ситуацию. Обращался к психологу.",
    "key_phrases": ["уже полгода не могу выйти", "готов начать работать"],
    "preliminary_status": "warm",
    "topics_to_clarify": ["Что именно менялось после работы с психологом"],
    "confidence": 88,
}


class FakeBot:
    id = 1

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("клиент заблокировал бота")
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, text: str | None, user_id: int = CLIENT_ID, chat_type: str = "private"):
        self.from_user = User(id=user_id, is_bot=False, first_name="Анна")
        self.text = text
        self.caption = None
        self.content_type = "text" if text else "voice"
        self.chat = type("Chat", (), {"type": chat_type})()
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeConsentMessage:
    def __init__(self):
        self.sent: list[str] = []
        self.markup_removed = False

    async def edit_reply_markup(self, **kwargs):
        self.markup_removed = True

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeCallback:
    def __init__(self, data: str, user_id: int = CLIENT_ID):
        self.from_user = User(id=user_id, is_bot=False, first_name="Анна")
        self.data = data
        self.message = FakeConsentMessage()
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


class DiagnosticsCRM:
    """Подменяет операции Airtable, нужные анкете."""

    def __init__(self, monkeypatch, existing: list | None = None):
        self.created: list[tuple[int, dict]] = []
        self.touches: list[tuple] = []
        self.existing = existing or []
        self.contact = {
            "id": "rec1",
            "fields": {
                "telegram_id": CLIENT_ID,
                "name": "Анна",
                "username": "@anna",
                "source": "telegram_channel",
                "paused": True,
                "assigned_to": "yulia",
            },
        }

        async def create_diagnostic(tid, data):
            self.created.append((tid, data))
            return {"id": "recD"}

        async def get_diagnostics(tid):
            return self.existing

        async def add_touch(tid, type, description, **kwargs):
            self.touches.append((type, description))
            return {"id": "recT"}

        async def find_contact(tid):
            return self.contact

        async def find_contact_checked(tid):
            return True, self.contact

        for name, fn in [
            ("create_diagnostic", create_diagnostic),
            ("get_diagnostics", get_diagnostics),
            ("add_touch", add_touch),
            ("find_contact", find_contact),
            ("find_contact_checked", find_contact_checked),
        ]:
            monkeypatch.setattr(airtable, name, fn)


class FakeAI:
    def __init__(self, result=ANALYSIS, raises: bool = False):
        self.result = result
        self.raises = raises
        self.calls: list[list[str]] = []

    async def analyze_questionnaire(self, answers, **kwargs):
        self.calls.append(list(answers))
        if self.raises:
            raise RuntimeError("OpenAI недоступен")
        return self.result


def make_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CLIENT_ID, user_id=CLIENT_ID)
    )


async def fill_form(state: FSMContext, answers=ANSWERS) -> list[FakeMessage]:
    """Проходит анкету от первого вопроса до запроса согласия."""
    await state.set_state(Questionnaire.q1)
    await state.update_data(form_answers=[])
    messages = []
    for answer in answers:
        message = FakeMessage(answer)
        await form.form_answer(message, state)
        messages.append(message)
    return messages


# ── Состав анкеты ──


async def test_seven_questions_then_consent():
    """Критерий: все 7 вопросов + согласие на запись, по одному вопросу за раз."""
    state = make_state()
    messages = await fill_form(state)

    asked = [m.sent[0] for m in messages]
    assert asked[:6] == [
        texts.Q_FORM_2,
        texts.Q_FORM_3,
        texts.Q_FORM_4,
        texts.Q_FORM_5,
        texts.Q_FORM_6,
        texts.Q_FORM_7,
    ]
    # После седьмого ответа — согласие на запись
    assert asked[6] == texts.Q_FORM_CONSENT
    assert await state.get_state() == Questionnaire.consent.state
    assert (await state.get_data())["form_answers"] == ANSWERS


async def test_intro_and_first_question_sent():
    """Вступление из документа + первый вопрос; состояние выставлено клиенту."""
    bot = FakeBot()
    state = make_state()
    assert await form.send_questionnaire(bot, state, CLIENT_ID) is True
    assert bot.sent[0] == (CLIENT_ID, texts.QUESTIONNAIRE_INTRO)
    assert bot.sent[1] == (CLIENT_ID, texts.Q_FORM_1)
    assert await state.get_state() == Questionnaire.q1.state


async def test_undelivered_questionnaire_does_not_reset_state():
    """Клиент заблокировал бота: прежние ответы не затираются."""
    bot = FakeBot(fail=True)
    state = make_state()
    await state.set_state(Questionnaire.q3)
    await state.update_data(form_answers=["раз", "два"])

    assert await form.send_questionnaire(bot, state, CLIENT_ID) is False
    assert await state.get_state() == Questionnaire.q3.state
    assert (await state.get_data())["form_answers"] == ["раз", "два"]


async def test_non_text_answer_does_not_advance():
    """Голосовое/стикер не засчитывается за ответ — вопрос остаётся тем же."""
    state = make_state()
    await state.set_state(Questionnaire.q1)
    await state.update_data(form_answers=[])

    message = FakeMessage(None)
    await form.form_answer(message, state)

    assert message.sent == [texts.ASK_TEXT_PLEASE]
    assert await state.get_state() == Questionnaire.q1.state
    assert (await state.get_data())["form_answers"] == []


# ── Сохранение и отчёт ──


async def test_answers_saved_to_diagnostics(monkeypatch, config):  # noqa: F811
    """Критерий: ответы сохраняются в Diagnostics вместе с согласием."""
    crm = DiagnosticsCRM(monkeypatch)
    monkeypatch.setattr(form, "get_ai", lambda: FakeAI())
    state = make_state()
    await fill_form(state)

    callback = FakeCallback("consent:yes")
    await form.consent_answer(callback, state, FakeBot(), config)

    assert len(crm.created) == 1
    telegram_id, data = crm.created[0]
    assert telegram_id == CLIENT_ID
    assert data["recording_consent"] is True
    assert data["booking_status"] == "requested"
    saved = json.loads(data["questionnaire"])
    assert len(saved) == 7
    assert [item["answer"] for item in saved] == ANSWERS
    # Касание фиксируется, состояние очищено
    assert crm.touches[0][0] == "question_answered"
    assert await state.get_state() is None


async def test_consent_no_is_recorded(monkeypatch, config):  # noqa: F811
    """Отказ от записи сохраняется как отказ, а не теряется."""
    crm = DiagnosticsCRM(monkeypatch)
    monkeypatch.setattr(form, "get_ai", lambda: FakeAI())
    state = make_state()
    await fill_form(state)

    await form.consent_answer(FakeCallback("consent:no"), state, FakeBot(), config)
    assert crm.created[0][1]["recording_consent"] is False


async def test_report_sent_to_yulia(monkeypatch, config):  # noqa: F811
    """Критерий: отчёт формируется и отправляется Юлии."""
    DiagnosticsCRM(monkeypatch)
    monkeypatch.setattr(form, "get_ai", lambda: FakeAI())
    bot = FakeBot()
    state = make_state()
    await fill_form(state)

    await form.consent_answer(FakeCallback("consent:yes"), state, bot, config)

    to_yulia = [text for chat_id, text in bot.sent if chat_id == config.telegram_admin_id]
    assert len(to_yulia) == 1
    report = to_yulia[0]
    # Все разделы, перечисленные в ТЗ для отчёта перед встречей
    for section in (
        "ОСНОВНОЙ ЗАПРОС",
        "ПОЧЕМУ ИМЕННО СЕЙЧАС",
        "ЖЕЛАЕМЫЙ РЕЗУЛЬТАТ",
        "ЧТО УЖЕ ПРОБОВАЛ(А)",
        "КЛЮЧЕВЫЕ ФОРМУЛИРОВКИ КЛИЕНТА",
        "ЧТО СТОИТ УТОЧНИТЬ НА ВСТРЕЧЕ",
    ):
        assert section in report
    assert "Анна" in report and "telegram_channel" in report


async def test_report_survives_ai_failure(monkeypatch, config):  # noqa: F811
    """Сбой OpenAI не отменяет анкету: отчёт уходит из сырых ответов."""
    crm = DiagnosticsCRM(monkeypatch)
    monkeypatch.setattr(form, "get_ai", lambda: FakeAI(raises=True))
    bot = FakeBot()
    state = make_state()
    await fill_form(state)

    await form.consent_answer(FakeCallback("consent:yes"), state, bot, config)

    assert len(crm.created) == 1, "ответы должны сохраниться даже без AI"
    report = [t for c, t in bot.sent if c == config.telegram_admin_id][0]
    assert "AI-разбор анкеты недоступен" in report
    assert ANSWERS[1] in report  # «почему именно сейчас» — из ответа напрямую


def test_report_without_analysis_keeps_client_answers():
    """Отчёт без AI содержит ответы клиента дословно."""
    report = build_pre_meeting_report({"name": "Анна"}, ANSWERS, True, None)
    for answer in ANSWERS:
        assert answer in report
    assert "Согласие на запись встречи: да" in report


def test_report_marks_missing_answers():
    """Пропущенные ответы отмечаются явно, а не пустой строкой."""
    report = build_pre_meeting_report({"name": "Анна"}, ["только первый"], False, None)
    assert "— (без ответа)" in report
    assert "Согласие на запись встречи: нет" in report


# ── Граница: AI не интерпретирует ──


def test_prompt_forbids_interpretation():
    """Критерий ТЗ: AI не проводит диагностику и не интерпретирует ответы."""
    prompt = build_questionnaire_prompt(ANSWERS)
    for ban in (
        "проводить диагностику",
        "интерпретировать ответы",
        "добавлять то, чего клиент не говорил",
    ):
        assert ban in prompt
    # Ответы клиента попадают в промпт целиком
    for answer in ANSWERS:
        assert answer in prompt


def test_validator_rejects_interpretation_shaped_output():
    """Валидатор ловит неполный или неверный разбор анкеты."""
    assert validate_questionnaire_analysis(ANALYSIS) == []
    assert validate_questionnaire_analysis({}) != []
    bad_status = {**ANALYSIS, "preliminary_status": "diagnosed"}
    assert validate_questionnaire_analysis(bad_status) != []
    bad_quotes = {**ANALYSIS, "key_phrases": "одна строка"}
    assert validate_questionnaire_analysis(bad_quotes) != []
    bad_confidence = {**ANALYSIS, "confidence": 130}
    assert validate_questionnaire_analysis(bad_confidence) != []


# ── Интеграция с Блоком 7 ──


def _patch_message_type(monkeypatch) -> None:
    """middleware проверяет isinstance(event, Message) — подменяем тип.

    Без этого фейковое сообщение проходит насквозь по первой же строке
    middleware, и тест «пропускает анкету» зеленел бы, ничего не проверяя.
    """
    from bot.middlewares import pause_check as pause_module

    monkeypatch.setattr(pause_module, "Message", FakeMessage)


async def test_pause_check_passes_questionnaire_answers(monkeypatch, config):  # noqa: F811
    """Переданный Юлии клиент отвечает на анкету — middleware пропускает.

    Без этого ответы проглатывались бы как сообщения переданного клиента
    и анкету нельзя было бы заполнить в принципе.
    """
    _patch_message_type(monkeypatch)
    DiagnosticsCRM(monkeypatch)
    state = make_state()
    await state.set_state(Questionnaire.q2)

    reached = []

    async def handler(event, data):
        reached.append(event)
        return "ok"

    middleware = PauseCheckMiddleware(config)
    message = FakeMessage("мой ответ")
    result = await middleware(handler, message, {"state": state, "bot": FakeBot()})

    assert result == "ok" and len(reached) == 1
    # Ответ на анкету не должен вызывать «Юлия уже знает о вашем обращении»
    assert message.sent == []


async def test_pause_check_still_blocks_outside_questionnaire(monkeypatch, config):  # noqa: F811
    """Вне анкеты автоматика для переданного клиента по-прежнему остановлена."""
    _patch_message_type(monkeypatch)
    DiagnosticsCRM(monkeypatch)

    async def update_contact(record_id, data):
        return {"id": record_id}

    monkeypatch.setattr(airtable, "update_contact", update_contact)
    state = make_state()  # состояния нет

    reached = []

    async def handler(event, data):
        reached.append(event)
        return "ok"

    middleware = PauseCheckMiddleware(config)
    message = FakeMessage("обычное сообщение")
    result = await middleware(handler, message, {"state": state, "bot": FakeBot()})

    assert result is None and reached == []
    assert texts.ALREADY_WITH_YULIA in message.sent


# ── Команда Юлии ──


async def test_anketa_requires_admin(monkeypatch, config):  # noqa: F811
    """Критерий Части 7: admin-команда недоступна обычному пользователю."""
    DiagnosticsCRM(monkeypatch)
    bot = FakeBot()
    message = FakeMessage(f"/anketa {CLIENT_ID}", user_id=CLIENT_ID)

    await admin_handlers.cmd_anketa(message, config, make_state(), bot)

    assert message.sent == ["Команда недоступна."]
    assert bot.sent == []


async def test_anketa_sends_form(monkeypatch, config):  # noqa: F811
    """Юлия отправляет анкету — клиент получает вступление и первый вопрос."""
    DiagnosticsCRM(monkeypatch)
    bot = FakeBot()
    message = FakeMessage(f"/anketa {CLIENT_ID}", user_id=ADMIN_ID)

    await admin_handlers.cmd_anketa(message, config, make_state(), bot)

    assert [text for _, text in bot.sent] == [texts.QUESTIONNAIRE_INTRO, texts.Q_FORM_1]
    assert "Анкета отправлена" in message.sent[0]


async def test_anketa_warns_when_already_filled(monkeypatch, config):  # noqa: F811
    """Повторная отправка не затирает заполненную анкету молча."""
    DiagnosticsCRM(monkeypatch, existing=[{"id": "recD"}])
    bot = FakeBot()
    message = FakeMessage(f"/anketa {CLIENT_ID}", user_id=ADMIN_ID)

    await admin_handlers.cmd_anketa(message, config, make_state(), bot)

    assert bot.sent == []
    assert "уже есть заполненная анкета" in message.sent[0]


async def test_anketa_force_resends(monkeypatch, config):  # noqa: F811
    """/anketa_force отправляет анкету повторно осознанно."""
    DiagnosticsCRM(monkeypatch, existing=[{"id": "recD"}])
    bot = FakeBot()
    message = FakeMessage(f"/anketa_force {CLIENT_ID}", user_id=ADMIN_ID)

    await admin_handlers.cmd_anketa_force(message, config, make_state(), bot)

    assert [text for _, text in bot.sent] == [texts.QUESTIONNAIRE_INTRO, texts.Q_FORM_1]
    assert "повторно" in message.sent[0]
