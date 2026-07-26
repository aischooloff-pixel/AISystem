"""Обработка таймаутов диалогов (Блок 6). Запуск по cron каждый час:

    0 * * * * cd /home/yulia/geikina-bot && .venv/bin/python -m scripts.check_timeouts

Правила из ТЗ:
- нет ответа 24 ч на этапе квалификации → ОДНО мягкое напоминание
  («Если вопрос всё ещё актуален — я здесь.») + задача Юлии;
- нет ответа 72 ч → status=cold, result=no_response, автоматика остановлена;
- warm без движения 7 дней → задача «Решить по {имя}: прогрев или закрытие».

Напоминание отправляется один раз — преследование нарушало бы принцип
«без давления» (Конституция, п. 3).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram import Bot

from bot import texts
from bot.config import load_config
from bot.services import airtable
from bot.services.airtable import init_airtable
from bot.utils.helpers import status_locked_by_yulia
from bot.utils.logger import get_app_logger, setup_logging

logger = get_app_logger()

REMINDER_NOTE = "Напоминание 24 ч"


def _tid(record: dict) -> int:
    return int(record.get("fields", {}).get("telegram_id") or 0)


def _name(record: dict) -> str:
    return record.get("fields", {}).get("name") or str(_tid(record))


def _parse_moment(raw: str | None) -> datetime | None:
    """ISO-строка Airtable → aware-datetime (даты без времени и «Z» — тоже)."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


async def _reminder_already_sent(telegram_id: int, last_contact: str) -> bool:
    """Напоминание после последнего сообщения клиента уже уходило?

    Даты сравниваются как datetime: Airtable может отдавать «Z», смещение
    или дату без времени — лексикографическое сравнение строк здесь врёт.
    """
    touches = await airtable.get_touches(telegram_id)
    if touches is None:
        return True  # Airtable сбоит — лучше не слать, чем задвоить
    last_moment = _parse_moment(last_contact)
    for touch in touches:
        fields = touch.get("fields", {})
        if fields.get("type") == "nurturing_touch" and REMINDER_NOTE in (
            fields.get("description") or ""
        ):
            touch_moment = _parse_moment(fields.get("date"))
            if touch_moment is None or last_moment is None:
                return True  # непарсибельные даты — не рискуем дублем
            if touch_moment >= last_moment:
                return True
    return False


async def _task_exists(action: str) -> bool:
    tasks = await airtable.get_open_tasks("yulia")
    if tasks is None:
        return True  # не плодим задачи вслепую при сбое
    return any(t.get("fields", {}).get("action") == action for t in tasks)


async def process_reminders(bot: Bot, reminder_hours: int, cold_hours: int) -> int:
    """Молчание ≥ 24 ч (но < 72 ч) на этапе квалификации → одно напоминание."""
    stale = await airtable.get_stale_contacts(reminder_hours)
    if stale is None:
        logger.error("check_timeouts: Airtable недоступен, напоминания пропущены")
        return 0
    # Молчащие ≥ 72 ч — зона process_cold: им напоминание уже не шлём
    beyond_cold = await airtable.get_stale_contacts(cold_hours)
    cold_ids = {record["id"] for record in beyond_cold} if beyond_cold is not None else set()
    sent = 0
    for record in stale:
        if record["id"] in cold_ids:
            continue
        fields = record.get("fields", {})
        # Этап квалификации: она ещё не завершена, статусы финальных маршрутов не трогаем
        if fields.get("qualification_completed") or fields.get("result"):
            continue
        if fields.get("status") in ("non_target", "client", "in_progress"):
            continue
        telegram_id = _tid(record)
        if await _reminder_already_sent(telegram_id, fields.get("last_contact_date", "")):
            continue
        try:
            await bot.send_message(telegram_id, texts.REMINDER_24H)
        except Exception:
            logger.exception("Напоминание не доставлено telegram_id=%s", telegram_id)
            continue
        await airtable.add_touch(
            telegram_id, "nurturing_touch", f"{REMINDER_NOTE}: «{texts.REMINDER_24H}»"
        )
        action = f"Проверить диалог с {_name(record)}"
        if not await _task_exists(action):
            await airtable.create_task(
                action,
                "yulia",
                fields.get("last_contact_date", "")[:10] or "",
                f"Нет ответа {reminder_hours} ч на этапе квалификации",
                contact_telegram_id=telegram_id,
            )
        sent += 1
        logger.info("Напоминание отправлено telegram_id=%s", telegram_id)
    return sent


async def process_cold(cold_hours: int) -> int:
    """Молчание ≥ 72 ч → cold, result=no_response, автоматика остановлена."""
    stale = await airtable.get_stale_contacts(cold_hours)
    if stale is None:
        return 0
    changed = 0
    for record in stale:
        # Одна битая запись не должна блокировать обработку остальных
        try:
            fields = record.get("fields", {})
            if fields.get("result") == "no_response":
                continue
            if fields.get("status") in ("non_target", "client", "in_progress"):
                continue
            if fields.get("qualification_completed"):
                continue
            if status_locked_by_yulia(fields):
                continue  # решение Юлии автоматика не переопределяет
            old_status = fields.get("status", "cold")
            if old_status != "cold":
                await airtable.add_status_change(
                    record["id"], old_status, "cold", f"Нет ответа {cold_hours} ч", "ai"
                )
            await airtable.update_contact(record["id"], {"result": "no_response", "paused": True})
            changed += 1
            logger.info("Контакт telegram_id=%s → cold/no_response", _tid(record))
        except Exception:
            logger.exception("process_cold: ошибка на записи %s", record.get("id"))
    return changed


async def process_stale_warm(days: int) -> int:
    """warm без движения N дней → задача «Решить по {имя}: прогрев или закрытие»."""
    warm = await airtable.get_contacts_by_status("warm")
    if warm is None:
        return 0
    stale = await airtable.get_stale_contacts(days * 24)
    if stale is None:
        return 0
    stale_set = {record["id"] for record in stale}
    created = 0
    for record in warm:
        fields = record.get("fields", {})
        if fields.get("assigned_to") != "ai" or fields.get("paused"):
            continue
        if record["id"] not in stale_set:
            continue
        action = f"Решить по {_name(record)}: прогрев или закрытие"
        if await _task_exists(action):
            continue
        await airtable.create_task(
            action,
            "yulia",
            fields.get("last_contact_date", "")[:10] or "",
            f"Тёплый клиент без движения {days} дней",
            contact_telegram_id=_tid(record),
        )
        created += 1
    return created


async def main() -> None:
    config = load_config()
    setup_logging(config.log_dir, config.log_level)
    init_airtable(config)
    bot = Bot(token=config.telegram_bot_token)
    try:
        reminders = await process_reminders(
            bot, config.timeout_reminder_hours, config.timeout_cold_hours
        )
        cold = await process_cold(config.timeout_cold_hours)
        warm_tasks = await process_stale_warm(config.nurturing_review_days)
        logger.info(
            "check_timeouts: напоминаний %d, переведено в cold %d, задач по warm %d",
            reminders,
            cold,
            warm_tasks,
        )
    finally:
        await bot.session.close()
        await airtable.get_client().close()


if __name__ == "__main__":
    asyncio.run(main())
