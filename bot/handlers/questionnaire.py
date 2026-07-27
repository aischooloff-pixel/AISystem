"""Анкета перед системной диагностикой «Точка сбоя» (Блок 11).

Источник: «Анкета перед системной диагностикой "Точка сбоя" v1.0».
Блок включён в Спринт 1 по подтверждению Юлии от 2026-07-27.

Запускает анкету Юлия командой ``/anketa {telegram_id}`` — сама система
её не рассылает. Причина: анкету заполняют уже переданные клиенты, для
которых автоматика остановлена (Блок 7), и решение «пора заполнять» —
экспертное, а значит принимает его человек (Конституция, принцип 5).

Роль AI строго ограничена ТЗ: сохранить ответы, выделить основной запрос,
собрать дословные формулировки и подготовить отчёт Юлии.
Диагностику AI не проводит, ответы не интерпретирует, выводов о причинах
ситуации не делает.
"""

from __future__ import annotations

import json

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.config import Config
from bot.keyboards.user import CONSENT_CALLBACK_PREFIX, consent_keyboard
from bot.prompts.questionnaire_analyzer import FORM_LABELS
from bot.services import airtable
from bot.services.ai import get_ai
from bot.services.notifier import build_pre_meeting_report, notify_yulia
from bot.states import Questionnaire
from bot.utils.logger import get_app_logger

logger = get_app_logger()

router = Router(name="questionnaire")

# Состояние → текст вопроса и следующее состояние. Один вопрос за раз:
# следующий не задаётся, пока нет ответа на предыдущий (ТЗ, Блок 6, правила).
FORM_STEPS: tuple[tuple, ...] = (
    (Questionnaire.q1, texts.Q_FORM_2, Questionnaire.q2),
    (Questionnaire.q2, texts.Q_FORM_3, Questionnaire.q3),
    (Questionnaire.q3, texts.Q_FORM_4, Questionnaire.q4),
    (Questionnaire.q4, texts.Q_FORM_5, Questionnaire.q5),
    (Questionnaire.q5, texts.Q_FORM_6, Questionnaire.q6),
    (Questionnaire.q6, texts.Q_FORM_7, Questionnaire.q7),
)

FORM_STATES = (
    Questionnaire.q1,
    Questionnaire.q2,
    Questionnaire.q3,
    Questionnaire.q4,
    Questionnaire.q5,
    Questionnaire.q6,
    Questionnaire.q7,
)


def client_state(bot: Bot, state: FSMContext, telegram_id: int) -> FSMContext:
    """FSM-контекст клиента из контекста Юлии.

    Команду ``/anketa`` выполняет Юлия, а состояние нужно выставить клиенту:
    берём то же хранилище, но с ключом клиента.
    """
    return FSMContext(
        storage=state.storage,
        key=StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id),
    )


async def send_questionnaire(bot: Bot, state: FSMContext, telegram_id: int) -> bool:
    """Отправляет клиенту вступление и первый вопрос. ``False`` — не доставлено."""
    target = client_state(bot, state, telegram_id)
    try:
        await bot.send_message(telegram_id, texts.QUESTIONNAIRE_INTRO)
        await bot.send_message(telegram_id, texts.Q_FORM_1)
    except Exception:
        logger.exception("Не удалось отправить анкету клиенту %s", telegram_id)
        return False
    # Прошлые ответы затираем только после успешной отправки
    await target.set_state(Questionnaire.q1)
    await target.update_data(form_answers=[])
    logger.info("Анкета «Точка сбоя» отправлена клиенту %s", telegram_id)
    return True


async def _store_answer(state: FSMContext, text: str) -> list[str]:
    data = await state.get_data()
    answers = list(data.get("form_answers") or [])
    answers.append(text.strip())
    await state.update_data(form_answers=answers)
    return answers


@router.message(StateFilter(*FORM_STATES))
async def form_answer(message: Message, state: FSMContext) -> None:
    """Ответ на вопрос анкеты: сохранить и задать следующий."""
    if not message.text:
        # Голос/стикер/фото: вопрос не засчитываем, состояние не двигаем
        await message.answer(texts.ASK_TEXT_PLEASE)
        return
    current = await state.get_state()
    await _store_answer(state, message.text)

    for form_state, next_question, next_state in FORM_STEPS:
        if current == form_state.state:
            await state.set_state(next_state)
            await message.answer(next_question)
            return

    # Отвечен вопрос 7 — остаётся согласие на запись
    await state.set_state(Questionnaire.consent)
    await message.answer(texts.Q_FORM_CONSENT, reply_markup=consent_keyboard())
    logger.info(
        "Анкета %s: получены все 7 ответов, запрошено согласие на запись",
        message.from_user.id if message.from_user else "unknown",
    )


@router.callback_query(Questionnaire.consent, F.data.startswith(CONSENT_CALLBACK_PREFIX))
async def consent_answer(
    callback: CallbackQuery, state: FSMContext, bot: Bot, config: Config
) -> None:
    """Согласие на запись — последний шаг: сохранить анкету и отчитаться Юлии."""
    await callback.answer()
    if callback.from_user is None:
        return
    telegram_id = callback.from_user.id
    consent = (callback.data or "").removeprefix(CONSENT_CALLBACK_PREFIX) == "yes"

    data = await state.get_data()
    answers = list(data.get("form_answers") or [])
    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Не удалось убрать кнопки согласия у %s", telegram_id)

    # Разбор анкеты — вспомогательный: при сбое OpenAI отчёт всё равно уходит
    analysis = None
    try:
        analysis = await get_ai().analyze_questionnaire(answers)
    except Exception:
        logger.exception("Разбор анкеты %s не удался — отчёт из сырых ответов", telegram_id)

    questionnaire_json = json.dumps(
        [
            {"question": label, "answer": answers[index] if index < len(answers) else ""}
            for index, label in enumerate(FORM_LABELS)
        ],
        ensure_ascii=False,
    )
    await airtable.create_diagnostic(
        telegram_id,
        {
            "questionnaire": questionnaire_json,
            "recording_consent": consent,
            "booking_status": "requested",
        },
    )
    await airtable.add_touch(
        telegram_id,
        "question_answered",
        "Заполнил(а) анкету «Точка сбоя» перед диагностикой",
    )

    contact = await airtable.find_contact(telegram_id)
    fields = (contact or {}).get("fields", {})
    report = build_pre_meeting_report(fields, answers, consent, analysis)
    await notify_yulia(bot, config.telegram_admin_id, report)

    try:
        await callback.message.answer(texts.QUESTIONNAIRE_DONE)
    except Exception:
        logger.exception("Не удалось подтвердить клиенту %s заполнение анкеты", telegram_id)
    logger.info(
        "Анкета %s сохранена (согласие на запись: %s), отчёт отправлен Юлии", telegram_id, consent
    )
