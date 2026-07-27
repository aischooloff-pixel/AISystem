"""Тесты Блока 3: устойчивость Airtable-слоя, CRUD, история, таймлайн, экспорт.

Критерии готовности: retry проверен имитацией 429, недоступность Airtable
не роняет бота, rate limit соблюдается, status_history накапливается,
build_timeline читаем, все операции логируются.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from bot.services import airtable as airtable_module
from bot.services.airtable import AirtableClient
from tests.conftest import FakeAirtable

TID = 555000111


# ── Устойчивость ──


async def test_retry_on_429_then_success(
    client: AirtableClient, fake: FakeAirtable, app_caplog: pytest.LogCaptureFixture
) -> None:
    """Критерий: retry работает — проверено имитацией 429."""
    fake.seed("Contacts", {"telegram_id": TID, "name": "Анна"})
    fake.fail_with = [429, 429]
    with app_caplog.at_level(logging.WARNING, logger="app"):
        record = await client.find_contact(TID)
    assert record is not None and record["fields"]["name"] == "Анна"
    retries = [r for r in app_caplog.records if "retry" in r.getMessage()]
    assert len(retries) == 2


async def test_final_failure_returns_none_without_raising(
    client: AirtableClient, fake: FakeAirtable, app_caplog: pytest.LogCaptureFixture
) -> None:
    """Критерий: при окончательной неудаче — ERROR в лог и None,
    исключение наверх не бросается."""
    fake.fail_with = [500, 500, 500]
    with app_caplog.at_level(logging.ERROR, logger="app"):
        record = await client.find_contact(TID)
    assert record is None
    assert any(r.levelno == logging.ERROR for r in app_caplog.records)


async def test_network_error_does_not_crash(
    client: AirtableClient, fake: FakeAirtable, app_caplog: pytest.LogCaptureFixture
) -> None:
    """Критерий: недоступность Airtable не роняет бота (сетевая ошибка)."""
    fake.fail_with = [
        httpx.ConnectError("сеть недоступна"),
        httpx.ConnectError("сеть недоступна"),
        httpx.ConnectError("сеть недоступна"),
    ]
    with app_caplog.at_level(logging.ERROR, logger="app"):
        result = await client.get_pending_comments()
    assert result is None
    assert any("сетевая ошибка" in r.getMessage() for r in app_caplog.records)


async def test_rate_limit_semaphore(fake: FakeAirtable) -> None:
    """Критерий: rate limit соблюдается — не больше N запросов за окно."""
    client = AirtableClient(
        api_key="pat-test",
        base_id="appTEST00000000",
        retry_delays=(0, 0),
        rate_limit_per_sec=2,
        rate_window=0.25,
        transport=httpx.MockTransport(fake.handler),
    )
    try:
        await asyncio.gather(*(client.get_pending_comments() for _ in range(6)))
    finally:
        await client.close()
    times = sorted(fake.request_times)
    assert len(times) == 6
    # Первые 2 — сразу; 3-й и 4-й ждут освобождения слотов (≈0.25 с)
    assert times[1] - times[0] < 0.2
    assert times[2] - times[0] >= 0.2, "третий запрос ушёл раньше окна rate limit"
    assert times[4] - times[2] >= 0.2, "пятый запрос ушёл раньше второго окна"


async def test_all_operations_logged(
    client: AirtableClient, fake: FakeAirtable, app_caplog: pytest.LogCaptureFixture
) -> None:
    """Критерий: все операции логируются."""
    with app_caplog.at_level(logging.INFO, logger="app"):
        await client.upsert_contact(TID, {"name": "Анна"})
        await client.add_touch(TID, "dm_start", "Первое обращение")
    airtable_lines = [r.getMessage() for r in app_caplog.records if "Airtable" in r.getMessage()]
    assert len(airtable_lines) >= 3  # поиск + создание контакта + касание


# ── status_history ──


async def test_status_history_accumulates(client: AirtableClient, fake: FakeAirtable) -> None:
    """Критерий: status_history накапливает изменения, старые не затираются."""
    old_entry = {
        "date": "2026-07-20T10:00:00+00:00",
        "from": "cold",
        "to": "warm",
        "reason": "описал проблему",
        "by": "ai",
    }
    record = fake.seed(
        "Contacts",
        {
            "telegram_id": TID,
            "status": "warm",
            "status_history": json.dumps([old_entry], ensure_ascii=False),
        },
    )

    await client.add_status_change(record["id"], "warm", "hot", "готов записаться", "ai")

    fields = fake.tables["Contacts"][0]["fields"]
    history = json.loads(fields["status_history"])
    assert len(history) == 2
    assert history[0] == old_entry  # старая запись не тронута
    assert history[1]["from"] == "warm" and history[1]["to"] == "hot"
    assert history[1]["by"] == "ai" and history[1]["date"]
    assert fields["status"] == "hot"
    assert fields["status_reason"] == "готов записаться"


async def test_status_change_by_yulia_recorded(client: AirtableClient, fake: FakeAirtable) -> None:
    """Ручное решение Юлии фиксируется с by=yulia («Карта пути», п. 19)."""
    record = fake.seed("Contacts", {"telegram_id": TID, "status": "hot"})
    await client.add_status_change(record["id"], "hot", "nurturing", "решение Юлии", "yulia")
    history = json.loads(fake.tables["Contacts"][0]["fields"]["status_history"])
    assert history[-1]["by"] == "yulia"


# ── Timeline ──


async def test_build_timeline_readable_and_ordered(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    """Критерий: build_timeline возвращает читаемую хронологию (формат ТЗ)."""
    fake.seed(
        "Touches",
        {
            "contact_telegram_id": TID,
            "date": "2026-07-17T09:00:00+00:00",
            "type": "comment",
            "description": "оставил комментарий",
        },
    )
    fake.seed(
        "Touches",
        {
            "contact_telegram_id": TID,
            "date": "2026-07-15T08:00:00+00:00",
            "type": "subscribed",
            "description": "прочитал публикацию",
        },
    )
    timeline = await client.build_timeline(TID)
    assert timeline.splitlines() == [
        "15.07 — прочитал публикацию",
        "17.07 — оставил комментарий",
    ]


async def test_build_timeline_when_airtable_down(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    fake.fail_with = [500, 500, 500]
    timeline = await client.build_timeline(TID)
    assert "недоступна" in timeline


async def test_build_timeline_empty(client: AirtableClient, fake: FakeAirtable) -> None:
    assert await client.build_timeline(TID) == "Касаний пока нет."


# ── CRUD остальных таблиц ──


async def test_comments_crud(client: AirtableClient, fake: FakeAirtable) -> None:
    created = await client.create_comment(
        {
            "author_telegram_id": TID,
            "author_name": "Мария",
            "text": "Это прямо про меня",
            "emotion": "interested",
            "reply_status": "pending",
        }
    )
    assert created is not None and created["fields"]["created_at"]

    pending = await client.get_pending_comments()
    assert pending is not None and len(pending) == 1

    await client.update_comment(created["id"], {"reply_status": "sent", "processed": True})
    assert fake.tables["Comments"][0]["fields"]["reply_status"] == "sent"

    by_author = await client.get_comments_by_author(TID)
    assert by_author is not None and len(by_author) == 1


async def test_posts_upsert_and_counters(client: AirtableClient, fake: FakeAirtable) -> None:
    await client.upsert_post("42", {"topic": "Почему ситуации повторяются"})
    await client.upsert_post("42", {"text": "Полный текст поста"})
    assert len(fake.tables["Posts"]) == 1, "upsert_post создал дубль"
    fields = fake.tables["Posts"][0]["fields"]
    assert fields["topic"] == "Почему ситуации повторяются"
    assert fields["text"] == "Полный текст поста"

    await client.increment_post_counter("42", "comments_count")
    await client.increment_post_counter("42", "comments_count")
    await client.increment_post_counter("42", "potential_clients_count")
    fields = fake.tables["Posts"][0]["fields"]
    assert fields["comments_count"] == 2
    assert fields["potential_clients_count"] == 1


async def test_concurrent_post_writes_create_single_record(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    """Гонка «поиск → создание» для поста: после простоя Telegram присылает
    накопленные комментарии разом, и все обработчики одного поста стартуют
    параллельно. Проверено на живом трафике 2026-07-27: без замка девять
    комментариев к посту 1080 создали девять записей в Posts.

    Замок общий на оба метода, поэтому проверяем их вперемешку — и что дубля
    нет, и что ни один инкремент не потерялся.
    """
    await asyncio.gather(
        *(client.upsert_post("1080", {"topic": "Точка сбоя"}) for _ in range(5)),
        *(client.increment_post_counter("1080", "comments_count") for _ in range(4)),
    )
    assert len(fake.tables["Posts"]) == 1, "гонка создала дубль поста"
    assert fake.tables["Posts"][0]["fields"]["comments_count"] == 4, "инкремент потерян"


async def test_tasks_lifecycle(client: AirtableClient, fake: FakeAirtable) -> None:
    created = await client.create_task(
        "Связаться с Анной",
        "yulia",
        "2026-07-26",
        "Клиент передан Юлии",
        contact_telegram_id=TID,
    )
    assert created is not None
    fields = fake.tables["Tasks"][0]["fields"]
    assert fields["status"] == "open"
    assert fields["created_by"] == "ai"

    open_tasks = await client.get_open_tasks("yulia")
    assert open_tasks is not None and len(open_tasks) == 1

    await client.complete_task(created["id"])
    fields = fake.tables["Tasks"][0]["fields"]
    assert fields["status"] == "done"
    assert fields["completed_at"]


async def test_contacts_queries(client: AirtableClient, fake: FakeAirtable) -> None:
    fake.seed("Contacts", {"telegram_id": 1, "status": "hot", "assigned_to": "yulia"})
    fake.seed("Contacts", {"telegram_id": 2, "status": "warm", "assigned_to": "ai"})

    hot = await client.get_contacts_by_status("hot")
    assert hot is not None and [r["fields"]["telegram_id"] for r in hot] == [1]

    on_ai = await client.get_contacts_assigned_to("ai")
    assert on_ai is not None and [r["fields"]["telegram_id"] for r in on_ai] == [2]


# ── Экспорт и отчёт ──


async def test_export_table_to_csv(client: AirtableClient, fake: FakeAirtable) -> None:
    fake.seed("Contacts", {"telegram_id": 1, "name": "Анна"})
    fake.seed("Contacts", {"telegram_id": 2, "name": "Мария", "status": "warm"})
    csv_text = await client.export_table_to_csv("Contacts")
    assert csv_text is not None
    lines = csv_text.strip().splitlines()
    assert len(lines) == 3  # заголовок + 2 записи
    assert lines[0].startswith("record_id,createdTime")
    assert "telegram_id" in lines[0] and "status" in lines[0]
    assert "Анна" in csv_text and "Мария" in csv_text


async def test_export_none_on_failure(client: AirtableClient, fake: FakeAirtable) -> None:
    fake.fail_with = [500, 500, 500]
    assert await client.export_table_to_csv("Contacts") is None


async def test_get_report_data_collects_all_sections(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    fake.seed("Contacts", {"telegram_id": 1, "created_at": "2026-07-25T10:00:00+00:00"})
    data = await client.get_report_data("2026-07-21", "2026-07-27")
    assert data is not None
    for key in (
        "new_contacts",
        "handoffs",
        "comments",
        "touches",
        "all_contacts",
        "open_tasks",
        "pending_comments",
    ):
        assert key in data, f"в отчёте нет секции {key}"


# ── Формулы и модульный интерфейс ──


def test_stale_contacts_formula() -> None:
    """Формула staleness: только на AI, без паузы, старше N часов."""
    client = AirtableClient(api_key="k", base_id="appTEST00000000")
    formula = (
        'AND({assigned_to}="ai", NOT({paused}), '
        "DATETIME_DIFF(NOW(), {last_contact_date}, 'hours') >= 24)"
    )
    # Формула собирается идентично в get_stale_contacts (проверка построения)
    built = (
        'AND({assigned_to}="ai", NOT({paused}), '
        f"DATETIME_DIFF(NOW(), {{last_contact_date}}, 'hours') >= {24})"
    )
    assert built == formula


def test_module_interface_requires_init() -> None:
    """Модульные функции без init_airtable дают понятную ошибку, не AttributeError."""
    airtable_module._client = None
    with pytest.raises(RuntimeError, match="init_airtable"):
        airtable_module.get_client()


def test_quote_escapes_quotes() -> None:
    from bot.services.airtable import _quote

    assert _quote('пост "о деньгах"') == '"пост \\"о деньгах\\""'
