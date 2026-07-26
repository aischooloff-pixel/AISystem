"""Тест дедупликации (критерий Блока 3 и контрольный тест из ТЗ, Часть 3).

«Человек оставил 3 комментария и написал в бот → в Contacts 1 запись,
в Touches — 4 записи». И жёстче: 5 сообщений от одного → 1 контакт, 5 касаний.
"""

from __future__ import annotations

from tests.conftest import FakeAirtable
from bot.services.airtable import AirtableClient

TID = 123456789


async def test_five_messages_one_contact_five_touches(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    """Критерий Блока 3: 5 сообщений от одного человека → 1 запись в Contacts,
    5 в Touches, touches_count = 5."""
    for i in range(5):
        await client.upsert_contact(TID, {"name": "Анна", "source": "telegram_dm"})
        await client.add_touch(TID, "dm_start", f"Сообщение {i + 1}")

    contacts = fake.tables["Contacts"]
    touches = fake.tables["Touches"]
    assert len(contacts) == 1, "дедупликация нарушена: контакт задублирован"
    assert len(touches) == 5
    assert contacts[0]["fields"]["touches_count"] == 5
    assert contacts[0]["fields"]["telegram_id"] == TID


async def test_three_comments_and_dm_scenario(client: AirtableClient, fake: FakeAirtable) -> None:
    """Контрольный пример из ТЗ: 3 комментария + переход в бот →
    1 контакт, 4 касания."""
    for i in range(3):
        await client.upsert_contact(TID, {"name": "Анна", "source": "telegram_comment"})
        await client.add_touch(TID, "comment", f"Комментарий {i + 1}", source="telegram_comment")
    await client.upsert_contact(TID, {"source": "telegram_comment"})
    await client.add_touch(TID, "dm_start", "Первое обращение в бот")

    assert len(fake.tables["Contacts"]) == 1
    assert len(fake.tables["Touches"]) == 4
    assert fake.tables["Contacts"][0]["fields"]["touches_count"] == 4


async def test_upsert_does_not_create_when_search_fails(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    """При сбое поиска запись НЕ создаётся — иначе дубль (правило «НИКОГДА
    не создавать без предварительной проверки»)."""
    fake.fail_with = [500, 500, 500]  # все попытки поиска исчерпаны
    result = await client.upsert_contact(TID, {"name": "Анна"})
    assert result is None
    assert fake.create_calls == 0
    assert fake.tables.get("Contacts", []) == []


async def test_first_upsert_sets_required_defaults(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    """Первое касание заполняет обязательные поля Contacts (ТЗ, Часть 3)."""
    await client.upsert_contact(TID, {"name": "Анна", "source": "referral"})
    fields = fake.tables["Contacts"][0]["fields"]
    for required in (
        "first_touch_date",
        "last_contact_date",
        "status",
        "status_reason",
        "assigned_to",
        "created_at",
        "updated_at",
    ):
        assert fields.get(required), f"не заполнено обязательное поле {required}"
    assert fields["touches_count"] == 1
    assert fields["paused"] is False
    assert fields["consent"] is True


async def test_repeat_upsert_updates_last_contact_and_merges_data(
    client: AirtableClient, fake: FakeAirtable
) -> None:
    """Повторное касание обновляет last_contact_date/updated_at и данные,
    не создавая дубля."""
    await client.upsert_contact(TID, {"name": "Анна"})
    record = fake.tables["Contacts"][0]
    record["fields"]["last_contact_date"] = "2026-07-01T00:00:00+00:00"

    await client.upsert_contact(TID, {"username": "anna_example"})
    fields = fake.tables["Contacts"][0]["fields"]
    assert len(fake.tables["Contacts"]) == 1
    assert fields["username"] == "anna_example"
    assert fields["name"] == "Анна"  # прежние данные не потеряны
    assert fields["last_contact_date"] != "2026-07-01T00:00:00+00:00"
