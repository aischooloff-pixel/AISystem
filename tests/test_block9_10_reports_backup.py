"""Тесты Блоков 9–10: еженедельный отчёт, таймауты, экспорт для бэкапа."""

from __future__ import annotations

from datetime import date

import pytest

from bot.services import airtable
from scripts.check_timeouts import process_cold, process_reminders
from scripts.weekly_report import build_report, last_full_week
from tests.test_block6_qualification import FakeBot


def record(fields: dict) -> dict:
    return {"id": f"rec{id(fields)}", "fields": fields}


def report_data(**over) -> dict:
    data = {
        "date_from": "2026-07-20",
        "date_to": "2026-07-26",
        "new_contacts": [],
        "handoffs": [],
        "comments": [],
        "touches": [],
        "all_contacts": [],
        "open_tasks": [],
        "pending_comments": [],
    }
    data.update(over)
    return data


def test_last_full_week():
    # Понедельник 27.07.2026 → прошлая неделя 20–26 июля
    assert last_full_week(date(2026, 7, 27)) == (date(2026, 7, 20), date(2026, 7, 26))
    # Среда → та же прошлая полная неделя
    assert last_full_week(date(2026, 7, 29)) == (date(2026, 7, 20), date(2026, 7, 26))


def test_report_with_no_data_says_so():
    """Критерий Блока 9: при отсутствии данных отчёт корректен, без ложных
    значений — прямо написано, что данных нет."""
    report = build_report(report_data())
    assert "Данных за период нет" in report
    assert "0%" not in report  # нули не выдаются за достижения


def test_report_metrics_computed():
    data = report_data(
        new_contacts=[
            record({"source": "telegram_channel", "ai_confidence": 90}),
            record({"source": "telegram_channel"}),
            record({"source": "referral", "result": "no_response"}),
        ],
        handoffs=[
            record({"ai_confidence": 70, "result": "entered"}),
            record({"ai_confidence": 95}),
        ],
        comments=[
            record({"is_potential_client": True, "post_id": "42", "post_topic": "Выгорание"}),
            record({"is_potential_client": False, "post_id": "42"}),
        ],
        touches=[
            record({"type": "question_answered", "contact_telegram_id": 1}),
            record({"type": "question_answered", "contact_telegram_id": 2}),
            record({"type": "comment", "contact_telegram_id": 3}),
        ],
        all_contacts=[
            record({"status": "hot"}),
            record({"status": "warm"}),
            record({"status": "warm"}),
        ],
        open_tasks=[record({}), record({})],
        pending_comments=[record({})],
    )
    report = build_report(data)
    assert "ОТЧЁТ ЗА НЕДЕЛЮ · 20–26 июля" in report
    assert "Новых обращений: 3" in report
    assert "Передано вам: 2" in report
    assert "Вошли в работу: 1" in report
    assert "telegram_channel: 2 (67%)" in report
    assert "Обработано комментариев: 2" in report
    assert "Выявлено потенциальных: 1" in report
    assert "«Выгорание»" in report
    assert "Передач по низкой уверенности: 1" in report
    assert "🔥 Горячих: 1 · 🟡 Тёплых: 2" in report
    assert "Открытых задач: 2" in report
    assert "Комментариев на утверждение: 1" in report


# ── check_timeouts ──


class TimeoutCRM:
    def __init__(self, monkeypatch, stale_records, touches=None):
        self.stale_records = stale_records
        self.touches_by_tid = touches or {}
        self.sent_touches: list[tuple] = []
        self.tasks: list[str] = []
        self.updates: list[tuple[str, dict]] = []
        self.status_changes: list[tuple] = []

        async def get_stale_contacts(hours):
            return self.stale_records

        async def get_touches(tid):
            return self.touches_by_tid.get(tid, [])

        async def add_touch(tid, type, description, **kwargs):
            self.sent_touches.append((tid, type, description))
            return {}

        async def get_open_tasks(assignee=None):
            return []

        async def create_task(action, assignee, due, reason, **kwargs):
            self.tasks.append(action)
            return {}

        async def update_contact(record_id, data):
            self.updates.append((record_id, data))
            return {}

        async def add_status_change(record_id, old, new, reason, by):
            self.status_changes.append((record_id, old, new, by))
            return {}

        for name, fn in [
            ("get_stale_contacts", get_stale_contacts),
            ("get_touches", get_touches),
            ("add_touch", add_touch),
            ("get_open_tasks", get_open_tasks),
            ("create_task", create_task),
            ("update_contact", update_contact),
            ("add_status_change", add_status_change),
        ]:
            monkeypatch.setattr(airtable, name, fn)


async def test_reminder_sent_once(monkeypatch):
    """Критерий Блока 6: напоминание отправляется один раз."""
    contact = record(
        {"telegram_id": 111, "name": "Анна", "last_contact_date": "2026-07-25T10:00:00"}
    )
    crm = TimeoutCRM(monkeypatch, [contact])
    bot = FakeBot()

    sent = await process_reminders(bot, 24, 72)
    assert sent == 1
    assert bot.sent[0][0] == 111
    assert any("Проверить диалог с Анна" in t for t in crm.tasks)

    # Второй прогон: касание-напоминание уже есть → повторно не шлём
    crm2 = TimeoutCRM(
        monkeypatch,
        [contact],
        touches={
            111: [
                record(
                    {
                        "type": "nurturing_touch",
                        "description": "Напоминание 24 ч: «…»",
                        "date": "2026-07-26T11:00:00",
                    }
                )
            ]
        },
    )
    bot2 = FakeBot()
    assert await process_reminders(bot2, 24, 72) == 0
    assert bot2.sent == []


async def test_cold_after_72h(monkeypatch):
    """72 ч молчания → cold, result=no_response, автоматика остановлена."""
    contact = record({"telegram_id": 222, "status": "warm", "last_contact_date": "x"})
    contact["id"] = "rec222"
    crm = TimeoutCRM(monkeypatch, [contact])

    changed = await process_cold(72)
    assert changed == 1
    assert crm.status_changes[0][1:] == ("warm", "cold", "ai")
    assert crm.updates[0][1] == {"result": "no_response", "paused": True}


async def test_cold_respects_yulia_lock(monkeypatch):
    import json

    contact = record(
        {
            "telegram_id": 333,
            "status": "warm",
            "status_history": json.dumps([{"by": "yulia", "to": "warm"}]),
        }
    )
    crm = TimeoutCRM(monkeypatch, [contact])
    assert await process_cold(72) == 0
    assert crm.updates == []


async def test_handed_off_clients_not_reminded(monkeypatch):
    """Переданные Юлии (qualification_completed) не получают напоминаний."""
    contact = record(
        {"telegram_id": 444, "qualification_completed": True, "last_contact_date": "x"}
    )
    crm = TimeoutCRM(monkeypatch, [contact])
    bot = FakeBot()
    assert await process_reminders(bot, 24, 72) == 0
    assert bot.sent == []
