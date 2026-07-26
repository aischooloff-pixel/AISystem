"""Мониторинг комментариев из linked discussion group (Блок 8).

Бот — администратор канала; комментарии приходят в связанную группу как
ответы на автопересланный пост (``is_automatic_forward``). Каждый
комментарий: анализ AI → запись в Comments → контакт без дублей → касание →
счётчики поста → уведомление Юлии о потенциальном клиенте.

Правило MVP (ТЗ Юлии, п. 5): НИКАКОЙ автопубликации. Каждый ответ — только
через явное утверждение, публикуется от имени бота.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Config
from bot.keyboards.admin import COMMENT_CALLBACK_PREFIX, comment_keyboard
from bot.services import airtable
from bot.services.ai import get_ai
from bot.services.notifier import notify_yulia
from bot.states import AdminFlow
from bot.utils.logger import get_app_logger

logger = get_app_logger()

router = Router(name="comments")

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━"


def _post_id_of(message: Message) -> str | None:
    """ID исходного поста канала из автопересланного сообщения."""
    origin = getattr(message, "forward_origin", None)
    origin_id = getattr(origin, "message_id", None)
    return str(origin_id) if origin_id else None


def _post_link(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    origin_id = getattr(origin, "message_id", None)
    username = getattr(chat, "username", None)
    if username and origin_id:
        return f"https://t.me/{username}/{origin_id}"
    return None


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def discussion_message(message: Message, bot: Bot, config: Config) -> None:
    """Сообщения в discussion group: посты канала и комментарии к ним."""
    if message.chat.id != config.telegram_discussion_group_id:
        return

    # Автопересланный пост канала → фиксируем в Posts
    if getattr(message, "is_automatic_forward", False):
        post_id = _post_id_of(message)
        if post_id:
            await airtable.upsert_post(
                post_id,
                {
                    "date": (message.date or datetime.now(timezone.utc)).isoformat(),
                    "text": (message.text or message.caption or "")[:5000],
                    "link": _post_link(message),
                },
            )
            logger.info("Пост канала зафиксирован: post_id=%s", post_id)
        return

    # Комментарий: ответ на автопересланный пост
    reply_to = message.reply_to_message
    if reply_to is None or not getattr(reply_to, "is_automatic_forward", False):
        return
    author = message.from_user
    if author is None or author.is_bot:
        return

    text = message.text or message.caption or ""
    if not text.strip():
        logger.info("Комментарий без текста (%s) — пропущен", message.content_type)
        return
    post_id = _post_id_of(reply_to) or "unknown"
    post_text = reply_to.text or reply_to.caption or ""

    await airtable.upsert_post(post_id, {"text": post_text[:5000], "link": _post_link(reply_to)})
    await airtable.increment_post_counter(post_id, "comments_count")

    analysis = await get_ai().analyze_comment(
        text, author.full_name, post_topic=post_text[:100] or "тема неизвестна", post_text=post_text
    )
    if analysis is None:
        # Ошибка AI: комментарий сохраняем без анализа, Юлию уведомляем
        analysis = {
            "topic": "",
            "emotion": "neutral",
            "key_problem": None,
            "interest_level": 1,
            "is_potential_client": False,
            "needs_reply": False,
            "request_detected": None,
            "suggested_reply": "",
            "should_invite_to_bot": False,
            "confidence": 0,
        }
        await notify_yulia(
            bot,
            config.telegram_admin_id,
            f"⚠️ Ошибка обработки AI: комментарий от {author.full_name} "
            f"сохранён без анализа:\n«{text[:500]}»",
        )

    comment_record = await airtable.create_comment(
        {
            "author_telegram_id": author.id,
            "author_name": author.full_name,
            "author_username": f"@{author.username}" if author.username else None,
            "text": text[:5000],
            "post_id": post_id,
            "post_link": _post_link(reply_to),
            "post_topic": analysis.get("topic") or "",
            "date": (message.date or datetime.now(timezone.utc)).isoformat(),
            "emotion": analysis.get("emotion"),
            "key_problem": analysis.get("key_problem"),
            "interest_level": analysis.get("interest_level"),
            "is_potential_client": analysis.get("is_potential_client"),
            "needs_reply": analysis.get("needs_reply"),
            "suggested_reply": analysis.get("suggested_reply"),
            "reply_status": "pending" if analysis.get("needs_reply") else "skipped",
            "processed": not analysis.get("needs_reply"),
            "ai_confidence": analysis.get("confidence"),
        }
    )

    # Контакт: дедупликация обязательна (источник первого касания не затирается)
    contact_data = {
        "name": author.full_name,
        "source": "telegram_comment",
        "source_detail": f"комментарий под постом {post_id}",
        "first_action": "comment",
    }
    if author.username:
        contact_data["username"] = f"@{author.username}"
    await airtable.upsert_contact(author.id, contact_data)
    await airtable.add_touch(
        author.id,
        "comment",
        f"Комментарий под постом: {analysis.get('topic') or post_id}",
        source="telegram_comment",
        related_post=post_id,
        raw_content=text[:1000],
    )

    if analysis.get("is_potential_client"):
        await airtable.increment_post_counter(post_id, "potential_clients_count")
        await _notify_potential_client(
            bot, config, message, author, text, post_text, analysis, comment_record
        )


async def _notify_potential_client(
    bot: Bot,
    config: Config,
    message: Message,
    author,
    text: str,
    post_text: str,
    analysis: dict,
    comment_record: dict | None,
) -> None:
    """Уведомление Юлии о потенциальном клиенте (формат из ТЗ, Блок 8)."""
    previous = await airtable.get_comments_by_author(author.id)
    history_line = f"История: {len(previous)}-й комментарий" if previous else ""
    username = f" (@{author.username})" if author.username else ""
    stamp = (message.date or datetime.now(timezone.utc)).strftime("%d.%m %H:%M")

    card = (
        "💬 ПОТЕНЦИАЛЬНЫЙ КЛИЕНТ В КОММЕНТАРИЯХ\n\n"
        f"👤 {author.full_name}{username}\n"
        f"📄 Пост: «{(post_text or 'без текста')[:100]}»\n"
        f"🕐 {stamp}\n\n"
        f"КОММЕНТАРИЙ\n«{text[:1000]}»\n\n"
        f"Эмоция: {analysis.get('emotion')} · Интерес: {analysis.get('interest_level')}/5\n"
        f"Ключевая проблема: {analysis.get('key_problem') or '—'}\n\n"
        f"{history_line}\n\n{DIVIDER}\n\n"
        f"ПРЕДЛОЖЕННЫЙ ОТВЕТ\n«{analysis.get('suggested_reply') or '—'}»"
    )
    keyboard = None
    if comment_record is not None:
        # Кнопки — всегда (формат ТЗ); msg_id нужен для публикации реплаем
        keyboard = comment_keyboard(f"{comment_record['id']}:{message.message_id}")
    await notify_yulia(bot, config.telegram_admin_id, card, reply_markup=keyboard)


# ── Кнопки Юлии: Опубликовать / Изменить / Пропустить ──


def _parse_comment_callback(data: str) -> tuple[str, str, int | None]:
    # cmt:{action}:{record_id}:{msg_id}
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    record_id = parts[2] if len(parts) > 2 else ""
    msg_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    return action, record_id, msg_id


async def _mark_notification(callback: CallbackQuery, mark: str) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\n{mark}", reply_markup=None)
    except Exception:
        logger.exception("Не удалось отредактировать уведомление о комментарии")


@router.callback_query(F.data.startswith(COMMENT_CALLBACK_PREFIX))
async def comment_action(
    callback: CallbackQuery, bot: Bot, config: Config, state: FSMContext
) -> None:
    if callback.from_user.id != config.telegram_admin_id:
        try:
            await callback.answer("Недоступно")
        except Exception:
            logger.exception("callback.answer() не прошёл")
        return
    action, record_id, msg_id = _parse_comment_callback(callback.data or "")
    try:
        await callback.answer()
    except Exception:
        logger.exception("callback.answer() не прошёл")

    if action == "skip":
        await airtable.update_comment(record_id, {"reply_status": "skipped", "processed": True})
        logger.info("Действие Юлии: комментарий %s пропущен", record_id)
        await _mark_notification(callback, "⏭ Пропущено")
        return

    if action == "edit":
        await state.set_state(AdminFlow.waiting_comment_reply)
        await state.update_data(comment_record_id=record_id, comment_msg_id=msg_id)
        await _mark_notification(
            callback, "✏️ Пришлите свой вариант ответа сообщением (или /cancel — отменить)"
        )
        return

    if action == "pub":
        record = await airtable.get_comment(record_id)
        if record is None:
            await notify_yulia(
                bot, config.telegram_admin_id, "Airtable недоступен — ответ не опубликован."
            )
            return
        reply_text = record.get("fields", {}).get("suggested_reply") or ""
        if not reply_text:
            await notify_yulia(bot, config.telegram_admin_id, "Предложенный ответ пуст.")
            return
        published = await _publish_reply(bot, config, record_id, msg_id, reply_text, "sent")
        # Отметка честная: при ошибке Telegram кнопки остаются для повтора
        if published:
            await _mark_notification(callback, "✅ Опубликовано")


@router.message(AdminFlow.waiting_comment_reply, F.text, ~F.text.startswith("/"))
async def edited_reply_from_yulia(
    message: Message, bot: Bot, config: Config, state: FSMContext
) -> None:
    """Юлия прислала свой вариант ответа → публикуем от имени бота."""
    if message.from_user is None or message.from_user.id != config.telegram_admin_id:
        return
    data = await state.get_data()
    record_id = data.get("comment_record_id")
    msg_id = data.get("comment_msg_id")
    await state.clear()
    if not record_id:
        return
    published = await _publish_reply(bot, config, record_id, msg_id, message.text, "edited")
    try:
        await message.answer("✅ Опубликовано" if published else "⚠️ Не удалось опубликовать")
    except Exception:
        logger.exception("Не удалось подтвердить публикацию")


async def _publish_reply(
    bot: Bot, config: Config, record_id: str, msg_id: int | None, text: str, status: str
) -> bool:
    """Публикация ответа от имени бота реплаем на комментарий.

    ``True`` — опубликовано и зафиксировано в CRM; ``False`` — не удалось.
    """
    try:
        await bot.send_message(
            config.telegram_discussion_group_id, text, reply_to_message_id=msg_id
        )
    except Exception:
        logger.exception("Не удалось опубликовать ответ на комментарий %s", record_id)
        await notify_yulia(
            bot, config.telegram_admin_id, "Не удалось опубликовать ответ (ошибка Telegram)."
        )
        return False
    await airtable.update_comment(
        record_id, {"reply_status": status, "final_reply": text, "processed": True}
    )
    logger.info("Ответ на комментарий %s опубликован (%s)", record_id, status)
    return True
