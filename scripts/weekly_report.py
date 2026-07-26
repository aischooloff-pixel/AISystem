"""Еженедельный отчёт Юлии (Блок 9). Cron: понедельник 10:00 МСК:

    0 7 * * 1 cd /home/yulia/geikina-bot && .venv/bin/python -m scripts.weekly_report
    (07:00 UTC = 10:00 МСК)

Структура — «Карта клиентского пути», раздел 17: Продажи, Источники,
Контент, AI, Клиенты, Конверсии, Точки отвала, Требует внимания.

Важно (ТЗ): если данных за период нет — так и написать; метрики, которых
система не собирает, не выдумываются.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from aiogram import Bot

from bot.config import load_config
from bot.services import airtable
from bot.services.airtable import init_airtable
from bot.utils.logger import get_app_logger, setup_logging

logger = get_app_logger()

MONTHS_RU = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def last_full_week(today: date) -> tuple[date, date]:
    """Прошлая полная неделя: понедельник–воскресенье до текущей даты."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def _fields(records: list[dict]) -> list[dict]:
    return [r.get("fields", {}) for r in records]


def _pct(part: int, total: int) -> str:
    return f"{round(part * 100 / total)}%" if total else "—"


def build_report(data: dict) -> str:
    """Текст отчёта из сырых данных get_report_data. Чистая функция (тесты)."""
    date_from = date.fromisoformat(data["date_from"])
    date_to = date.fromisoformat(data["date_to"])
    if date_from.month == date_to.month:
        period = f"{date_from.day}–{date_to.day} {MONTHS_RU[date_to.month]}"
    else:  # неделя на стыке месяцев: «27 июля – 2 августа»
        period = (
            f"{date_from.day} {MONTHS_RU[date_from.month]} – "
            f"{date_to.day} {MONTHS_RU[date_to.month]}"
        )

    new_contacts = _fields(data["new_contacts"])
    handoffs = _fields(data["handoffs"])
    comments = _fields(data["comments"])
    touches = _fields(data["touches"])
    all_contacts = _fields(data["all_contacts"])
    open_tasks = data["open_tasks"]
    pending_comments = data["pending_comments"]

    if not any((new_contacts, handoffs, comments, touches)):
        return (
            f"📊 ОТЧЁТ ЗА НЕДЕЛЮ · {period}\n\n"
            "Данных за период нет: не было ни новых обращений, ни комментариев, "
            "ни диалогов. Отчёт сформирован, метрики не выдуманы."
        )

    # ── Продажи ──
    dialog_ids = {
        t.get("contact_telegram_id")
        for t in touches
        if t.get("type") in ("question_answered", "dm_start")
    }
    hot_now = sum(1 for c in all_contacts if c.get("status") == "hot")
    entered = sum(1 for c in handoffs if c.get("result") == "entered")

    # ── Источники (по новым обращениям) ──
    sources: dict[str, int] = {}
    for c in new_contacts:
        source = c.get("source") or "не указан"
        sources[source] = sources.get(source, 0) + 1
    source_lines = [
        f"{name}: {count} ({_pct(count, len(new_contacts))})"
        for name, count in sorted(sources.items(), key=lambda kv: -kv[1])
    ] or ["данных нет"]

    # ── Контент ──
    potential = [c for c in comments if c.get("is_potential_client")]
    best_line = "данных нет"
    if potential:
        by_post: dict[str, int] = {}
        topics: dict[str, str] = {}
        for c in potential:
            post = str(c.get("post_id") or "—")
            by_post[post] = by_post.get(post, 0) + 1
            if c.get("post_topic"):
                topics[post] = c["post_topic"]
        best_post, best_count = max(by_post.items(), key=lambda kv: kv[1])
        best_line = (
            f"«{topics.get(best_post, f'пост {best_post}')}» — "
            f"{best_count} потенциальных клиента(ов)"
        )

    # ── AI ──
    # confidence=0 («Ошибка обработки AI») — самый тревожный случай:
    # он должен входить в среднее и в счётчик передач по низкой уверенности
    confidences = [
        c["ai_confidence"] for c in new_contacts + handoffs if c.get("ai_confidence") is not None
    ]
    avg_confidence = f"{round(sum(confidences) / len(confidences))}%" if confidences else "—"
    low_confidence_handoffs = sum(
        1 for c in handoffs if c.get("ai_confidence") is not None and c["ai_confidence"] < 85
    )

    # ── Клиенты (текущая база) ──
    def count(status: str) -> int:
        return sum(1 for c in all_contacts if c.get("status") == status)

    # ── Точки отвала: только то, что реально фиксируется ──
    no_response = sum(1 for c in new_contacts if c.get("result") == "no_response")

    lines = [
        f"📊 ОТЧЁТ ЗА НЕДЕЛЮ · {period}",
        "",
        "━━ ПРОДАЖИ ━━",
        f"Новых обращений: {len(new_contacts)}",
        f"Диалогов за неделю: {len(dialog_ids)}",
        f"Горячих клиентов сейчас: {hot_now}",
        f"Передано вам: {len(handoffs)}",
        f"Вошли в работу: {entered}",
        "",
        "━━ ИСТОЧНИКИ ━━",
        *source_lines,
        "",
        "━━ КОНТЕНТ ━━",
        f"Обработано комментариев: {len(comments)}",
        f"Выявлено потенциальных: {len(potential)}",
        f"Лучший пост: {best_line}",
        "",
        "━━ AI ━━",
        f"Средняя уверенность: {avg_confidence}",
        f"Передач по низкой уверенности: {low_confidence_handoffs}",
        "",
        "━━ КЛИЕНТЫ ━━",
        f"🔥 Горячих: {count('hot')} · 🟡 Тёплых: {count('warm')} · "
        f"🔵 Холодных: {count('cold')} · ⚪ Нецелевых: {count('non_target')}",
        "",
        "━━ КОНВЕРСИИ ━━",
        f"Комментарий → потенциальный клиент: {_pct(len(potential), len(comments))}",
        f"Новое обращение → передача вам: {_pct(len(handoffs), len(new_contacts))}",
        f"Передача → вход в работу: {_pct(entered, len(handoffs))}",
        "",
        "━━ ТОЧКИ ОТВАЛА ━━",
        f"Не ответили и ушли в cold: {no_response}",
        "",
        "━━ ТРЕБУЕТ ВНИМАНИЯ ━━",
        f"Открытых задач: {len(open_tasks)}",
        f"Комментариев на утверждение: {len(pending_comments)}",
    ]
    return "\n".join(lines)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_dir, config.log_level)
    init_airtable(config)
    date_from, date_to = last_full_week(date.today())

    data = await airtable.get_report_data(date_from.isoformat(), date_to.isoformat())
    bot = Bot(token=config.telegram_bot_token)
    try:
        if data is None:
            await bot.send_message(
                config.telegram_admin_id,
                "⚠️ Еженедельный отчёт не собран: Airtable недоступен. "
                "Попробую в следующий понедельник, либо запустите вручную: "
                "python -m scripts.weekly_report",
            )
            return
        report = build_report(data)
        await bot.send_message(config.telegram_admin_id, report)
        logger.info("Еженедельный отчёт отправлен (%d символов)", len(report))
    finally:
        await bot.session.close()
        await airtable.get_client().close()


if __name__ == "__main__":
    asyncio.run(main())
