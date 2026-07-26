"""Блок 12: 15 контрольных сценариев из ТЗ (offline, на имитациях API).

Каждый тест — сценарий из таблицы «Архитектуры», раздел 20. Живой прогон
с реальными OpenAI/Airtable/Telegram выполняется на деплое; результаты
фиксируются в tests/RESULTS.md.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from bot import texts
from bot.handlers import comments as com
from bot.handlers import qualification as qual
from bot.services import ai as ai_module
from bot.services import airtable
from bot.states import Dialog
from tests.test_block6_qualification import (  # noqa: F401
    CRM,
    FakeAI,
    FakeBot,
    FakeMessage,
    config,
    fake_ai,
    make_state,
    make_user,
    valid_qualification,
)
from tests.test_block7_admin import PauseCheckMiddleware
from tests.test_block8_comments import CommentsCRM, FakeAI as CommentsAI
from tests.test_block8_comments import make_analysis, make_comment_message

TID = 777001


# Сценарий 1: холодный подписчик оставил общий комментарий
async def test_scenario_01_cold_commenter(monkeypatch, config):
    crm = CommentsCRM(monkeypatch)
    monkeypatch.setattr(
        ai_module,
        "_service",
        CommentsAI(make_analysis(is_potential_client=False, needs_reply=False, interest_level=2)),
    )
    bot = FakeBot()
    await com.discussion_message(make_comment_message("Интересная мысль"), bot, config)
    assert crm.comments and crm.comments[0]["is_potential_client"] is False
    assert bot.sent == []  # уведомления нет
    assert crm.contacts  # контакт создан (по умолчанию cold — задаёт create_contact)


# Сценарий 2: человек описал проблему → квалификация → warm
async def test_scenario_02_problem_described(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 92, "reason": "проблема"}]
    m = FakeMessage(make_user(), "У меня повторяется одна и та же ситуация")
    await qual.first_message(m, state, FakeBot(), config)
    assert m.sent == [texts.QUESTION_2]

    fake_ai.qualifications = [valid_qualification(confidence=90, status="warm")]
    m2 = FakeMessage(make_user(), "Сильнее всего беспокоит бессилие")
    await qual.b_answer_2(m2, state, FakeBot(), config)
    assert crm.contact["fields"]["status"] == "warm"


# Сценарий 3: человек спросил о стоимости → сценарий A → hot → передача
async def test_scenario_03_price_question(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    fake_ai.scenarios = [{"scenario": "A_ready", "confidence": 96, "reason": "вопрос о цене"}]
    m1 = FakeMessage(make_user(), "Сколько стоит диагностика?")
    await qual.first_message(m1, state, bot, config)
    m2 = FakeMessage(make_user(), "Бизнес не растёт")
    await qual.a_answer_1(m2, state)
    fake_ai.qualifications = [valid_qualification()]
    m3 = FakeMessage(make_user(), "Рост и системность")
    await qual.a_answer_2(m3, state, bot, config)
    assert crm.contact["fields"]["status"] == "hot"
    assert m3.sent == [texts.HANDOFF_MESSAGE]
    assert bot.sent  # карточка Юлии


# Сценарий 4: просит связаться с Юлией → немедленная передача с любого этапа
async def test_scenario_04_asks_for_yulia(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.b_question_3)
    fake_ai.qualifications = [
        valid_qualification(needs_yulia=True, needs_yulia_reason="просит личное общение")
    ]
    m = FakeMessage(make_user(), "Можно поговорить с Юлией лично?")
    await qual.b_answer_3(m, state, FakeBot(), config)
    assert crm.contact["fields"]["assigned_to"] == "yulia"
    assert await state.get_state() is None


# Сценарий 5: не отвечает → 24ч одно напоминание, 72ч cold (см. также block9_10)
async def test_scenario_05_timeouts(monkeypatch):
    from tests.test_block9_10_reports_backup import TimeoutCRM, record
    from scripts.check_timeouts import process_cold, process_reminders

    contact = record({"telegram_id": 111, "name": "Анна", "last_contact_date": "2026-07-25"})
    crm = TimeoutCRM(monkeypatch, [contact], stale_by_hours={72: []})
    bot = FakeBot()
    assert await process_reminders(bot, 24, 72) == 1
    assert bot.sent == [(111, texts.REMINDER_24H)]

    contact72 = record({"telegram_id": 111, "status": "warm", "last_contact_date": "x"})
    crm2 = TimeoutCRM(monkeypatch, [contact72])
    assert await process_cold(72) == 1
    assert crm2.updates[0][1]["result"] == "no_response"


# Сценарий 6: отказался от коммуникации → вежливое завершение, paused
async def test_scenario_06_opt_out(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch, fields={"status": "warm"})
    state = make_state()
    await state.set_state(Dialog.open_dialog)
    farewell = "Конечно, больше не побеспокою. Всего вам доброго!"
    fake_ai.qualifications = [
        valid_qualification(
            status="non_target",
            confidence=95,
            needs_yulia=False,
            bot_response=farewell,
            status_reason="попросил больше не писать",
        )
    ]
    m = FakeMessage(make_user(), "Пожалуйста, больше не пишите мне")
    await qual.open_dialog_message(m, state, FakeBot(), config)
    # Финальная маршрутизация non_target: прощание + автоматика остановлена
    await qual._finish(
        m,
        state,
        FakeBot(),
        config,
        crm.contact,
        (
            fake_ai.qualifications[0]
            if fake_ai.qualifications
            else valid_qualification(status="non_target", confidence=95, bot_response=farewell)
        ),
    )
    assert crm.contact["fields"]["paused"] is True


# Сценарий 7: агрессия → non_target, корректный ответ, завершение
async def test_scenario_07_aggression(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.b_question_2)
    fake_ai.qualifications = [
        valid_qualification(
            status="non_target",
            confidence=93,
            bot_response="Похоже, наш формат вам не подходит. Всего доброго!",
            status_reason="агрессия и троллинг",
        )
    ]
    m = FakeMessage(make_user(), "Вы все шарлатаны!!!")
    await qual.b_answer_2(m, state, FakeBot(), config)
    assert crm.contact["fields"]["status"] == "non_target"
    assert crm.contact["fields"]["paused"] is True
    assert await state.get_state() is None


# Сценарий 8: запрос вне компетенции → non_target + рекомендация специалиста
async def test_scenario_08_out_of_scope(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    reply = (
        "Спасибо, что написали. Этот запрос выходит за рамки специализации Юлии — "
        "с подбором лечения поможет врач-психиатр. Всего вам доброго!"
    )
    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 90, "reason": "мед. запрос"}]
    m = FakeMessage(make_user(), "Какие таблетки попить от депрессии?")
    await qual.first_message(m, state, FakeBot(), config)
    fake_ai.qualifications = [
        valid_qualification(
            status="non_target",
            confidence=96,
            bot_response=reply,
            status_reason="медицинский запрос",
        )
    ]
    m2 = FakeMessage(make_user(), "Просто посоветуйте лекарство")
    await qual.b_answer_2(m2, state, FakeBot(), config)
    assert "врач" in m2.sent[0]
    assert crm.contact["fields"]["status"] == "non_target"


# Сценарий 9: один человек из нескольких источников → одна запись
async def test_scenario_09_multi_source_dedup(client, fake):
    """Проверка на уровне Airtable-клиента (FakeAirtable из conftest)."""
    await client.upsert_contact(TID, {"name": "Анна", "source": "telegram_comment"})
    await client.upsert_contact(TID, {"source": "telegram_dm"})
    await client.upsert_contact(TID, {"source": "referral"})
    assert len(fake.tables["Contacts"]) == 1
    assert fake.tables["Contacts"][0]["fields"]["source"] == "telegram_comment"


# Сценарий 10: AI не знает ответа → «требует уточнения» → передача
async def test_scenario_10_unknown_question(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    fake_ai.scenarios = [{"scenario": "C_info", "confidence": 91, "reason": "вопрос"}]
    fake_ai.answers = [{"answer": "", "needs_yulia": True, "reason": "нет в базе знаний"}]
    fake_ai.qualifications = [valid_qualification(confidence=90)]
    m = FakeMessage(make_user(), "Делаете ли вы гороскопы совместимости?")
    await qual.first_message(m, state, FakeBot(), config)
    assert m.sent == [texts.HANDOFF_MESSAGE]
    assert crm.contact["fields"]["assigned_to"] == "yulia"


# Сценарий 11: человек уже клиент → квалификация заново не запускается
async def test_scenario_11_existing_client(monkeypatch, config):
    """Клиент в работе (status=in_progress) держится на Юлии/паузе —
    middleware останавливает автоматику, сообщение сохраняется."""
    from tests.test_block7_admin import FakeMessage as MWMessage, make_middleware_call

    crm, bot, message, state, handler_called, _ = await make_middleware_call(
        monkeypatch, config, {"status": "in_progress", "assigned_to": "yulia", "name": "Анна"}
    )
    assert handler_called == []  # AI-квалификация не запускалась
    history = json.loads(crm.contact["fields"]["conversation_history"])
    assert history  # сообщение не потеряно


# Сценарий 12: пришёл по рекомендации → source=referral, обычный флоу
async def test_scenario_12_referral(monkeypatch):
    from tests.test_block5_start import AirtableCalls, FakeMessage as StartMessage, make_user as su

    calls = AirtableCalls(monkeypatch, existing=None)
    from bot.handlers import start as start_handler

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    m = StartMessage(su(), "/start referral")
    await start_handler.cmd_start(m, state)
    assert calls.upserts[0][1]["source"] == "referral"
    assert m.sent[0][0] == texts.GREETING


# Сценарий 13: не хочет общаться с ботом → немедленная передача без уговоров
async def test_scenario_13_refuses_bot(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.b_question_2)
    fake_ai.qualifications = [
        valid_qualification(needs_yulia=True, needs_yulia_reason="не хочет говорить с ботом")
    ]
    m = FakeMessage(make_user(), "Я не хочу разговаривать с ботом")
    await qual.b_answer_2(m, state, FakeBot(), config)
    assert m.sent == [texts.HANDOFF_MESSAGE]  # без уговоров — сразу передача
    assert crm.contact["fields"]["assigned_to"] == "yulia"


# Сценарий 14: API недоступен → retry → корректное сообщение, бот жив
async def test_scenario_14_api_down(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    fake_ai.scenarios = [None]  # AI окончательно недоступен
    m = FakeMessage(make_user(), "Здравствуйте!")
    await qual.first_message(m, state, FakeBot(), config)
    assert m.sent == [texts.TECH_ERROR]
    assert any("Проверить диалог" in t for t in crm.tasks)  # уведомление Юлии


# Сценарий 15: чувствительная информация → передача Юлии без интерпретаций
async def test_scenario_15_sensitive_info(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.b_question_2)
    fake_ai.qualifications = [
        valid_qualification(
            needs_yulia=True,
            needs_yulia_reason="эмоционально тяжёлая ситуация",
            summary="Клиент сообщил о тяжёлой личной ситуации (без деталей).",
        )
    ]
    m = FakeMessage(make_user(), "Недавно пережила очень тяжёлое событие в семье...")
    await qual.b_answer_2(m, state, FakeBot(), config)
    assert m.sent == [texts.HANDOFF_MESSAGE]
    assert crm.contact["fields"]["assigned_to"] == "yulia"


# ── Дополнительные технические тесты (Блок 12) ──


async def test_tech_dedup_10_messages(client, fake):
    """10 сообщений → 1 контакт."""
    for i in range(10):
        await client.upsert_contact(TID, {"name": "Анна"})
    assert len(fake.tables["Contacts"]) == 1
    assert fake.tables["Contacts"][0]["fields"]["touches_count"] == 10


async def test_tech_10_concurrent_dialogs(client, fake):
    """10 одновременных диалогов разных людей — все создаются, без ошибок."""
    await asyncio.gather(*(client.upsert_contact(1000 + i, {"name": f"К{i}"}) for i in range(10)))
    assert len(fake.tables["Contacts"]) == 10


async def test_tech_5000_char_message(monkeypatch, fake_ai, config):
    """Сообщение на 5000 символов не ломает конвейер."""
    CRM(monkeypatch)
    state = make_state()
    await state.set_state(Dialog.waiting_first_message)
    fake_ai.scenarios = [{"scenario": "B_problem", "confidence": 90, "reason": "ок"}]
    m = FakeMessage(make_user(), "х" * 5000)
    await qual.first_message(m, state, FakeBot(), config)
    assert m.sent == [texts.QUESTION_2]


async def test_tech_restart_recovery_mid_dialog(monkeypatch, fake_ai, config):
    """Рестарт бота во время диалога: состояние потеряно, но клиент не в тишине."""
    crm = CRM(monkeypatch, fields={"qualification_completed": True, "status": "warm"})
    state = make_state()  # состояния нет — как после рестарта
    fake_ai.qualifications = [valid_qualification(status="warm", confidence=90)]
    m = FakeMessage(make_user(), "Я подумала и хочу продолжить")
    await qual.restore_after_restart(m, state, FakeBot(), config)
    assert m.sent  # ответ получен
    assert await state.get_state() == Dialog.open_dialog.state


async def test_tech_restart_recovery_unqualified(monkeypatch, fake_ai, config):
    crm = CRM(monkeypatch)
    state = make_state()
    fake_ai.scenarios = [{"scenario": "C_info", "confidence": 90, "reason": "вопрос"}]
    fake_ai.answers = [{"answer": "Ответ по базе", "needs_yulia": False, "reason": None}]
    m = FakeMessage(make_user(), "Что такое диагностика?")
    await qual.restore_after_restart(m, state, FakeBot(), config)
    assert m.sent == ["Ответ по базе"]
