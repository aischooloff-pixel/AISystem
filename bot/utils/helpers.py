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
    """История статусов контакта из JSON-поля; битые данные — пустой список.

    Поле — обычный Long text, его можно испортить ручной правкой в Airtable:
    не-словари внутри списка отфильтровываются, а не роняют бота.
    """
    raw = fields.get("status_history") or "[]"
    try:
        history = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(history, list):
        return []
    return [entry for entry in history if isinstance(entry, dict)]


def automation_stopped(fields: dict) -> bool:
    """Автоматика для контакта остановлена?

    Пауза, передача Юлии, а также статусы «в работе»/«клиент» (ТЗ,
    сценарий 11: действующий клиент не проходит квалификацию заново).
    """
    return bool(
        fields.get("paused")
        or fields.get("assigned_to") == "yulia"
        or fields.get("status") in ("in_progress", "client")
    )


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
    """ISO-дата из Airtable → короткий вид по Москве («25.07 14:32»).

    Значение «только дата» (поле типа date) форматируется без времени
    и без конвертации таймзоны — иначе появлялись бы фантомные «03:00»
    и сдвиг даты на серверах не в UTC.
    """
    if not iso_value:
        return "—"
    if len(iso_value) == 10:  # YYYY-MM-DD
        try:
            day = datetime.fromisoformat(iso_value).date()
        except ValueError:
            return "—"
        return day.strftime("%d.%m.%Y" if "%Y" in fmt else "%d.%m")
    try:
        moment = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(MSK).strftime(fmt)
