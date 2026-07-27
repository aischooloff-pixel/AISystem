"""Admin-команды Юлии (Блок 7). Только для TELEGRAM_ADMIN_ID —
проверка на каждом хендлере (ТЗ, Часть 7). Все действия логируются.
"""

from __future__ import annotations

import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Config
from bot.handlers.questionnaire import send_questionnaire
from bot.services import airtable
from bot.services.ai import get_ai
from bot.services.knowledge import get_knowledge_stats, reload_knowledge
from bot.services.notifier import build_client_card
from bot.utils.helpers import MSK, STATUS_RU, fmt_moment
from bot.utils.logger import get_app_logger

logger = get_app_logger()

router = Router(name="admin")


class _AdminOrIdle(BaseFilter):
    """Пропускает в admin-роутер Юлию всегда, остальных — только вне диалога.

    Иначе клиент посреди квалификации, написавший «/stop», получил бы
    «Команда недоступна» вместо обработки своего ответа FSM-хендлером.
    Пользователь без состояния всё же получает явный отказ (Блок 12).
    """

    async def __call__(self, message: Message, config: Config, state: FSMContext) -> bool:
        if message.from_user is not None and message.from_user.id == config.telegram_admin_id:
            return True
        return await state.get_state() is None


router.message.filter(_AdminOrIdle())

START_TIME = time.monotonic()

VALID_STATUSES = ("non_target", "cold", "warm", "hot", "in_progress", "client")

HELP = (
    "/info {id} — карточка клиента\n"
    "/timeline {id} — история касаний\n"
    "/status {id} {статус} — сменить статус\n"
    "/pause {id} · /resume {id} — пауза / снять\n"
    "/assign_me {id} · /assign_ai {id} — взять / вернуть AI\n"
    "/stop {id} — завершить взаимодействие\n"
    "/note {id} {текст} — заметка\n"
    "/anketa {id} — отправить анкету «Точка сбоя» (/anketa_force — повторно)\n"
    "/stats — статистика · /tasks — задачи · /hot — горячие\n"
    "/reload_knowledge — перечитать базу знаний\n/cancel — отменить правку ответа на комментарий\n"
    "/health — проверка систем"
)


def _is_admin(message: Message, config: Config) -> bool:
    is_admin = message.from_user is not None and message.from_user.id == config.telegram_admin_id
    if not is_admin:
        uid = message.from_user.id if message.from_user else "unknown"
        logger.warning("Отказ в admin-команде: telegram_id=%s, текст=%r", uid, message.text)
    return is_admin


async def _reply(message: Message, text: str) -> None:
    try:
        await message.answer(text[:4000])
    except Exception:
        logger.exception("Не удалось ответить на admin-команду")


def _args(message: Message) -> list[str]:
    return (message.text or "").split()[1:]


async def _contact_or_report(message: Message, args: list[str]) -> dict | None:
    if not args or not args[0].lstrip("-").isdigit():
        await _reply(message, "Укажите telegram_id клиента: /команда 123456789")
        return None
    ok, contact = await airtable.find_contact_checked(int(args[0]))
    if not ok:
        await _reply(message, "Airtable недоступен, попробуйте позже.")
        return None
    if contact is None:
        await _reply(message, f"Контакт {args[0]} не найден.")
        return None
    return contact


def _log_action(message: Message, action: str) -> None:
    logger.info("Действие Юлии: %s (%r)", action, message.text)


@router.message(F.text == "/admin")
async def cmd_help(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    await _reply(message, HELP)


@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, config: Config, state: FSMContext) -> None:
    """Выход из режима правки ответа на комментарий (Блок 8)."""
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    await state.clear()
    _log_action(message, "cancel")
    await _reply(message, "Ок, отменил. Режим правки закрыт.")


@router.message(F.text.startswith("/info"))
async def cmd_info(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    contact = await _contact_or_report(message, _args(message))
    if contact is None:
        return
    fields = contact.get("fields", {})
    qualification = {
        "summary": fields.get("request_summary"),
        "key_phrases": (
            (fields.get("key_phrases") or "").split("\n") if fields.get("key_phrases") else []
        ),
        "status": fields.get("status"),
        "status_reason": fields.get("status_reason"),
        "awareness": fields.get("awareness"),
        "readiness": fields.get("readiness"),
        "urgency": fields.get("urgency"),
        "confidence": fields.get("ai_confidence") or 0,
        "next_action": fields.get("next_step"),
    }
    timeline = await airtable.build_timeline(int(fields.get("telegram_id") or 0))
    card = build_client_card(
        fields, qualification, timeline, confidence_threshold=0  # без плашки «оценка»
    )
    _log_action(message, "info")
    await _reply(message, card)


@router.message(F.text.startswith("/timeline"))
async def cmd_timeline(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    contact = await _contact_or_report(message, _args(message))
    if contact is None:
        return
    tid = int(contact["fields"].get("telegram_id") or 0)
    _log_action(message, "timeline")
    await _reply(message, await airtable.build_timeline(tid))


@router.message(F.text.startswith("/status"))
async def cmd_status(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    args = _args(message)
    if len(args) < 2 or args[1] not in VALID_STATUSES:
        await _reply(message, f"Формат: /status {{id}} {{{'|'.join(VALID_STATUSES)}}}")
        return
    contact = await _contact_or_report(message, args)
    if contact is None:
        return
    old = contact["fields"].get("status", "—")
    await airtable.add_status_change(
        contact["id"], old, args[1], "Смена статуса вручную (/status)", "yulia"
    )
    _log_action(message, f"status {old} → {args[1]}")
    await _reply(message, f"Статус изменён: {old} → {args[1]}")


async def _simple_update(
    message: Message, config: Config, updates: dict, done_text: str, action: str
) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    contact = await _contact_or_report(message, _args(message))
    if contact is None:
        return
    await airtable.update_contact(contact["id"], updates)
    _log_action(message, action)
    await _reply(message, done_text)


@router.message(F.text.startswith("/anketa_force"))
async def cmd_anketa_force(message: Message, config: Config, state: FSMContext, bot: Bot) -> None:
    """Повторная отправка анкеты, даже если она уже заполнена.

    Зарегистрирована ДО ``/anketa``: фильтр startswith иначе перехватил бы
    ``/anketa_force`` первым же обработчиком.
    """
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    args = _args(message)
    contact = await _contact_or_report(message, args)
    if contact is None:
        return
    telegram_id = int(args[0])
    name = contact.get("fields", {}).get("name") or "клиент"
    _log_action(message, "anketa_force")
    if await send_questionnaire(bot, state, telegram_id):
        await _reply(message, f"Анкета отправлена повторно: {name}.")
    else:
        await _reply(message, f"Не удалось отправить анкету {name}.")


@router.message(F.text.startswith("/anketa"))
async def cmd_anketa(message: Message, config: Config, state: FSMContext, bot: Bot) -> None:
    """Отправляет клиенту анкету «Точка сбоя» (Блок 11).

    Рассылает только Юлия: решение «пора заполнять анкету» экспертное
    (Конституция, принцип 5), и клиент к этому моменту уже передан ей.
    """
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    args = _args(message)
    contact = await _contact_or_report(message, args)
    if contact is None:
        return
    telegram_id = int(args[0])
    name = contact.get("fields", {}).get("name") or "клиент"

    existing = await airtable.get_diagnostics(telegram_id)
    if existing:
        # Анкету не отправляем молча повторно: прежние ответы важнее
        await _reply(
            message,
            f"У {name} уже есть заполненная анкета ({len(existing)} шт.). "
            "Отправить ещё раз: /anketa_force {id}",
        )
        return

    _log_action(message, "anketa")
    if await send_questionnaire(bot, state, telegram_id):
        await _reply(message, f"Анкета отправлена: {name}.")
    else:
        await _reply(
            message,
            f"Не удалось отправить анкету {name} — возможно, клиент "
            "не начинал диалог с ботом или заблокировал его.",
        )


@router.message(F.text.startswith("/pause"))
async def cmd_pause(message: Message, config: Config) -> None:
    await _simple_update(message, config, {"paused": True}, "⏸ Автоматика остановлена.", "pause")


@router.message(F.text.startswith("/resume"))
async def cmd_resume(message: Message, config: Config) -> None:
    await _simple_update(message, config, {"paused": False}, "▶️ Автоматика включена.", "resume")


@router.message(F.text.startswith("/assign_me"))
async def cmd_assign_me(message: Message, config: Config) -> None:
    await _simple_update(
        message,
        config,
        {"assigned_to": "yulia", "paused": True},
        "Клиент на вас. Автоматика остановлена.",
        "assign_me",
    )


@router.message(F.text.startswith("/assign_ai"))
async def cmd_assign_ai(message: Message, config: Config) -> None:
    await _simple_update(
        message,
        config,
        {"assigned_to": "ai", "paused": False},
        "Клиент возвращён AI.",
        "assign_ai",
    )


@router.message(F.text.startswith("/stop"))
async def cmd_stop(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    contact = await _contact_or_report(message, _args(message))
    if contact is None:
        return
    await airtable.update_contact(
        contact["id"], {"paused": True, "result": "declined", "assigned_to": "yulia"}
    )
    tid = int(contact["fields"].get("telegram_id") or 0)
    await airtable.add_touch(tid, "status_change", "Взаимодействие завершено Юлией (/stop)")
    _log_action(message, "stop")
    await _reply(message, "Взаимодействие завершено, автоматика остановлена.")


@router.message(F.text.startswith("/note"))
async def cmd_note(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await _reply(message, "Формат: /note {id} {текст заметки}")
        return
    contact = await _contact_or_report(message, [args[1]])
    if contact is None:
        return
    stamp = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    old_notes = contact["fields"].get("notes") or ""
    new_notes = f"{old_notes}\n[{stamp}] {args[2]}".strip()
    await airtable.update_contact(contact["id"], {"notes": new_notes})
    _log_action(message, "note")
    await _reply(message, "Заметка добавлена.")


@router.message(F.text.startswith("/stats"))
async def cmd_stats(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    counts: dict[str, int] = {}
    for status in VALID_STATUSES:
        records = await airtable.get_contacts_by_status(status)
        if records is None:
            await _reply(message, "Airtable недоступен.")
            return
        counts[status] = len(records)
    total = sum(counts.values())
    lines = [f"Всего контактов: {total}"] + [
        f"· {STATUS_RU.get(s, s)}: {n}" for s, n in counts.items() if n
    ]
    _log_action(message, "stats")
    await _reply(message, "\n".join(lines))


@router.message(F.text.startswith("/tasks"))
async def cmd_tasks(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    tasks = await airtable.get_open_tasks()
    if tasks is None:
        await _reply(message, "Airtable недоступен.")
        return
    if not tasks:
        await _reply(message, "Открытых задач нет.")
        return
    lines = ["Открытые задачи:"]
    for task in tasks[:30]:
        fields = task.get("fields", {})
        lines.append(f"· {fields.get('action', '—')} (до {fields.get('due_date', '—')})")
    _log_action(message, "tasks")
    await _reply(message, "\n".join(lines))


@router.message(F.text.startswith("/hot"))
async def cmd_hot(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    records = await airtable.get_contacts_by_status("hot")
    if records is None:
        await _reply(message, "Airtable недоступен.")
        return
    if not records:
        await _reply(message, "Горячих лидов нет.")
        return
    lines = ["🔥 Горячие лиды:"]
    for record in records[:30]:
        fields = record.get("fields", {})
        lines.append(
            f"· {fields.get('name', '—')} (id {fields.get('telegram_id', '—')}) — "
            f"{fmt_moment(fields.get('last_contact_date'))}"
        )
    _log_action(message, "hot")
    await _reply(message, "\n".join(lines))


@router.message(F.text.startswith("/reload_knowledge"))
async def cmd_reload_knowledge(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    reload_knowledge(config.knowledge_dir)
    stats = get_knowledge_stats(config.knowledge_dir)
    _log_action(message, "reload_knowledge")
    await _reply(
        message,
        f"База знаний перечитана: {stats['files']} файлов, "
        f"~{stats['estimated_tokens']} токенов.",
    )


@router.message(F.text.startswith("/health"))
async def cmd_health(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        await _reply(message, "Команда недоступна.")
        return
    airtable_ms = await airtable.get_client().ping()
    openai_ms = await get_ai().ping()
    stats = get_knowledge_stats(config.knowledge_dir)

    log_line = "✅ пишутся"
    try:
        app_log = config.log_dir / "app.log"
        age = int(time.time() - app_log.stat().st_mtime)
        log_line = f"✅ пишутся, последняя запись {age // 60} мин назад"
        errors = sum(
            1 for line in app_log.read_text(encoding="utf-8").splitlines() if "[ERROR" in line
        )
    except OSError:
        log_line = "❌ файл лога недоступен"
        errors = "—"

    uptime = int(time.monotonic() - START_TIME)
    days, rest = divmod(uptime, 86400)
    hours = rest // 3600

    def status(ms: float | None) -> str:
        return f"✅ доступен ({ms:.0f} мс)" if ms is not None else "❌ недоступен"

    _log_action(message, "health")
    await _reply(
        message,
        f"Airtable:      {status(airtable_ms)}\n"
        f"OpenAI:        {status(openai_ms)}\n"
        f"База знаний:   ✅ {stats['files']} файлов, ~{stats['estimated_tokens']} токенов\n"
        f"Логи:          {log_line}\n"
        f"Uptime:        ✅ {days} дн. {hours} ч.\n"
        f"Ошибок в app.log: {errors}",
    )
