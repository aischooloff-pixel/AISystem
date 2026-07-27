"""Проверка структуры Airtable-базы: все ли таблицы и поля на месте (Блок 3).

Airtable API не создаёт таблицы программно на бесплатном плане, поэтому скрипт
не чинит базу сам, а сообщает, чего не хватает, и печатает инструкцию по
ручному созданию.

Запуск (из корня проекта, нужны AIRTABLE_API_KEY и AIRTABLE_BASE_ID в .env):

    python -m scripts.setup_airtable --check        # проверить структуру
    python -m scripts.setup_airtable --dump-schema  # выгрузить схему в JSON (для бэкапа)
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

META_URL = "https://api.airtable.com/v0/meta/bases/{base_id}/tables"

# Ожидаемая структура — модель данных из ТЗ, Часть 3.
# Diagnostics включена: Блок 11 (анкета «Точка сбоя») подтверждён Юлией
# 2026-07-27 и входит в Спринт 1.
# Формат: {таблица: {поле: множество допустимых типов Airtable}}.
_DATE = {"date", "dateTime"}
_TEXT = {"singleLineText"}
_LONG = {"multilineText", "richText"}
_NUM = {"number"}
_CHECK = {"checkbox"}
_SELECT = {"singleSelect"}

EXPECTED_SCHEMA: dict[str, dict[str, set[str]]] = {
    "Contacts": {
        "telegram_id": _NUM,
        "name": _TEXT,
        "username": _TEXT,
        "phone": {"phoneNumber", "singleLineText"},
        "email": {"email", "singleLineText"},
        "source": _SELECT,
        "source_detail": _LONG,
        "utm": _TEXT,
        "first_action": _TEXT,
        "first_touch_date": _DATE,
        "last_contact_date": _DATE,
        "status": _SELECT,
        "status_reason": _LONG,
        "status_history": _LONG,
        "request_summary": _LONG,
        "key_phrases": _LONG,
        "interests": _LONG,
        "conversation_history": _LONG,
        "awareness": _SELECT,
        "readiness": _SELECT,
        "urgency": _SELECT,
        "ai_confidence": _NUM,
        "scenario": _SELECT,
        "next_step": _TEXT,
        "next_action_date": _DATE,
        "assigned_to": _SELECT,
        "paused": _CHECK,
        "consent": _CHECK,
        "touches_count": _NUM,
        "product_interest": _SELECT,
        # Тема запроса из «Возможных направлений» продуктовой линейки —
        # для статистики по направлениям практики
        "request_category": _SELECT,
        "result": _SELECT,
        "qualification_completed": _CHECK,
        "handoff_date": _DATE,
        "notes": _LONG,
        # created_at/updated_at бот пишет ЯВНО: computed-типы
        # (createdTime/lastModifiedTime) отвергли бы запись — только date/dateTime
        "created_at": _DATE,
        "updated_at": _DATE,
    },
    "Touches": {
        "contact_telegram_id": _NUM,
        "date": _DATE,
        "type": _SELECT,
        "description": _LONG,
        "source": _SELECT,
        "related_post": _TEXT,
        "raw_content": _LONG,
        "created_at": _DATE,
    },
    "Comments": {
        "author_telegram_id": _NUM,
        "author_name": _TEXT,
        "author_username": _TEXT,
        "text": _LONG,
        "post_link": {"url", "singleLineText"},
        "post_id": _TEXT,
        "post_topic": _TEXT,
        "date": _DATE,
        "emotion": _SELECT,
        "key_problem": _LONG,
        "interest_level": _NUM,
        "is_potential_client": _CHECK,
        "needs_reply": _CHECK,
        "suggested_reply": _LONG,
        "final_reply": _LONG,
        "reply_status": _SELECT,
        "processed": _CHECK,
        "ai_confidence": _NUM,
        "created_at": _DATE,
    },
    "Posts": {
        "post_id": _TEXT,
        "date": _DATE,
        "topic": _TEXT,
        "text": _LONG,
        "link": {"url", "singleLineText"},
        "purpose": _TEXT,
        "call_to_action": _TEXT,
        "views_count": _NUM,
        "reactions_count": _NUM,
        "comments_count": _NUM,
        "clicks_count": _NUM,
        "potential_clients_count": _NUM,
        "result": _LONG,
        "created_at": _DATE,
    },
    "Tasks": {
        "action": _TEXT,
        "assignee": _SELECT,
        "due_date": _DATE,
        "status": _SELECT,
        "contact_telegram_id": _NUM,
        "reason": _LONG,
        "created_by": _SELECT,
        "completed_at": _DATE,
        "created_at": _DATE,
    },
    "Diagnostics": {
        "contact_telegram_id": _NUM,
        "date": _DATE,
        "booking_status": _SELECT,
        "questionnaire": _LONG,
        "recording_consent": _CHECK,
        "payment_status": _SELECT,
        "meeting_link": {"url", "singleLineText"},
        "materials": _LONG,
        "result": _LONG,
        "next_step": _TEXT,
        "created_at": _DATE,
    },
}

MANUAL_INSTRUCTIONS = """
Как добавить недостающее вручную (5 минут):
  1. Откройте базу на https://airtable.com (нужен доступ владельца).
  2. Недостающая таблица: кнопка «+ Add or import» → «Create blank table»,
     назовите точно как в отчёте (регистр важен: Contacts, Touches, ...).
  3. Недостающее поле: в таблице «+» справа от последнего столбца,
     имя — точно как в отчёте, тип — из отчёта:
       number → Number (precision 0)
       singleLineText → Single line text     multilineText → Long text
       singleSelect → Single select (значения — из модели данных, ТЗ Часть 3)
       checkbox → Checkbox                   date/dateTime → Date (+ время)
       url → URL        email → Email        phoneNumber → Phone
  4. Повторите проверку: python -m scripts.setup_airtable --check
"""


class AirtableSettings(BaseSettings):
    """Скрипту нужны только ключ и база — полный конфиг бота не требуется."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    airtable_api_key: str
    airtable_base_id: str


def load_settings() -> AirtableSettings:
    try:
        return AirtableSettings()
    except ValidationError:
        print(
            "ОШИБКА: не заданы AIRTABLE_API_KEY и/или AIRTABLE_BASE_ID.\n"
            "Заполните .env по образцу .env.example.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def fetch_schema(settings: AirtableSettings) -> list[dict]:
    """Схема базы через Meta API. Понятная ошибка при недоступности."""
    url = META_URL.format(base_id=settings.airtable_base_id)
    headers = {"Authorization": f"Bearer {settings.airtable_api_key}"}
    try:
        response = httpx.get(url, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        print(
            f"ОШИБКА: Airtable недоступен ({exc!r}). Проверьте сеть и повторите.", file=sys.stderr
        )
        raise SystemExit(2) from exc
    if response.status_code == 401:
        print(
            "ОШИБКА: Airtable отверг ключ (401). Проверьте AIRTABLE_API_KEY "
            "и права токена (scope data.records + schema.bases:read).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if response.status_code == 404:
        print(
            "ОШИБКА: база не найдена (404). Проверьте AIRTABLE_BASE_ID и доступ токена "
            "к этой базе.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if response.status_code != 200:
        print(
            f"ОШИБКА: Meta API вернул HTTP {response.status_code}: {response.text[:300]}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return response.json()["tables"]


def check_schema(actual_tables: list[dict]) -> list[str]:
    """Сравнивает фактическую схему с ожидаемой. Возвращает список проблем."""
    problems: list[str] = []
    by_name = {table["name"]: table for table in actual_tables}
    for table_name, expected_fields in EXPECTED_SCHEMA.items():
        table = by_name.get(table_name)
        if table is None:
            problems.append(f"НЕТ ТАБЛИЦЫ: {table_name}")
            continue
        actual_fields = {field["name"]: field["type"] for field in table["fields"]}
        for field_name, allowed_types in expected_fields.items():
            actual_type = actual_fields.get(field_name)
            if actual_type is None:
                problems.append(
                    f"НЕТ ПОЛЯ: {table_name}.{field_name} "
                    f"(тип: {' или '.join(sorted(allowed_types))})"
                )
            elif actual_type not in allowed_types:
                problems.append(
                    f"НЕВЕРНЫЙ ТИП: {table_name}.{field_name} — {actual_type}, "
                    f"ожидался {' или '.join(sorted(allowed_types))}"
                )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка структуры Airtable-базы")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="проверить таблицы и поля")
    group.add_argument(
        "--dump-schema", action="store_true", help="выгрузить схему базы в JSON (stdout)"
    )
    args = parser.parse_args()

    settings = load_settings()
    tables = fetch_schema(settings)

    if args.dump_schema:
        print(json.dumps({"tables": tables}, ensure_ascii=False, indent=2))
        return

    problems = check_schema(tables)
    if not problems:
        total_fields = sum(len(fields) for fields in EXPECTED_SCHEMA.values())
        print(
            f"✅ Структура в порядке: {len(EXPECTED_SCHEMA)} таблиц, "
            f"{total_fields} обязательных полей на месте."
        )
        return
    print(f"❌ Найдено проблем: {len(problems)}\n")
    for problem in problems:
        print(f"  · {problem}")
    print(MANUAL_INSTRUCTIONS)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
