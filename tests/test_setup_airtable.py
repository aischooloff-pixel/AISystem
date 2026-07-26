"""Тесты Блока 3: проверка структуры базы (scripts/setup_airtable.py).

Скрипт должен находить отсутствующие таблицы, отсутствующие поля
и неверные типы — и молчать, когда всё на месте.
"""

from __future__ import annotations

from scripts.setup_airtable import EXPECTED_SCHEMA, check_schema


def _valid_schema() -> list[dict]:
    """Схема, собранная из EXPECTED_SCHEMA (первый допустимый тип каждого поля)."""
    return [
        {
            "name": table_name,
            "fields": [
                {"name": field_name, "type": sorted(allowed)[0]}
                for field_name, allowed in fields.items()
            ],
        }
        for table_name, fields in EXPECTED_SCHEMA.items()
    ]


def test_valid_schema_passes() -> None:
    assert check_schema(_valid_schema()) == []


def test_missing_table_detected() -> None:
    schema = [table for table in _valid_schema() if table["name"] != "Touches"]
    problems = check_schema(schema)
    assert problems == ["НЕТ ТАБЛИЦЫ: Touches"]


def test_missing_field_detected() -> None:
    schema = _valid_schema()
    contacts = next(t for t in schema if t["name"] == "Contacts")
    contacts["fields"] = [f for f in contacts["fields"] if f["name"] != "telegram_id"]
    problems = check_schema(schema)
    assert any("Contacts.telegram_id" in p and "НЕТ ПОЛЯ" in p for p in problems)


def test_wrong_type_detected() -> None:
    schema = _valid_schema()
    tasks = next(t for t in schema if t["name"] == "Tasks")
    for field in tasks["fields"]:
        if field["name"] == "status":
            field["type"] = "singleLineText"
    problems = check_schema(schema)
    assert any("Tasks.status" in p and "НЕВЕРНЫЙ ТИП" in p for p in problems)


def test_date_fields_accept_both_date_and_datetime() -> None:
    """Поле «Date» из ТЗ в живой базе может быть date или dateTime — оба валидны."""
    schema = _valid_schema()
    touches = next(t for t in schema if t["name"] == "Touches")
    for field in touches["fields"]:
        if field["name"] == "date":
            field["type"] = "dateTime"
    assert check_schema(schema) == []


def test_diagnostics_not_required() -> None:
    """Diagnostics не входит в обязательную схему, пока Юлия не подтвердит
    анкету (решение в logs.txt, Сессия 1)."""
    assert "Diagnostics" not in EXPECTED_SCHEMA
