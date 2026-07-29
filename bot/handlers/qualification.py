"""FSM квалификации: сценарии A/B/C и маршрутизация (Блок 6).

Источник: «Логика принятия решений AI v1.0», разделы 4–7.

- A «Готовность записаться»: ровно два вопроса → status=hot → немедленная
  передача Юлии. Дополнительная квалификация не проводится.
- B «Описывает проблему»: первое сообщение = ответ на вопрос 1 (его задаёт
  приветствие), затем вопрос о давности ситуации (добавлен Юлией 2026-07-28)
  и вопросы 2–4; вопрос 5 — только при недостатке информации. Один вопрос
  за раз. Прежде чем решать судьбу лида, бот обязан собрать MANDATORY_ANSWERS
  ответов: ниже этого порога разговор обрывают ТОЛЬКО слова самого клиента
  (просит человека, хочет записаться, называет острое состояние) либо
  согласованный обоими вызовами модели вывод «запрос не наш». Уверенность
  в статусе таким словом не является — она измеряет уверенность в статусе,
  а не полноту собранной картины.
  Формулировку каждого вопроса подбирает модель под сказанное человеком;
  тема вопроса закреплена сценарием, заскриптованный текст — запасной.
- C «Информационный интерес»: ответ по базе знаний без квалификации;
  появился собственный запрос → переключение на сценарий B.

После каждого ответа: conversation_history + Touch. Все финальные решения
пишутся в ai_decisions.log в обязательном формате (Блок 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
from bot.utils import validators
from bot.utils.logger import get_app_logger, log_ai_decision

logger = get_app_logger()

router = Router(name="qualification")

# Ответы сценария B, из которых складывается картина для Юлии.
B_ANSWER_KEYS = ("duration", "q2", "q3", "q4", "q5")

# Сколько ответов бот обязан собрать, прежде чем решать судьбу лида.
# Ниже этого порога квалификацию обрывают только слова самого клиента
# (просит человека, хочет записаться, называет острое состояние) либо
# согласованный обоими вызовами модели вывод «запрос не наш».
# Требование заказчика от 2026-07-29: «два вопроса и сразу вывод — это
# очень мало». Совпадает с FSM из ТЗ: q1 → q2 → q3 → q4 → [q5].
MANDATORY_ANSWERS = 4

# Состояния, в которых идёт содержательный диалог (для catch-all нетекста)
DIALOG_STATES = (
    Dialog.waiting_first_message,
    Dialog.a_question_1,
    Dialog.a_question_2,
    Dialog.b_duration,
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
        # .value обязателен: с Python 3.11 f-строка от enum со смешанным типом
        # даёт «ContentType.VOICE», и в CRM у Юлии оказывается имя константы
        text = message.text or message.caption or f"<{message.content_type.value}>"
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


async def _store_qualification(contact: dict, qualification: dict, review_days: int) -> None:
    """Результаты квалификации → Contacts (для warm/cold, без передачи)."""
    updates = {
        "qualification_completed": True,
        "request_summary": qualification.get("summary") or "",
        "key_phrases": "\n".join(qualification.get("key_phrases") or []),
        "interests": "\n".join(qualification.get("interests") or []),
        "ai_confidence": int(qualification.get("confidence") or 0),
        "next_step": (qualification.get("next_action") or "")[:200],
    }
    if updates["next_step"]:
        # Шаг без срока не попадёт ни в один фильтр Юлии. Горизонт берём тот же,
        # на котором check_timeouts решает судьбу тёплого клиента, — так
        # «следующее действие» и напоминание смотрят в одну дату.
        updates["next_action_date"] = (
            datetime.now(timezone.utc) + timedelta(days=review_days)
        ).isoformat(timespec="seconds")
    for axis in ("awareness", "readiness", "urgency"):
        if qualification.get(axis):
            updates[axis] = qualification[axis]
    if qualification.get("scenario"):
        updates["scenario"] = qualification["scenario"]
    if qualification.get("product_interest"):
        updates["product_interest"] = qualification["product_interest"]
    if qualification.get("request_category"):
        updates["request_category"] = qualification["request_category"]
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
            "duration": state_data.get("duration"),
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
        # Рассказ о формате встречи уместен не всегда:
        # · нецелевому обращению — незачем, его передают по другой причине;
        # · человеку в остром состоянии («мне очень плохо, не вижу выхода») —
        #   тем более: он написал о том, что ему плохо, а в ответ получил бы
        #   описание онлайн-встречи на 60 минут. Ему нужен человек, а не
        #   продукт; Юлия уже уведомлена.
        crisis = qualification.get("handoff_trigger") == "heavy_situation"
        if status != "non_target" and not crisis:
            await _send_bot_turn(message, contact, texts.HANDOFF_FOLLOWUP)
        await state.clear()
        return

    bot_response = qualification.get("bot_response") or texts.NEUTRAL_FALLBACK
    if status == "cold" and bot_response.rstrip().endswith("?"):
        # Холодный ответил «не знаю» на всю цепочку — спрашивать его снова
        # некуда. Модель это правило игнорирует и в завершающей реплике
        # опять задаёт вопрос (прод 29.07: четыре подряд «что именно вас
        # беспокоит?»). Берём утверждённый Юлией текст прогрева.
        logger.info("Холодный лид: модель снова спросила — заменяю текстом прогрева")
        bot_response = texts.NURTURING_CLOSING
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
    await _store_qualification(contact, qualification, config.nurturing_review_days)
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


# Слова, которыми модель обозначает пустоту, когда пишет их в строку вместо
# JSON null. Клиент получал в чат ровно «null» — прод 29.07.
_EMPTY_WORDS = {"null", "none", "nan", "-", "—", "нет", "n/a"}


def _adapted_or_scripted(adapted: object, scripted: str | None) -> str | None:
    """Вопрос от модели, если он настоящий; иначе — текст из сценария."""
    if isinstance(adapted, str):
        text = adapted.strip()
        if text and text.lower() not in _EMPTY_WORDS:
            return text
        logger.info("next_question пуст (%r) — беру формулировку из сценария", adapted)
    return scripted


async def _intermediate_step(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    contact: dict,
    history: list,
    next_question: str | None,
    next_state,
    answered: int,
) -> None:
    """Шаг сценария B: проверка достаточности → следующий вопрос или финал.

    Промежуточная квалификация (final=False): низкая уверенность = «мало
    информации, спрашиваем дальше», а не передача Юлии.

    Досрочно завершают квалификацию только НАБЛЮДАЕМЫЕ события: человек
    назвал признак готовности (просит записаться · спрашивает цену и даты ·
    готов оплатить · просит связаться лично · подтверждает готовность ·
    называет срок), запрос оказался нецелевым, или модель назвала причину
    немедленной передачи из раздела 11 ТЗ.

    Высокая уверенность в статусе таким событием НЕ является — и раньше
    являлась. Модель после двух реплик уверенно ставила «warm 90%», условие
    считало это «информации достаточно», и диалог обрывался на втором
    вопросе. С точки зрения ТЗ это была подмена: confidence измеряет
    уверенность в статусе, а не полноту собранной картины. FSM сценария B
    в ТЗ — ``q1 → q2 → q3 → q4 → [q5]``: в скобках только пятый вопрос,
    остальные обязательны.
    """
    qualification = await get_ai().qualify(
        history, final=(next_question is None), question_topic=next_question
    )
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
    signal = qualification.get("readiness_signal")
    trigger = qualification.get("handoff_trigger")
    # needs_yulia сюда намеренно не входит. Это свободное суждение модели,
    # и промпт велит ей передавать в том числе «когда AI не уверен» — при
    # двух репликах она не уверена всегда. Живой прод 28.07: «хочу увеличить
    # доход» → warm, confidence 85, признака готовности нет, needs_yulia=true —
    # и человек уходил Юлии после одного вопроса. Обрывают цепочку только
    # наблюдаемые события; неуверенность учитывает порог 85% в finalize().
    if not trigger and qualification.get("needs_yulia"):
        logger.info(
            "needs_yulia без названного основания — продолжаю квалификацию (причина: %s)",
            qualification.get("needs_yulia_reason") or "не названа",
        )
    # Прерывают квалификацию только основания, где клиент ЯВНО что-то сказал
    # или попросил. Основания, которые модель ВЫВОДИТ из состояния человека
    # («тяжёлая ситуация», «конфликт»), цепочку не рвут: 29.07 heavy_situation
    # прилетал на «проблемы в семье» и «выгорание» — обычные целевые запросы.
    # Флаг при этом сохраняется, и Юлия получает карточку в конце цепочки.
    # Вопрос о цене посреди квалификации — это вопрос, а не готовность.
    # Прод 29.07: «цена консультации?» вместо ответа получала «Передал
    # информацию Юлии». Отвечаем и возвращаемся к своему вопросу: позиция
    # в цепочке не двигается, счётчик ответов не растёт.
    if trigger == validators.ANSWER_FIRST_TRIGGER and next_question is not None:
        answer = await get_ai().answer_info(message.text, history=_history_text(contact))
        if answer and answer.get("answer"):
            await _send_bot_turn(message, contact, answer["answer"])
        pending = next(
            (
                turn.get("text")
                for turn in reversed(history)
                if turn.get("role") == "bot" and (turn.get("text") or "").endswith("?")
            ),
            None,
        )
        if pending:
            await _send_bot_turn(message, contact, pending)
        logger.info("Вопрос о стоимости посреди квалификации — ответил и продолжаю")
        return

    # Основания уже проверены сервисом: непроцитированные и «острое
    # состояние» без слов человека сняты там же. Здесь остаётся развести
    # явные (обрывают разговор) и выведенные моделью (только флаг).
    explicit = trigger in validators.EXPLICIT_HANDOFF_TRIGGERS
    # Вердикт «нецелевой» посреди цепочки принимается только если с ним
    # согласен детектор сценария — отдельный вызов со своим промптом.
    # Квалификатор систематически называет нецелевыми темы, которые база
    # знаний прямо относит к практике: «выгорание», «апатия», «проблемы
    # в семье». Одна оценка модели против её же базы знаний разговор
    # не закрывает; финальное решение (next_question is None) — закрывает.
    non_target_now = qualification.get("status") == "non_target"
    if non_target_now and next_question is not None:
        detected = (await state.get_data()).get("scenario")
        if detected not in (None, "non_target"):
            logger.info(
                "Квалификатор: non_target, детектор: %s — продолжаю вопросы, решаю в конце",
                detected,
            )
            non_target_now = False

    # Пол обязательных вопросов. Ниже него разговор обрывают ТОЛЬКО слова
    # самого клиента: он попросил живого человека, сказал «хочу записаться»,
    # назвал острое состояние — или это вообще не наш запрос, и с этим
    # согласны оба вызова модели. Всё остальное (оценки, уверенность,
    # «мне кажется, случай сложный») ждёт собранной картины.
    floor_reached = answered >= MANDATORY_ANSWERS
    stops_regardless = (
        qualification.get("_forced_handoff")  # стоп-фраза: решение кода, не модели
        or explicit
        or (signal not in (None, "none"))
        or non_target_now
    )
    if not floor_reached and not stops_regardless:
        logger.info(
            "Собрано %d из %d обязательных ответов — продолжаю квалификацию",
            answered,
            MANDATORY_ANSWERS,
        )
    # Уверенность вправе сократить разговор только после обязательных
    # ответов — тогда она закрывает пятый вопрос, единственный, который ТЗ
    # ставит в скобки («только если информации недостаточно»).
    confident_enough = floor_reached and (
        int(qualification.get("confidence") or 0) >= config.ai_confidence_threshold
    )
    enough = stops_regardless or confident_enough or next_question is None
    if enough:
        # Решение принимается здесь, а квалификация запрашивалась с
        # final=False (пятый вопрос ещё числился впереди). Досрочно
        # завершённый диалог обязан пройти те же финальные правила:
        # порог 85% и пометку «решение за вами».
        if next_question is not None:
            qualification = get_ai().finalize(qualification)
        await _finish(message, state, bot, config, contact, qualification)
        return
    # Формулировку следующего вопроса даёт модель, услышав ответ; тема
    # закреплена сценарием. Заскриптованный текст остаётся запасным: если
    # модель промолчала или выдала не строку, клиент всё равно получит
    # вопрос, а не тишину.
    asked = _adapted_or_scripted(qualification.get("next_question"), next_question)
    await _send_bot_turn(message, contact, asked)
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
    await _route_scenario(
        kind, message, state, bot, config, contact, scenario.get("first_question")
    )


async def _route_scenario(
    kind: str,
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    contact: dict,
    first_question: str | None = None,
) -> None:
    """Ветвление по определённому сценарию (ТЗ, Блок 6 + разделы 8 и 11).

    ``first_question`` — формулировка первого вопроса сценария B от детектора.
    """
    if kind == "A_ready":
        # Сначала ответ на то, с чем человек пришёл, потом уточнения.
        # Прод 29.07: «сколько стоит диагностика» и «хочу записаться»
        # уходили прямо в анкету — человек не получал ни цены, ни того,
        # как записаться. И цену, и порядок записи бот называть вправе
        # («Продуктовая линейка»).
        answer = await get_ai().answer_info(message.text, history=_history_text(contact))
        text = (answer or {}).get("answer") or ""
        # needs_yulia здесь не помеха: в сценарии A человек и так уйдёт Юлии
        # в конце. Модель ставит флаг на «хочу записаться» как на основание
        # передачи — и ответ о том, КАК записаться, глотался вместе с ним
        # (прод 29.07). Отсекаем только заглушку «передам Юлии».
        if text and text != texts.NEUTRAL_FALLBACK:
            await _send_bot_turn(message, contact, text)
        await _send_bot_turn(message, contact, texts.A_INTRO)
        await state.set_state(Dialog.a_question_1)
    elif kind == "B_problem":
        # Первое сообщение — уже ответ на вопрос 1 (его задало приветствие).
        # Дальше цепочка ТЗ q2 → q3 → q4 → [q5], перед ней — вопрос о давности
        # ситуации (добавлен Юлией 2026-07-28).
        #
        # Формулировку даёт детектор сценария — он уже прочитал сообщение,
        # отдельный вызов модели не нужен. Прод 29.07: после отказа по
        # тарологии клиент написал «хочу помощь в бизнесе», и бот спросил
        # «как давно длится эта ситуация?» — про ситуацию, которой ему ещё
        # не рассказали.
        asked = _adapted_or_scripted(first_question, texts.QUESTION_DURATION)
        await _send_bot_turn(message, contact, asked)
        await state.set_state(Dialog.b_duration)
    elif kind == "non_target":
        await _close_non_target(message, state, bot, config, contact)
    elif kind == "handoff":
        await _handoff_immediately(message, state, bot, config, contact)
    else:  # C_info
        await _answer_info_question(message, state, bot, config, contact)


_PRICE_WORDS = ("сколько стоит", "стоимость", "цена", "почём", "почем", "прайс")


def _looks_like_price_question(text: str | None) -> bool:
    """Вопрос о деньгах, заданный без знака вопроса («сколько стоит диагностика»)."""
    normalized = validators.normalize_for_match(text)
    return any(word in normalized for word in _PRICE_WORDS)


def _history_text(contact: dict) -> str:
    """Переписка текущего обращения строкой — контекст для справочного ответа."""
    from bot.prompts.qualifier import current_cycle

    return "\n".join(
        f"{turn.get('role')}: {turn.get('text')}" for turn in current_cycle(_history_from(contact))
    )


async def _close_non_target(
    message: Message, state: FSMContext, bot: Bot, config: Config, contact: dict
) -> None:
    """Нецелевое обращение: вежливо завершить, Юлию НЕ беспокоить.

    ТЗ, «Маршрутизация после квалификации»: non_target → «корректно ответить ·
    рекомендовать профильного специалиста · вежливо завершить · paused=true».
    Карточки на «погода на завтра» Юлия получать не должна.

    Вердикт детектора перепроверяется полной квалификацией: закрыть диалог
    имеет право только уверенный вывод. Неуверенный (confidence < порога)
    отправляется Юлии — «AI не уверен → передача» (ТЗ, Блок 6).
    """
    qualification = await get_ai().qualify(_history_from(contact), final=True)
    if qualification is None:
        await _ai_error(message, contact)
        return
    qualification.setdefault("scenario", "non_target")
    if qualification.get("status") != "non_target":
        # Квалификация не подтвердила вердикт детектора — доверяем ей:
        # она видела весь диалог, детектор — одно сообщение
        logger.info(
            "Детектор: non_target, квалификация: %s — иду по квалификации",
            qualification.get("status"),
        )
    await _finish(message, state, bot, config, contact, qualification)


async def _handoff_immediately(
    message: Message, state: FSMContext, bot: Bot, config: Config, contact: dict
) -> None:
    """Отказ говорить с ботом или просьба о живом человеке → сразу Юлии.

    ТЗ, «Обработка возражений», особый случай: «Я не хочу разговаривать
    с ботом» → немедленная передача. Никаких уговоров и никаких вопросов —
    задавать их человеку, который просил живого собеседника, значит спорить
    с ним.
    """
    qualification = await get_ai().qualify(_history_from(contact), final=False)
    if qualification is None:
        qualification = {
            "summary": (message.text or "")[:300],
            "key_phrases": [],
            "status_reason": "Просит живого человека",
            "confidence": 100,
            "status": contact.get("fields", {}).get("status") or "warm",
        }
    qualification["needs_yulia"] = True
    qualification.setdefault("needs_yulia_reason", "Просит живого человека, не хочет говорить с AI")
    qualification["scenario"] = "handoff"
    if qualification.get("status") == "non_target":
        # Целевой человек, которому нужен живой собеседник, — не нецелевое
        # обращение: иначе _finish закрыл бы диалог вместо передачи
        qualification["status"] = "warm"
    await _finish(message, state, bot, config, contact, qualification)


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
        # Передаём Юлии всё, КРОМЕ уверенно нецелевого. «Нет ответа в базе
        # знаний» — законное основание передачи (ТЗ, раздел 11), но у вопроса
        # про погоду ответа в базе знаний нет и не будет: до этой оговорки
        # безусловное needs_yulia=True гнало Юлии каждое постороннее сообщение.
        if not (
            qualification.get("status") == "non_target"
            and int(qualification.get("confidence") or 0) >= config.ai_confidence_threshold
        ):
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
    history = await _save_client_turn(contact, message.text, "Сценарий A, ответ на вопрос 1")
    await state.update_data(a1=message.text)
    # Тема вопроса 2 — из ТЗ («достичь», не «получить»), формулировку
    # подбирает модель под сказанное. Прод 29.07: на «по бизнесу» бот
    # выдавал дословное «Какого результата хотите достичь?», а на встречное
    # «в чем» — молча передавал Юлии.
    qualification = await get_ai().qualify(
        history, final=False, question_topic=texts.A_QUESTION_2
    )
    asked = texts.A_QUESTION_2
    if qualification is not None:
        asked = _adapted_or_scripted(qualification.get("next_question"), texts.A_QUESTION_2)
    await _send_bot_turn(message, contact, asked)
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
    qualification.pop("_hot_without_signal", None)  # решение здесь безусловное
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
    # Сколько содержательных ответов уже собрано: ключи цепочки B в данных FSM.
    # Считаем по данным, а не по позиции хендлера, — после рестарта позиция
    # восстанавливается из переписки, и счётчик обязан восстановиться вместе
    # с ней, иначе клиент прошёл бы четыре вопроса дважды.
    data = await state.get_data()
    answered = sum(1 for key in B_ANSWER_KEYS if data.get(key))
    await _intermediate_step(
        message, state, bot, config, contact, history, next_question, next_state, answered
    )


@router.message(Dialog.b_duration, F.text)
async def b_answer_duration(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    """Ответ о давности ситуации → вопрос 2 из ТЗ."""
    await _b_step(
        message,
        state,
        bot,
        config,
        "duration",
        "Сценарий B, ответ о давности ситуации",
        texts.QUESTION_2,
        Dialog.b_question_2,
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
    if kind in ("A_ready", "B_problem"):
        # Появился собственный запрос — переключаемся с C на рабочий сценарий
        await state.update_data(q1=message.text, scenario=kind)
        await airtable.update_contact(contact["id"], {"scenario": kind})
    await _route_scenario(
        kind, message, state, bot, config, contact, scenario.get("first_question")
    )


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

# Последняя реплика бота → состояние, в котором она была отправлена.
# MemoryStorage теряет позицию при перезапуске, а переписка в CRM его
# переживает — по ней позиция и восстанавливается.
_STATE_BY_LAST_QUESTION: tuple[tuple[str, object], ...] = (
    (texts.A_INTRO, Dialog.a_question_1),
    (texts.A_QUESTION_2, Dialog.a_question_2),
    (texts.QUESTION_DURATION, Dialog.b_duration),
    (texts.QUESTION_2, Dialog.b_question_2),
    (texts.QUESTION_3, Dialog.b_question_3),
    (texts.QUESTION_4, Dialog.b_question_4),
    (texts.QUESTION_5, Dialog.b_question_5),
)

# Вопрос бота → ключ ответа в данных FSM. Нужен, чтобы после рестарта
# карточка Юлии не потеряла секции «ЧТО УЖЕ ПРОБОВАЛ» и «ЧЕГО ХОЧЕТ».
_ANSWER_KEY_BY_QUESTION = {
    texts.QUESTION_DURATION: "duration",
    texts.QUESTION_2: "q2",
    texts.QUESTION_3: "q3",
    texts.QUESTION_4: "q4",
    texts.QUESTION_5: "q5",
    texts.A_INTRO: "a1",
    # Вопрос сценария A о желаемом результате питает ту же секцию карточки,
    # что и вопрос 4 сценария B (см. a_answer_2)
    texts.A_QUESTION_2: "q4",
}


# Позиция в цепочке B по числу ответов клиента. Нужна потому, что формулировку
# вопроса теперь подбирает модель под сказанное человеком, и сверка по тексту
# больше не работает: адаптированный вопрос ни с чем не совпадёт, а клиент
# после рестарта услышал бы всю цепочку заново.
_B_CHAIN: tuple = (
    Dialog.b_duration,
    Dialog.b_question_2,
    Dialog.b_question_3,
    Dialog.b_question_4,
    Dialog.b_question_5,
)


def _resume_b_by_position(history: list[dict]):
    """Шаг сценария B по числу ответов клиента в текущем обращении."""
    from bot.prompts.qualifier import current_cycle

    answers = sum(1 for turn in current_cycle(history) if turn.get("role") in ("client", "user"))
    if not 1 <= answers <= len(_B_CHAIN):
        return None
    return _B_CHAIN[answers - 1]


def _resume_state(history: list[dict], fields: dict):
    """Позиция в диалоге, восстановленная по переписке.

    Без этого клиент, ответивший ровно в момент перезапуска, слышал тот же
    вопрос ещё раз: состояние терялось, и разговор начинался с определения
    сценария. Воспроизводилось при любом рестарте — деплой, авторестарт
    systemd, перезагрузка сервера.
    """
    last_bot = next((t.get("text") for t in reversed(history) if t.get("role") == "bot"), None)
    if last_bot is None:
        return Dialog.waiting_first_message
    for question, resumed in _STATE_BY_LAST_QUESTION:
        if last_bot == question:
            return resumed
    # Формулировку вопроса подбирает модель — дословного совпадения может
    # не быть. В сценарии B позицию даёт число ответов клиента.
    if fields.get("scenario") == "B_problem":
        resumed = _resume_b_by_position(history)
        if resumed is not None:
            return resumed
    # Последняя реплика — не вопрос: это ответ по базе знаний (сценарий C)
    if fields.get("scenario") == "C_info":
        return Dialog.c_info
    return Dialog.waiting_first_message


def _data_from_history(history: list[dict], fields: dict | None = None) -> dict:
    """Ответы клиента по вопросам бота — восстановление данных FSM."""
    data: dict = {}
    first_client = next((t.get("text") for t in history if t.get("role") == "client"), None)
    if first_client:
        data["q1"] = first_client
    for index, turn in enumerate(history):
        if turn.get("role") != "bot":
            continue
        key = _ANSWER_KEY_BY_QUESTION.get(turn.get("text") or "")
        if key is None:
            continue
        answer = next(
            (t.get("text") for t in history[index + 1 :] if t.get("role") == "client"), None
        )
        if answer:
            data[key] = answer
    if (fields or {}).get("scenario") == "B_problem":
        # Вопросы адаптированы — по тексту их не опознать. Ответы цепочки B
        # идут подряд после первого сообщения, поэтому раскладываем позиционно
        # всё, чего не дало сопоставление по тексту. Без этого счётчик
        # обязательных ответов после рестарта обнулился бы, и человек прошёл
        # бы четыре вопроса второй раз.
        from bot.prompts.qualifier import current_cycle

        answers = [t.get("text") for t in current_cycle(history) if t.get("role") == "client"]
        for key, answer in zip(B_ANSWER_KEYS, answers[1:]):
            data.setdefault(key, answer)
    return data


async def _continue_from(
    resumed, message: Message, state: FSMContext, bot: Bot, config: Config
) -> None:
    """Передаёт сообщение хендлеру того шага, на котором диалог прервался."""
    if resumed is Dialog.a_question_1:
        await a_answer_1(message, state)
    elif resumed is Dialog.a_question_2:
        await a_answer_2(message, state, bot, config)
    elif resumed is Dialog.b_duration:
        await b_answer_duration(message, state, bot, config)
    elif resumed is Dialog.b_question_2:
        await b_answer_2(message, state, bot, config)
    elif resumed is Dialog.b_question_3:
        await b_answer_3(message, state, bot, config)
    elif resumed is Dialog.b_question_4:
        await b_answer_4(message, state, bot, config)
    elif resumed is Dialog.b_question_5:
        await b_answer_5(message, state, bot, config)
    elif resumed is Dialog.c_info:
        await c_message(message, state, bot, config)
    else:
        await first_message(message, state, bot, config)


@router.message(StateFilter(None), F.text)
async def restore_after_restart(
    message: Message, state: FSMContext, bot: Bot, config: Config
) -> None:
    """Личное сообщение без состояния FSM (рестарт бота / клиент без /start).

    MemoryStorage теряет состояния при перезапуске — клиент посреди диалога
    не должен ни получать тишину, ни слышать один и тот же вопрос дважды
    (Блок 12: «рестарт бота во время диалога»). Квалифицированные продолжают
    свободный диалог, остальные — с того шага, на котором остановились.
    Переданные Юлии сюда не дойдут (middleware pause_check).
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
    fields = contact.get("fields", {})
    if fields.get("qualification_completed"):
        await state.set_state(Dialog.open_dialog)
        await open_dialog_message(message, state, bot, config)
        return

    history = _history_from(contact)
    resumed = _resume_state(history, fields)
    await state.set_state(resumed)
    restored = _data_from_history(history, fields)
    if fields.get("scenario"):
        restored["scenario"] = fields["scenario"]
    if restored:
        await state.update_data(**restored)
    if resumed is not Dialog.waiting_first_message:
        logger.info(
            "Диалог telegram_id=%s восстановлен по переписке: продолжаю с %s",
            message.from_user.id,
            resumed.state,
        )
    await _continue_from(resumed, message, state, bot, config)


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
        raw = message.caption or f"<{message.content_type.value}>"
        await airtable.add_touch(
            int(contact["fields"].get("telegram_id") or 0),
            "question_answered",
            "Нетекстовое сообщение в диалоге",
            raw_content=raw,
        )
    await _reply_safe(message, texts.ASK_TEXT_PLEASE)
