"""Вспомогательные функции: история статусов, форматирование (Блоки 6–7)."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

LEVEL_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}

STATUS_RU = {
    "hot": "горячий",
    "warm": "тёплый",
    "cold": "холодный",
    "non_target": "нецелевой",
    "in_progress": "в работе",
    "client": "клиент",
}


def parse_status_history(fields: dict) -> list[dict]:
    """История статусов контакта из JSON-поля; битые данные — пустой список."""
    raw = fields.get("status_history") or "[]"
    try:
        history = json.loads(raw)
        return history if isinstance(history, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def status_locked_by_yulia(fields: dict) -> bool:
    """Текущий статус установлен Юлией вручную?

    «Карта клиентского пути», п. 19: при конфликте автоматики и решения Юлии
    приоритет у Юлии — автоматика не переопределяет её статус.
    """
    history = parse_status_history(fields)
    return bool(history) and history[-1].get("by") == "yulia"


def level_ru(value: str | None) -> str:
    """high/medium/low → по-русски для карточки."""
    return LEVEL_RU.get(value or "", "—")


def fmt_moment(iso_value: str | None, fmt: str = "%d.%m %H:%M") -> str:
    """ISO-дата из Airtable → короткий вид по Москве («25.07 14:32»)."""
    if not iso_value:
        return "—"
    try:
        moment = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return moment.astimezone(MSK).strftime(fmt)
