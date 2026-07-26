"""FSM квалификации: сценарии A/B/C и маршрутизация (Блок 6).

Источник: «Логика принятия решений AI v1.0», разделы 4–7.

- A «Готовность записаться»: ровно два вопроса → status=hot → немедленная
  передача Юлии. Дополнительная квалификация не проводится.
- B «Описывает проблему»: первое сообщение = ответ на вопрос 1 (его задаёт
  приветствие), дальше вопросы 2–4 и вопрос 5 только при недостатке
  информации. Один вопрос за раз; квалификация завершается досрочно,
  как только информации достаточно (confidence ≥ порога).
- C «Информационный интерес»: ответ по базе знаний без квалификации;
  появился собственный запрос → переключение на сценарий B.

После каждого ответа: conversation_history + Touch. Все финальные решения
пишутся в ai_decisions.log в обязательном формате (Блок 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, User

from bot import texts
from bot.config import Config
from bot.services import airtable
from bot.services.ai import get_ai
from bot.services.notifier import handoff_to_yulia
from bot.states import Dialog
from bot.utils.logger import get_app_logger, log_ai_decision

logger = get_app_logger()

router = Router(name="qualification")

# Состояния, в которых идёт содержательный диалог (для catch-all нетекста)
DIALOG_STATES = (
    Dialog.waiting_first_message,
    Dialog.a_question_1,
    Dialog.a_question_2,
    Dialog.b_question_2,
    Dialog.b_question_3,
    Dialog.b_question_4,
    Dialog.b_question_5,
    Dialog.c_info,
    Dialog.open_dialog,
)


async def _reply_safe(message: Message, text: str) -> None:
    try:
        await message.answer(text)
    except Exception:
        user = getattr(message, "from_user", None)
        logger.exception(
            "Не удалось отправить сообщение telegram_id=%s", getattr(user, "id", "unknown")
        )


async def _get_or_create_contact(user: User) -> dict | None:
    """Контакт для диалога; ``None`` — Airtable недоступен."""
    ok, record = await airtable.find_contact_checked(user.id)
    if not ok:
        return None
    if record is not None:
        return record
    # Контакт потерялся (бот перезапущен до создания записи) — восстанавливаем
    data = {"name": user.full_name, "source": "telegram_dm", "first_action": "message"}
    if user.username:
        data["username"] = f"@{user.username}"
    return await airtable.upsert_contact(user.id, data)


def _history_from(contact: dict) -> list[dict]:
    raw = contact.get("fields", {}).get("conversation_history") or "[]"
    try:
        history = json.loads(raw)
        return history if isinstance(history, list) else []
    except (json.JSONDecodeError, TypeError):
        logger.warning("conversation_history повреждена у %s", contact.get("id"))
        return []


async def _append_history(
    contact: dict, role: str, text: str, extra_updates: dict | None = None
) -> list[dict]:
    """Дописывает реплику в conversation_history (полная переписка, ТЗ Часть 3)."""
    history = _history_from(contact)
    history.append(
        {
            "role": role,
            "text": text,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    await airtable.update_contact(
        contact["id"],
        {
            "conversation_history": json.dumps(history, ensure_ascii=False),
            **(extra_updates or {}),
        },
    )
    contact.setdefault("fields", {})["conversation_history"] = json.dumps(
        history, ensure_ascii=False
    )
    return history


async def _save_client_turn(contact: dict, message_text: str, touch_description: str) -> list:
    # last_contact_date обновляется КАЖДЫМ сообщением клиента — от него
    # считаются таймауты 24/72 ч; иначе напоминание пришло бы посреди
    # живого диалога (ТЗ, Блок 6: «нет ответа N часов»)
    history = await _append_history(
        contact,
        "client",
        message_text,
        {"last_contact_date": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    )
    telegram_id = int(contact["fields"].get("telegram_id") or 0)
    await airtable.add_touch(
        telegram_id, "question_answered", touch_description, raw_content=message_text[:1000]
    )
    return history


async def _contact_for_dialog(message: Message) -> dict | None:
    """Контакт для содержательного диалога; ``None`` — диалог не продолжать
    (уже отвечено или сообщение сохранено молча).

    Страховка от гонки: если middleware pause_check пропустил сообщение
    из-за сбоя Airtable, а к этому моменту CRM ответила — переданный Юлии
    клиент НЕ должен получить автоматику (ТЗ Юлии, п. 6).
    """
    user = message.from_user
    if user is None:
        return None
    contact = await _get_or_create_contact(user)
    if contact is None:
        await _reply_safe(message, texts.TECH_ERROR)
        return None
    fields = contact.get("fields", {})
    from bot.utils.helpers import automation_stopped

    if automation_stopped(fields):
        text = message.text or message.caption or f"<{message.content_type}>"
        await _append_history(contact, "client", text)
        await airtable.add_touch(
            user.id,
            "dm_start",
            "Сообщение от переданного клиента (поймано хендлером)",
            raw_content=text[:1000],
        )
        logger.info("Клиент %s у Юлии — автоматика в хендлере не запущена", user.id)
        return None
    return contact


async def _send_bot_turn(message: Message, contact: dict, text: str) -> None:
    await _reply_safe(message, text)
    await _append_history(contact, "bot", text)


async def _ai_error(message: Message, contact: dict) -> None:
    """Окончательный сбой AI: клиенту TECH_ERROR, Юлии задача (ТЗ, Блок 4)."""
    fields = contact.get("fields", {})
    await airtable.create_task(
        f"Проверить диалог с {fields.get('name') or fields.get('telegram_id')}",
        "yulia",
        datetime.now(timezone.utc).date().isoformat(),
        "Ошибка обработки AI: ответ не получен или не прошёл валидацию",
        contact_telegram_id=fields.get("telegram_id"),
    )
    await _reply_safe(message, texts.TECH_ERROR)


def _log_decision(fields: dict, qualification: dict, response_sent: str) -> None:
    log_ai_decision(
        telegram_id=int(fields.get("telegram_id") or 0),
        scenario=qualification.get("scenario") or "—",
        status=qualification.get("status") or "—",
        confidence=int(qualification.get("confidence") or 0),
        reason=qualification.get("status_reason") or "",
        awareness=qualification.get("awareness") or "—",
        readiness=qualification.get("readiness") or "—",
        urgency=qualification.get("urgency") or "—",
        next_action=qualification.get("next_action") or "",
        needs_yulia=bool(qualification.get("needs_yulia")),
        response_sent=response_sent[:200],
    )


async def _store_qualification(contact: dict, qualification: dict) -> None:
    """Результаты квалификации → Contacts (для warm/cold, без передачи)."""
    updates = {
        "qualification_completed": True,
        "request_summary": qualification.get("summary") or "",
        "key_phrases": "\n".join(qualification.get("key_phrases") or []),
        "interests": "\n".join(qualification.get("interests") or []),
        "ai_confidence": int(qualification.get("confidence") or 0),
        "next_step": (qualification.get("next_action") or "")[:200],
    }
    for axis in ("awareness", "readiness", "urgency"):
        if qualification.get(axis):
            updates[axis] = qualification[axis]
    if qualification.get("scenario"):
        updates["scenario"] = qualification["scenario"]
    if qualification.get("product_interest"):
        updates["product_interest"] = qualification["product_interest"]
    await airtable.update_contact(contact["id"], updates)


async def _finish(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    contact: dict,
    qualification: dict,
) -> None:
    """Маршрутизация после квалификации (таблица из ТЗ, Блок 6)."""
    fields = contact.get("fields", {})
    status = qualification.get("status") or "warm"
    state_data = await state.get_data()
    # Модель могла вернуть лишний ключ answers не-словарём — не даём упасть
    if not isinstance(qualification.get("answers"), dict):
        qualification["answers"] = {}
    qualification["answers"].update(
        {
            "tried": state_data.get("q3"),
            "goal": state_data.get("q4"),
        }
    )

    if qualification.get("needs_yulia") or status == "hot":
        # Немедленная передача: CRM, задача, карточка — notifier;
        # клиенту — сообщение из ТЗ; FSM завершается (дальше — pause_check)
        _log_decision(fields, qualification, texts.HANDOFF_MESSAGE)
        await handoff_to_yulia(
            bot,
            config.telegram_admin_id,
            contact,
            qualification,
            confidence_threshold=config.ai_confidence_threshold,
        )
        await _send_bot_turn(message, contact, texts.HANDOFF_MESSAGE)
        await state.clear()
        return

    bot_response = qualification.get("bot_response") or texts.NEUTRAL_FALLBACK
    _log_decision(fields, qualification, bot_response)
    old_status = fields.get("status", "cold")
    if status != old_status:
        from bot.utils.helpers import status_locked_by_yulia

        if status_locked_by_yulia(fields):
            logger.info("Статус %s заблокирован Юлией — не меняю", fields.get("telegram_id"))
        else:
            await airtable.add_status_change(
                contact["id"], old_status, status, qualification.get("status_reason") or "", "ai"
            )
    await _store_qualification(contact, qualification)
    await airtable.add_touch(
        int(fields.get("telegram_id") or 0),
        "qualified",
        f"Квалификация завершена: {status} ({qualification.get('status_reason', '')[:200]})",
    )

    if status == "non_target":
        # Алгоритм нецелевого обращения выполняет bot_response (4 шага);
        # автоматика останавливается (ТЗ: paused=true)
        await airtable.update_contact(contact["id"], {"paused": True})
        await _send_bot_turn(message, contact, bot_response)
        await state.clear()
        return

    # warm / cold: ответить, продолжать диалог без давления
    await _send_bot_turn(message, contact, bot_response)
    await state.set_state(Dialog.open_dialog)


async def _intermediate_step(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    contact: dict,
    history: list,
    next_question: str | None,
    next_state,
) -> None:
    """Шаг сценария B: проверка достаточности → следующий вопрос или финал.

    Промежуточная квалификация (final=False): низкая уверенность = «мало
    информации, спрашиваем дальше», а не передача Юлии. Триггеры передачи
    (needs_yulia от модели), hot и non_target завершают квалификацию сразу.
    """
    qualification = await get_ai().qualify(history, final=(next_question is None))
    if qualification is None:
        await _ai_error(message, contact)
        return
    if (
        qualification.get("status") == "non_target"
        and int(qualification.get("confidence") or 0) < config.ai_confidence_threshold
    ):
        # Неуверенный вердикт «нецелевой» не имеет права закрыть диалог:
        # «AI не уверен → немедленная передача» (ТЗ, Блок 6) — решает Юлия
        qualification["needs_yulia"] = True
        if not qualification.get("needs_yulia_reason"):
            qualification["needs_yulia_reason"] = "Требуется экспертная оценка"
    enough = (
        qualification.get("needs_yulia")
        or qualification.get("status") in ("hot", "non_target")
        or int(qualification.get("confidence") or 0) >= config.ai_confidence_threshold
        or next_question is None
    )
    if enough:
        await _finish(message, state, bot, config, contact, qualification)
        return
    await _send_bot_turn(message, contact, next_question)
    await state.set_state(next_state)


# ── Первое содержательное сообщение: определение сценария ──


@router.message(Dialog.waiting_first_message, F.text)
async def first_message(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    user = message.from_user
    if user is None:
        return
    contact = await _contact_for_dialog(message)
    if contact is None:
        return
    await _save_client_turn(contact, message.text, "Первое содержательное сообщение")

    scenario = await get_ai().detect_scenario(message.text)
    if scenario is None:
        await _ai_error(message, contact)
        return
    kind = scenario["scenario"]
    logger.info(
        "Сценарий telegram_id=%s: %s (%s%%) — %s",
        user.id,
        kind,
        scenario.get("confidence"),
        scenario.get("reason"),
    )
    await airtable.update_contact(contact["id"], {"scenario": kind})
    await state.update_data(q1=message.text, scenario=kind)

    if kind == "A_ready":
        await _send_bot_turn(message, contact, texts.A_INTRO)
        await state.set_state(Dialog.a_question_1)
    elif kind == "B_problem":
        # Первое сообщение — уже ответ на вопрос 1 (его задало приветствие)
        await _send_bot_turn(message, contact, texts.QUESTION_2)
        await state.set_state(Dialog.b_question_2)
    else:  # C_info
        await _answer_info_question(message, state, bot, config, contact)


async def _answer_info_question(
    message: Message, state: FSMContext, bot: Bot, config: Config, contact: dict
) -> None:
    """Сценарий C: ответить по базе знаний, квалификацию не начинать."""
    history = _history_from(contact)
    history_text = "\n".join(f"{t.get('role')}: {t.get('text')}" for t in history[-10:-1])
    answer = await get_ai().answer_info(message.text, history=history_text)
    if answer is None:
        await _ai_error(message, contact)
        return
    if answer.get("needs_yulia"):
        # Вопрос за пределами базы знаний или триггер передачи —
        # собираем карточку полной квалификацией и передаём
        qualification = await get_ai().qualify(_history_from(contact), final=True)
        if qualification is None:
            qualification = {
                "status": contact["fields"].get("status", "warm"),
                "summary": (message.text or "")[:300],
                "needs_yulia": True,
                "needs_yulia_reason": answer.get("reason") or "Вопрос требует уточнения",
                "confidence": 0,
                "scenario": "C_info",
            }
        qualification["needs_yulia"] = True
        await _finish(message, state, bot, config, contact, qualification)
        return
    await _send_bot_turn(message, contact, answer["answer"])
    await state.set_state(Dialog.c_info)


# ── Сценарий A: два вопроса → передача ──


@router.message(Dialog.a_question_1, F.text)
async def a_answer_1(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    contact = await _contact_for_dialog(message)
    if contact is None:
        return
    await _save_client_turn(contact, message.text, "Сценарий A, ответ на вопрос 1")
    await state.update_data(a1=message.text)
    # Вопрос 2 сценария A — дословно из ТЗ («достичь», не «получить»)
    await _send_bot_turn(message, contact, texts.A_QUESTION_2)
    await state.set_state(Dialog.a_question_2)


@router.message(Dialog.a_question_2, F.text)
async def a_answer_2(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    user = message.from_user
    if user is None:
        return
    contact = await _contact_for_dialog(message)
    if contact is None:
        return
    await _save_client_turn(contact, message.text, "Сценарий A, ответ на вопрос 2")
    # Ответ на «Какого результата хотите достичь?» → секция «ЧЕГО ХОЧЕТ» карточки
    await state.update_data(q4=message.text)

    # Квалификация нужна только для карточки; статус — hot безусловно
    # (ТЗ, сценарий A: доп. квалификация не проводится, немедленная передача)
    qualification = await get_ai().qualify(_history_from(contact), final=False)
    if qualification is None:
        data = await state.get_data()
        qualification = {
            "summary": f"Готов записаться. Ситуация: {data.get('a1', '—')[:200]}. "
            f"Желаемый результат: {message.text[:200]}",
            "key_phrases": [],
            "status_reason": "готовность записаться (сценарий A)",
            "confidence": 100,
        }
    qualification["status"] = "hot"
    qualification["needs_yulia"] = True
    qualification.setdefault("needs_yulia_reason", "Готов записаться (сценарий A)")
    qualification["scenario"] = "A_ready"
    await _finish(message, state, bot, config, contact, qualification)


# ── Сценарий B: вопросы 2–5 с досрочным завершением ──


async def _b_step(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    data_key: str,
    touch_note: str,
    next_question: str | None,
    next_state,
) -> None:
    user = message.from_user
    if user is None:
        return
    contact = await _contact_for_dialog(message)
    if contact is None:
        return
    history = await _save_client_turn(contact, message.text, touch_note)
    await state.update_data(**{data_key: message.text})
    await _intermediate_step(
        message, state, bot, config, contact, history, next_question, next_state
    )


@router.message(Dialog.b_question_2, F.text)
async def b_answer_2(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    await _b_step(
        message,
        state,
        bot,
        config,
        "q2",
        "Сценарий B, ответ на вопрос 2",
        texts.QUESTION_3,
        Dialog.b_question_3,
    )


@router.message(Dialog.b_question_3, F.text)
async def b_answer_3(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    await _b_step(
        message,
        state,
        bot,
        config,
        "q3",
        "Сценарий B, ответ на вопрос 3",
        texts.QUESTION_4,
        Dialog.b_question_4,
    )


@router.message(Dialog.b_question_4, F.text)
async def b_answer_4(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    # Вопрос 5 задаётся только если информации недостаточно —
    # решает _intermediate_step по confidence
    await _b_step(
        message,
        state,
        bot,
        config,
        "q4",
        "Сценарий B, ответ на вопрос 4",
        texts.QUESTION_5,
        Dialog.b_question_5,
    )


@router.message(Dialog.b_question_5, F.text)
async def b_answer_5(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    await _b_step(
        message,
        state,
        bot,
        config,
        "q5",
        "Сценарий B, ответ на вопрос 5",
        None,
        None,
    )


# ── Сценарий C: информационный диалог ──


@router.message(Dialog.c_info, F.text)
async def c_message(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    """Появился собственный запрос → сценарий B; иначе продолжаем отвечать."""
    user = message.from_user
    if user is None:
        return
    contact = await _contact_for_dialog(message)
    if contact is None:
        return
    await _save_client_turn(contact, message.text, "Сообщение в информационном диалоге")

    scenario = await get_ai().detect_scenario(message.text)
    if scenario is None:
        await _ai_error(message, contact)
        return
    kind = scenario["scenario"]
    if kind == "A_ready":
        await state.update_data(q1=message.text, scenario=kind)
        await airtable.update_contact(contact["id"], {"scenario": kind})
        await _send_bot_turn(message, contact, texts.A_INTRO)
        await state.set_state(Dialog.a_question_1)
    elif kind == "B_problem":
        await state.update_data(q1=message.text, scenario=kind)
        await airtable.update_contact(contact["id"], {"scenario": kind})
        await _send_bot_turn(message, contact, texts.QUESTION_2)
        await state.set_state(Dialog.b_question_2)
    else:
        await _answer_info_question(message, state, bot, config, contact)


# ── Свободный диалог после квалификации (warm/cold) ──


@router.message(Dialog.open_dialog, F.text)
async def open_dialog_message(
    message: Message, state: FSMContext, bot: Bot, config: Config
) -> None:
    """Продолжение общения: возражения, дозревание, триггеры передачи."""
    user = message.from_user
    if user is None:
        return
    contact = await _contact_for_dialog(message)
    if contact is None:
        return
    history = await _save_client_turn(contact, message.text, "Сообщение после квалификации")

    qualification = await get_ai().qualify(history, final=True)
    if qualification is None:
        await _ai_error(message, contact)
        return
    if qualification.get("needs_yulia") or qualification.get("status") == "hot":
        await _finish(message, state, bot, config, contact, qualification)
        return
    # Статус может дозреть (cold → warm) — фиксируем, отвечаем, остаёмся
    fields = contact.get("fields", {})
    old_status = fields.get("status")
    new_status = qualification.get("status")
    if new_status and new_status != old_status:
        from bot.utils.helpers import status_locked_by_yulia

        if not status_locked_by_yulia(fields):
            await airtable.add_status_change(
                contact["id"],
                old_status or "cold",
                new_status,
                qualification.get("status_reason") or "",
                "ai",
            )
    bot_response = qualification.get("bot_response") or texts.NEUTRAL_FALLBACK
    _log_decision(fields, qualification, bot_response)
    await _send_bot_turn(message, contact, bot_response)


# ── Восстановление после рестарта: сообщение без состояния FSM ──


@router.message(StateFilter(None), F.text)
async def restore_after_restart(
    message: Message, state: FSMContext, bot: Bot, config: Config
) -> None:
    """Личное сообщение без состояния FSM (рестарт бота / клиент без /start).

    MemoryStorage теряет состояния при перезапуске — клиент посреди диалога
    не должен получать тишину (Блок 12: «рестарт бота во время диалога»).
    Квалифицированные продолжают свободный диалог, остальные — с определения
    сценария. Переданные Юлии сюда не дойдут (middleware pause_check).
    """
    if message.chat.type != "private" or message.from_user is None:
        return
    if (message.text or "").startswith("/"):
        return  # неизвестные команды — не наша зона
    if message.from_user.id == config.telegram_admin_id:
        return  # Юлия не клиент — не заводим на неё контакт
    contact = await _get_or_create_contact(message.from_user)
    if contact is None:
        await _reply_safe(message, texts.TECH_ERROR)
        return
    if contact.get("fields", {}).get("qualification_completed"):
        await state.set_state(Dialog.open_dialog)
        await open_dialog_message(message, state, bot, config)
    else:
        await state.set_state(Dialog.waiting_first_message)
        await first_message(message, state, bot, config)


# ── Нетекстовые сообщения в любом состоянии диалога ──


@router.message(StateFilter(*DIALOG_STATES))
async def non_text_in_dialog(message: Message) -> None:
    """Голос/стикер/фото в диалоге: просим текст, ничего не теряем.

    Регистрируется ПОСЛЕ текстовых хендлеров — сюда попадает только нетекст.
    """
    user = message.from_user
    if user is None:
        return
    contact = await _get_or_create_contact(user)
    if contact is not None:
        raw = message.caption or f"<{message.content_type}>"
        await airtable.add_touch(
            int(contact["fields"].get("telegram_id") or 0),
            "question_answered",
            "Нетекстовое сообщение в диалоге",
            raw_content=raw,
        )
    await _reply_safe(message, texts.ASK_TEXT_PLEASE)
