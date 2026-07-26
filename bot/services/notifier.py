"""Карточки клиентов и уведомления Юлии (Блоки 6–7).

Карточка отвечает на восемь вопросов из «критериев качественной квалификации»
(«Логика принятия решений», раздел 15): кто · откуда · какая проблема ·
что предпринималось · чего хочет · насколько готов · нужно ли личное
участие · какой следующий шаг. Юлия должна понять всё за две минуты.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot

from bot.keyboards.admin import client_card_keyboard
from bot.services import airtable
from bot.utils.helpers import MSK, fmt_moment, level_ru, status_locked_by_yulia
from bot.utils.logger import get_app_logger

logger = get_app_logger()

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━"

STATUS_HEADERS = {
    "hot": "🔥 ГОРЯЧИЙ ЛИД",
    "warm": "🟡 ТЁПЛЫЙ ЛИД",
    "cold": "🔵 ХОЛОДНЫЙ ЛИД",
    "non_target": "⚪ НЕЦЕЛЕВОЕ ОБРАЩЕНИЕ",
}


def build_client_card(
    fields: dict,
    qualification: dict,
    timeline: str,
    *,
    confidence_threshold: int = 85,
    error_mark: str | None = None,
) -> str:
    """Текст карточки клиента для Юлии (формат из ТЗ, Блок 7)."""
    confidence = int(qualification.get("confidence") or 0)
    status = qualification.get("status", fields.get("status", "warm"))

    if error_mark:
        header = f"⚠️ {error_mark}"
    elif confidence < confidence_threshold:
        header = (
            f"⚠️ ТРЕБУЕТСЯ ЭКСПЕРТНАЯ ОЦЕНКА · уверенность AI {confidence}%\n"
            "Причина: AI не смог однозначно определить статус"
        )
    else:
        header = f"{STATUS_HEADERS.get(status, status)} · уверенность AI {confidence}%"

    name = fields.get("name") or "Без имени"
    username = fields.get("username")
    who = f"👤 {name} ({username})" if username else f"👤 {name}"
    source = fields.get("source", "—")
    source_detail = fields.get("source_detail")
    source_line = f"📍 Источник: {source}" + (f" → {source_detail}" if source_detail else "")
    first_touch = fmt_moment(fields.get("first_touch_date"), "%d.%m.%Y")
    touches_count = fields.get("touches_count", "—")

    quotes = "\n".join(f"· «{q}»" for q in qualification.get("key_phrases") or []) or "—"
    answers = qualification.get("answers") or {}

    parts = [
        header,
        "",
        who,
        source_line,
        f"🔗 Первое касание: {first_touch} · всего касаний: {touches_count}",
        "",
        DIVIDER,
        "",
        "ЗАПРОС",
        qualification.get("summary") or "—",
    ]
    # Карточка обязана отвечать на все 8 вопросов (раздел 15) — секции
    # присутствуют всегда, даже если ответ в диалоге не прозвучал
    parts += ["", "ЧТО УЖЕ ПРОБОВАЛ(А)", answers.get("tried") or "— (не прозвучало в диалоге)"]
    parts += ["", "ЧЕГО ХОЧЕТ", answers.get("goal") or "— (не прозвучало в диалоге)"]
    parts += [
        "",
        "КЛЮЧЕВЫЕ ЦИТАТЫ",
        quotes,
        "",
        DIVIDER,
        "",
        f"Осознанность: {level_ru(qualification.get('awareness'))}",
        f"Готовность: {level_ru(qualification.get('readiness'))}",
        f"Срочность: {level_ru(qualification.get('urgency'))}",
        "",
        "ОСНОВАНИЕ СТАТУСА",
        qualification.get("status_reason") or "—",
        "",
        DIVIDER,
        "",
        "ИСТОРИЯ КАСАНИЙ",
        timeline,
        "",
        DIVIDER,
        "",
        "РЕКОМЕНДАЦИЯ",
        qualification.get("next_action") or "—",
    ]
    if qualification.get("needs_yulia_reason"):
        parts += ["", f"Причина передачи: {qualification['needs_yulia_reason']}"]
    return "\n".join(parts)


async def notify_yulia(bot: Bot, admin_id: int, text: str, reply_markup=None) -> bool:
    """Сообщение Юлии. Ошибка отправки логируется, бот не падает."""
    try:
        await bot.send_message(admin_id, text, reply_markup=reply_markup)
        logger.info("Уведомление Юлии отправлено (%d символов)", len(text))
        return True
    except Exception:
        logger.exception("Не удалось отправить уведомление Юлии")
        return False


async def handoff_to_yulia(
    bot: Bot,
    admin_id: int,
    contact: dict,
    qualification: dict,
    *,
    confidence_threshold: int = 85,
    error_mark: str | None = None,
) -> None:
    """Передача клиента Юлии (ТЗ, Блок 6 «hot» и Блок 4 «автопередача»).

    CRM: статус (если не заблокирован решением Юлии) + assigned_to=yulia,
    paused=true, handoff_date, данные квалификации; Task «Связаться с {имя}»
    со сроком сегодня; касание handoff; карточка Юлии с кнопками.
    Сообщение клиенту отправляет вызывающий хендлер.
    """
    fields = contact.get("fields", {})
    record_id = contact["id"]
    telegram_id = int(fields.get("telegram_id") or 0)
    name = fields.get("name") or "клиент"

    new_status = qualification.get("status") or "hot"
    old_status = fields.get("status", "cold")
    if new_status != old_status:
        if status_locked_by_yulia(fields):
            # Приоритет решения Юлии («Карта пути», п. 19): статус не трогаем,
            # но передача происходит — это событие клиента, а не оценка AI
            logger.info(
                "Статус %s заблокирован решением Юлии — оставляю %s", telegram_id, old_status
            )
            qualification = {**qualification, "status": old_status}
        else:
            await airtable.add_status_change(
                record_id,
                old_status,
                new_status,
                qualification.get("status_reason") or "передача Юлии",
                "ai",
            )

    updates = {
        "assigned_to": "yulia",
        "paused": True,
        "handoff_date": datetime.now(MSK).isoformat(timespec="seconds"),
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
    product = qualification.get("product_interest")
    if product:
        updates["product_interest"] = product
    await airtable.update_contact(record_id, updates)

    await airtable.create_task(
        f"Связаться с {name}",
        "yulia",
        datetime.now(MSK).date().isoformat(),
        qualification.get("needs_yulia_reason") or "Клиент передан Юлии",
        contact_telegram_id=telegram_id,
    )
    await airtable.add_touch(
        telegram_id,
        "handoff",
        f"Передан Юлии: {qualification.get('status_reason') or 'квалификация завершена'}",
    )

    timeline = await airtable.build_timeline(telegram_id)
    card = build_client_card(
        fields,
        qualification,
        timeline,
        confidence_threshold=confidence_threshold,
        error_mark=error_mark,
    )
    await notify_yulia(bot, admin_id, card, reply_markup=client_card_keyboard(telegram_id))
