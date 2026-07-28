"""Тесты Блока 6: FSM квалификации, три сценария, маршрутизация, таймауты.

Критерии: A — 2 вопроса → передача; B — до 5 вопросов, досрочное завершение;
C — ответ без квалификации, переключение на B; все 4 маршрута;
confidence < 85 → автопередача; решение Юлии не переопределяется;
история пишется в conversation_history и Touches.
"""

from __future__ import annotations

import json

import pytest
from aiogram.enums import ContentType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from bot import texts
from bot.config import Config
from bot.handlers import qualification as qual
from bot.services import ai as ai_module
from bot.services import airtable
from bot.states import Dialog

TID = 777001


class FakeMessage:
    def __init__(self, user, text="привет", caption=None, content_type="text"):
        self.from_user = user
        self.text = text
        self.caption = caption
        # Enum, как у настоящего Message (у str-заглушки нет .value)
        self.content_type = ContentType(content_type)
        self.chat = type("Chat", (), {"type": "private"})()
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeAI:
    """Подменяет AIService: очереди ответов трёх функций."""

    def __init__(self):
        self.scenarios: list[dict | None] = []
        self.qualifications: list[dict | None] = []
        self.answers: list[dict | None] = []
        self.qualify_calls: list[dict] = []

    async def detect_scenario(self, message, **kwargs):
        return self.scenarios.pop(0) if self.scenarios else None

    async def qualify(self, conversation, *, final=True, **kwargs):
        self.qualify_calls.append({"final": final})
        result = self.qualifications.pop(0) if self.qualifications else None
        return self.finalize(result) if result is not None and final else result

    def finalize(self, data):
        """Финальные правила настоящего AIService: порог 85% → передача Юлии."""
        if data.get("confidence", 100) < 85:
            return {
                **data,
                "needs_yulia": True,
                "needs_yulia_reason": "Требуется экспертная оценка",
            }
        return data

    async def answer_info(self, question, **kwargs):
        return self.answers.pop(0) if self.answers else None


class CRM:
    """Запоминает вызовы airtable, отдаёт контакт."""

    def __init__(self, monkeypatch, fields=None):
        base_fields = {"telegram_id": TID, "name": "Анна", "status": "cold", "touches_count": 1}
        base_fields.update(fields or {})
        self.contact = {"id": "rec1", "fields": base_fields}
        self.updates: list[dict] = []
        self.touches: list[tuple] = []
        self.status_changes: list[tuple] = []
        self.tasks: list[str] = []

        async def find_contact_checked(tid):
            return True, self.contact

        async def upsert_contact(tid, data):
            return self.contact

        async def update_contact(record_id, data):
            self.updates.append(data)
            self.contact["fields"].update(data)
            return self.contact

        async def add_touch(tid, type, description, **kwargs):
            self.touches.append((type, description, kwargs))
            return {"id": "recT"}

        async def add_status_change(record_id, old, new, reason, by):
            self.status_changes.append((old, new, reason, by))
            history = json.loads(self.contact["fields"].get("status_history") or "[]")
            history.append({"from": old, "to": new, "reason": reason, "by": by})
            self.contact["fields"]["status_history"] = json.dumps(history, ensure_ascii=False)
            self.contact["fields"]["status"] = new
            return self.contact

        async def create_task(action, assignee, due, reason, **kwargs):
            self.tasks.append(action)
            return {"id": "recTask"}

        async def build_timeline(tid):
            return "15.07 — первое обращение"

        for name, fn in [
            ("find_contact_checked", find_contact_checked),
            ("upsert_contact", upsert_contact),
            ("update_contact", update_contact),
            ("add_touch", add_touch),
            ("add_status_change", add_status_change),
            ("create_task", create_task),
            ("build_timeline", build_timeline),
        ]:
            monkeypatch.setattr(airtable, name, fn)


@pytest.fixture
def fake_ai(monkeypatch) -> FakeAI:
    fake = FakeAI()
    monkeypatch.setattr(ai_module, "_service", fake)
    return fake


@pytest.fixture
def config() -> Config:
    return Config(
        telegram_bot_token="42:X",
        telegram_admin_id=999,
        telegram_channel_id=-100,
        telegram_discussion_group_id=-200,
        webhook_url="https://x.example",
        openai_api_key="sk-x",
        airtable_api_key="pat-x",
        airtable_base_id="appX",
        _env_file=None,
    )


def make_user() -> User:
    return User(id=TID, is_bot=False, first_name="Анна")


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TID, user_id=TID))


def valid_qualification(**over) -> dict:
    data = {
        "summary": "Повторяющаяся ситуация в отношениях.",
        "key_phrases": ["не могу выйти из этого состояния"],
        "status": "warm",
        "status_reason": "описал проблему, интересуется методом",
        "awareness": "medium",
        "readiness": "medium",
        "urgency": "low",
        "confidence": 90,
        "next_action": "предложить диагностику",
        "needs_yulia": False,
        "needs_yulia_reason": None,
        "product_interest": "diagnostics",
        "interests": ["повторяющиеся сценарии"],
        "bot_response": "Понимаю. Если хотите разобраться, первым шагом обычно становится диагностика.",
    }
    data.update(over)
    return data


# ── Сценарий A ──


async def test_scenario_a_two_questions_then_handoff(monkeypatch, fake_ai, config):
    """Критерий: сценарий A — 2 вопроса → передача, статус hot."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    fake_ai.scenarios = [{"scenario": "A_ready", "confidence": 96, "reason": "хочет записаться"}]
    m1 = FakeMessage(make_user(), "Хочу записаться")
    await qual.first_message(m1, state, bot, config)
    assert m1.sent == [texts.A_INTRO]
    assert await state.get_state() == Dialog.a_question_1.state

    m2 = FakeMessage(make_user(), "Повторяются конфликты в семье")
    await qual.a_answer_1(m2, state)
    # Вопросы сценария A — дословно из ТЗ («хотите достичь», не «получить»)
    assert m2.sent == [texts.A_QUESTION_2]
    assert texts.A_INTRO.endswith(texts.A_QUESTION_1)
    assert texts.A_QUESTION_1 == "С какой ситуацией хотите разобраться?"
    assert texts.A_QUESTION_2 == "Какого результата хотите достичь?"
    assert await state.get_state() == Dialog.a_question_2.state

    fake_ai.qualifications = [valid_qualification(status="warm", confidence=70)]
    m3 = FakeMessage(make_user(), "Хочу перестать наступать на грабли")
    await qual.a_answer_2(m3, state, bot, config)

    assert m3.sent == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]
    assert crm.contact["fields"]["status"] == "hot"  # принудительно hot
    assert crm.contact["fields"]["assigned_to"] == "yulia"
    assert crm.contact["fields"]["paused"] is True
    assert any("Связаться с Анна" in t for t in crm.tasks)
    assert bot.sent and bot.sent[0][0] == 999  # карточка Юлии
    assert await state.get_state() is None


# ── Сценарий B ──


async def test_scenario_b_full_flow_to_warm(monkeypatch, fake_ai, config):
    """B: вопросы по одному до конца цепочки, затем warm и свободный диалог.

    Высокая уверенность в статусе цепочку не обрывает: ``confidence``
    измеряет уверенность в статусе, а не полноту картины. FSM в ТЗ —
    ``q1 → q2 → q3 → q4 → [q5]``, в скобках только пятый.
    """
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 91, "reason": "описал проблему"}]
    m1 = FakeMessage(make_user(), "У меня повторяется одна и та же ситуация")
    await qual.first_message(m1, state, bot, config)
    assert m1.sent == [texts.QUESTION_DURATION]  # первое сообщение = ответ на вопрос 1
    assert await state.get_state() == Dialog.b_duration.state

    fake_ai.qualifications = [valid_qualification(confidence=60)]
    m2 = FakeMessage(make_user(), "Года полтора")
    await qual.b_answer_duration(m2, state, bot, config)
    assert m2.sent == [texts.QUESTION_2]
    assert fake_ai.qualify_calls[-1]["final"] is False

    fake_ai.qualifications = [valid_qualification(confidence=60)]
    m3 = FakeMessage(make_user(), "Больше всего беспокоит выгорание")
    await qual.b_answer_2(m3, state, bot, config)
    assert m3.sent == [texts.QUESTION_3]

    # Даже при высокой уверенности вопрос 4 задаётся: цепочка не окончена
    fake_ai.qualifications = [valid_qualification(confidence=90, status="warm")]
    m4 = FakeMessage(make_user(), "Пробовала психолога полгода")
    await qual.b_answer_3(m4, state, bot, config)
    assert m4.sent == [texts.QUESTION_4], "уверенность оборвала цепочку вопросов"

    # После вопроса 4 информации достаточно → финал warm, вопрос 5 не нужен
    fake_ai.qualifications = [valid_qualification(confidence=90, status="warm")]
    m5 = FakeMessage(make_user(), "Хочу перестать повторять этот сценарий")
    await qual.b_answer_4(m5, state, bot, config)
    assert "диагностика" in m5.sent[0]
    assert await state.get_state() == Dialog.open_dialog.state
    assert crm.contact["fields"]["status"] == "warm"
    assert crm.contact["fields"]["qualification_completed"] is True
    assert ("cold", "warm") == crm.status_changes[0][:2]


async def test_scenario_b_question_5_only_when_needed(monkeypatch, fake_ai, config):
    """Вопрос 5 задаётся только при недостатке информации после q4."""
    CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.b_question_4)
    await state.update_data(q1="ситуация")

    fake_ai.qualifications = [valid_qualification(confidence=50)]
    m = FakeMessage(make_user(), "Хочу спокойствия")
    await qual.b_answer_4(m, state, bot, config)
    assert m.sent == [texts.QUESTION_5]
    assert await state.get_state() == Dialog.b_question_5.state


async def test_scenario_b_low_confidence_after_q5_hands_off(monkeypatch, fake_ai, config):
    """Критерий: после q5 confidence < 85 → автопередача Юлии."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.b_question_5)

    fake_ai.qualifications = [valid_qualification(confidence=60, status="warm")]
    m = FakeMessage(make_user(), "Просто почувствовала, что пора")
    await qual.b_answer_5(m, state, bot, config)

    assert fake_ai.qualify_calls[-1]["final"] is True
    assert m.sent == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]
    assert crm.contact["fields"]["assigned_to"] == "yulia"
    assert bot.sent and "ТРЕБУЕТСЯ ЭКСПЕРТНАЯ ОЦЕНКА" in bot.sent[0][1]


async def test_immediate_handoff_trigger_mid_flow(monkeypatch, fake_ai, config):
    """Триггер немедленной передачи посреди сценария B (просит личный контакт)."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.b_question_2)

    fake_ai.qualifications = [
        valid_qualification(
            needs_yulia=True, needs_yulia_reason="просит связаться лично", confidence=95
        )
    ]
    m = FakeMessage(make_user(), "Свяжите меня с Юлией, пожалуйста")
    await qual.b_answer_2(m, state, bot, config)

    assert m.sent == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]
    assert crm.contact["fields"]["assigned_to"] == "yulia"
    assert await state.get_state() is None


async def test_non_target_route(monkeypatch, fake_ai, config):
    """Маршрут non_target: корректный ответ, paused=true, FSM завершён."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.b_question_2)

    farewell = "Спасибо, что написали. Этот запрос выходит за рамки специализации Юлии..."
    fake_ai.qualifications = [
        valid_qualification(
            status="non_target",
            confidence=95,
            bot_response=farewell,
            status_reason="медицинский запрос",
        )
    ]
    m = FakeMessage(make_user(), "Подберите мне лекарство от депрессии")
    await qual.b_answer_2(m, state, bot, config)

    assert m.sent == [farewell]
    assert crm.contact["fields"]["status"] == "non_target"
    assert crm.contact["fields"]["paused"] is True
    assert await state.get_state() is None
    assert bot.sent == []  # нецелевой не передаётся Юлии карточкой


# ── Сценарий C ──


async def test_scenario_c_answers_without_qualification(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    fake_ai.scenarios = [{"scenario": "C_info", "confidence": 93, "reason": "вопрос о методе"}]
    fake_ai.answers = [
        {
            "answer": "ITC — методология системной диагностики...",
            "needs_yulia": False,
            "reason": None,
        }
    ]
    m = FakeMessage(make_user(), "Что такое ITC?")
    await qual.first_message(m, state, bot, config)

    assert "ITC" in m.sent[0]
    assert await state.get_state() == Dialog.c_info.state
    assert crm.status_changes == []  # квалификация не начиналась


async def test_scenario_c_switches_to_b_on_problem(monkeypatch, fake_ai, config):
    """Критерий: появился собственный запрос → переключение на сценарий B."""
    CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.c_info)

    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 90, "reason": "описал проблему"}]
    m = FakeMessage(make_user(), "Вообще у меня самого бизнес не растёт уже год")
    await qual.c_message(m, state, bot, config)

    assert m.sent == [texts.QUESTION_DURATION]
    assert await state.get_state() == Dialog.b_duration.state


async def test_scenario_c_out_of_knowledge_hands_off(monkeypatch, fake_ai, config):
    """Нет ответа в базе знаний → needs_yulia → передача."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    fake_ai.scenarios = [{"scenario": "C_info", "confidence": 90, "reason": "вопрос"}]
    fake_ai.answers = [{"answer": "…", "needs_yulia": True, "reason": "нет в базе знаний"}]
    fake_ai.qualifications = [valid_qualification(confidence=88)]
    m = FakeMessage(make_user(), "Работаете ли вы с корпорациями из Сингапура?")
    await qual.first_message(m, state, bot, config)

    assert m.sent == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]
    assert crm.contact["fields"]["assigned_to"] == "yulia"


# ── История, приоритет Юлии, устойчивость ──


async def test_history_written_for_every_turn(monkeypatch, fake_ai, config):
    """Критерий: история пишется в conversation_history и Touches."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 91, "reason": "проблема"}]
    m = FakeMessage(make_user(), "Всё рушится")
    await qual.first_message(m, state, bot, config)

    history = json.loads(crm.contact["fields"]["conversation_history"])
    assert [h["role"] for h in history] == ["client", "bot"]
    assert history[0]["text"] == "Всё рушится"
    assert history[1]["text"] == texts.QUESTION_DURATION
    assert any(t[0] == "question_answered" for t in crm.touches)


async def test_yulia_status_not_overridden(monkeypatch, fake_ai, config):
    """Критерий: решение Юлии не переопределяется автоматикой."""
    crm = CRM(
        monkeypatch,
        fields={
            "status": "nurturing",
            "status_history": json.dumps(
                [{"from": "hot", "to": "nurturing", "by": "yulia", "reason": "решение Юлии"}]
            ),
        },
    )
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.b_question_5)

    fake_ai.qualifications = [valid_qualification(status="warm", confidence=95)]
    m = FakeMessage(make_user(), "ответ")
    await qual.b_answer_5(m, state, bot, config)

    # Статус Юлии не тронут: смены cold→warm не было
    assert crm.status_changes == []


async def test_ai_failure_creates_task_and_informs_client(monkeypatch, fake_ai, config):
    """Сбой AI: клиенту TECH_ERROR, Юлии задача, бот не падает."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)

    fake_ai.scenarios = [None]
    m = FakeMessage(make_user(), "тест")
    await qual.first_message(m, state, bot, config)

    assert m.sent == [texts.TECH_ERROR]
    assert any("Проверить диалог" in t for t in crm.tasks)


async def test_non_text_message_in_dialog(monkeypatch, fake_ai, config):
    CRM(monkeypatch)
    m = FakeMessage(make_user(), text=None, content_type="voice")
    await qual.non_text_in_dialog(m)
    assert m.sent == [texts.ASK_TEXT_PLEASE]


async def test_uncertain_non_target_mid_flow_goes_to_yulia(monkeypatch, fake_ai, config):
    """Неуверенный (confidence < 85) вердикт non_target на промежуточном шаге
    НЕ закрывает диалог — передача Юлии («лучше лишняя передача»)."""
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.b_question_2)

    fake_ai.qualifications = [
        valid_qualification(status="non_target", confidence=40, needs_yulia=False)
    ]
    m = FakeMessage(make_user(), "неоднозначное сообщение")
    await qual.b_answer_2(m, state, bot, config)

    # Передача, а не прощание. Рассказ о диагностике сюда не добавляется:
    # вердикт «нецелевой» под вопросом, и навязывать формат работы рано
    assert m.sent == [texts.HANDOFF_MESSAGE]
    assert crm.contact["fields"]["assigned_to"] == "yulia"
    assert bot.sent, "карточка Юлии не отправлена"


async def test_client_turn_updates_last_contact_date(monkeypatch, fake_ai, config):
    """Каждое сообщение клиента освежает last_contact_date — от него
    считаются таймауты 24/72 ч."""
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 91, "reason": "x"}]
    m = FakeMessage(make_user(), "Всё повторяется")
    await qual.first_message(m, state, FakeBot(), config)
    assert any("last_contact_date" in u for u in crm.updates)


async def test_blocked_contact_saved_silently_in_handler(monkeypatch, fake_ai, config):
    """Гонка со сбоем Airtable в middleware: хендлер сам не запускает
    автоматику для переданного клиента — сообщение сохраняется молча."""
    crm = CRM(monkeypatch, fields={"paused": True})
    state = make_state()
    await state.set_state(Dialog.b_question_2)
    m = FakeMessage(make_user(), "Есть новости?")
    await qual.b_answer_2(m, state, FakeBot(), config)
    assert m.sent == []  # AI не отвечал
    history = json.loads(crm.contact["fields"]["conversation_history"])
    assert history[-1]["text"] == "Есть новости?"


async def test_answers_non_dict_from_model_sanitized(monkeypatch, fake_ai, config):
    """Лишний ключ answers не-словарём от модели не роняет передачу."""
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.b_question_5)
    fake_ai.qualifications = [
        valid_qualification(status="hot", confidence=95, answers="строка вместо словаря")
    ]
    m = FakeMessage(make_user(), "готов")
    await qual.b_answer_5(m, state, FakeBot(), config)
    assert m.sent == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]  # не упало


async def test_open_dialog_upgrade_to_hot(monkeypatch, fake_ai, config):
    """Дозревание в свободном диалоге: warm → hot → передача."""
    crm = CRM(monkeypatch, fields={"status": "warm"})
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.open_dialog)

    fake_ai.qualifications = [
        valid_qualification(status="hot", confidence=97, status_reason="готов оплатить")
    ]
    m = FakeMessage(make_user(), "Хорошо, я готов оплатить диагностику")
    await qual.open_dialog_message(m, state, bot, config)

    assert m.sent == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]
    assert crm.contact["fields"]["assigned_to"] == "yulia"
    assert ("warm", "hot") == crm.status_changes[0][:2]
