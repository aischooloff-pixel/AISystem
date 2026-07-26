"""Inline-кнопки карточки клиента для Юлии (Блок 7).

| Кнопка       | Действия                                                        |
|--------------|-----------------------------------------------------------------|
| ✅ Беру      | assigned_to=yulia, paused=true, handoff_date · Task · клиенту   |
| 🔄 Прогрев   | status=warm, paused=false, assigned_to=ai · история by=yulia    |
| ⛔ Нецелевой | status=non_target, paused=true, result=declined · завершение    |
| ⏸ Пауза     | paused=true · клиенту ничего не отправляется                    |

После нажатия кнопки убираются, к карточке добавляется отметка с датой.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot import texts
from bot.config import Config
from bot.keyboards.admin import ADMIN_CALLBACK_PREFIX
from bot.services import airtable
from bot.utils.helpers import MSK
from bot.utils.logger import get_app_logger

logger = get_app_logger()

router = Router(name="admin_callbacks")

ACTION_MARKS = {
    "take": "✅ Взято в работу",
    "nurture": "🔄 Прогрев",
    "reject": "⛔ Нецелевой",
    "pause": "⏸ Пауза",
}


async def _answer_safe(callback: CallbackQuery, text: str | None = None) -> None:
    try:
        await callback.answer(text)
    except Exception:
        logger.exception("callback.answer() не прошёл")


async def _mark_card(callback: CallbackQuery, mark: str) -> None:
    """Убирает кнопки и дописывает отметку «✅ Взято в работу · 25.07 14:32»."""
    message = callback.message
    if message is None:
        return
    stamp = datetime.now(MSK).strftime("%d.%m %H:%M")
    try:
        new_text = f"{message.text}\n\n{mark} · {stamp}"
        await message.edit_text(new_text, reply_markup=None)
    except Exception:
        logger.exception("Не удалось отредактировать карточку")


async def _send_client(bot: Bot, telegram_id: int, text: str) -> None:
    try:
        await bot.send_message(telegram_id, text)
    except Exception:
        logger.exception("Сообщение клиенту %s не доставлено", telegram_id)


@router.callback_query(F.data.startswith(ADMIN_CALLBACK_PREFIX))
async def admin_card_action(callback: CallbackQuery, bot: Bot, config: Config) -> None:
    """Обработка кнопок карточки. Только для Юлии (TELEGRAM_ADMIN_ID)."""
    if callback.from_user.id != config.telegram_admin_id:
        # Проверка на каждом хендлере (ТЗ, Часть 7)
        logger.warning("Чужое нажатие админ-кнопки: telegram_id=%s", callback.from_user.id)
        await _answer_safe(callback, "Недоступно")
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ACTION_MARKS:
        await _answer_safe(callback, "Неизвестное действие")
        return
    action, telegram_id = parts[1], int(parts[2])

    ok, contact = await airtable.find_contact_checked(telegram_id)
    if not ok or contact is None:
        await _answer_safe(callback, "Airtable недоступен или контакт не найден")
        return
    fields = contact.get("fields", {})
    record_id = contact["id"]
    name = fields.get("name") or str(telegram_id)
    old_status = fields.get("status", "cold")

    if action == "take":
        await airtable.update_contact(
            record_id,
            {
                "assigned_to": "yulia",
                "paused": True,
                "handoff_date": datetime.now(MSK).isoformat(timespec="seconds"),
            },
        )
        await airtable.create_task(
            f"Связаться с {name}",
            "yulia",
            datetime.now(MSK).date().isoformat(),
            "Юлия взяла клиента в работу (кнопка «Беру»)",
            contact_telegram_id=telegram_id,
            created_by="yulia",
        )
        await airtable.add_touch(telegram_id, "handoff", "Юлия взяла клиента в работу")
        await _send_client(bot, telegram_id, texts.YULIA_WILL_CONTACT)
    elif action == "nurture":
        await airtable.add_status_change(
            record_id, old_status, "warm", "Решение Юлии: прогрев", "yulia"
        )
        await airtable.update_contact(record_id, {"paused": False, "assigned_to": "ai"})
    elif action == "reject":
        await airtable.add_status_change(
            record_id, old_status, "non_target", "Решение Юлии: нецелевой", "yulia"
        )
        await airtable.update_contact(record_id, {"paused": True, "result": "declined"})
        await _send_client(bot, telegram_id, texts.NON_TARGET_CLOSING)
    elif action == "pause":
        # Клиенту ничего не отправляется (ТЗ, Блок 7)
        await airtable.update_contact(record_id, {"paused": True})

    logger.info("Действие Юлии по карточке: %s для telegram_id=%s", action, telegram_id)
    await _mark_card(callback, ACTION_MARKS[action])
    await _answer_safe(callback)
