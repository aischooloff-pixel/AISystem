"""Регрессионные тесты исправлений финального аудита."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from bot.config import Config
from bot.services import knowledge
from bot.utils.helpers import automation_stopped, fmt_moment, status_locked_by_yulia
from bot.utils.logger import get_app_logger, setup_logging
from scripts.weekly_report import build_report
from tests.test_block9_10_reports_backup import report_data


@pytest.fixture(autouse=True)
def _reset_knowledge_cache():
    knowledge._cache = None
    knowledge._cache_dir = None
    yield
    knowledge._cache = None
    knowledge._cache_dir = None


def test_get_knowledge_no_arg_keeps_configured_dir(tmp_path: Path) -> None:
    """get_knowledge() без аргумента использует каталог первой загрузки
    (config.knowledge_dir), а не подменяет его дефолтным."""
    custom = tmp_path / "kb"
    custom.mkdir()
    (custom / "brand.md").write_text("Нестандартная база", encoding="utf-8")

    knowledge.load_knowledge(custom)  # как на старте: main.py передаёт config
    text = knowledge.get_knowledge()  # как в system_prompt: без аргумента
    assert "Нестандартная база" in text
    assert knowledge._cache_dir == custom


def test_status_history_with_non_dict_items_does_not_crash() -> None:
    """Ручная правка Long text в Airtable ('["yulia"]') не роняет бота."""
    for raw in ('["yulia"]', "[1]", "[null]", '[{"by": "yulia"}, "мусор"]'):
        fields = {"status_history": raw}
        assert status_locked_by_yulia(fields) in (True, False)  # не бросает
    # Последний dict-элемент учитывается, мусор игнорируется
    assert status_locked_by_yulia({"status_history": '[{"by": "yulia"}, "мусор"]'}) is True
    assert status_locked_by_yulia({"status_history": '["yulia"]'}) is False


def test_fmt_moment_date_only_no_phantom_time() -> None:
    """Дата без времени: без фантомных «03:00» и сдвига таймзоной сервера."""
    assert fmt_moment("2026-07-25") == "25.07"
    assert fmt_moment("2026-07-25", "%d.%m.%Y") == "25.07.2026"
    assert fmt_moment("2026-07-25T14:32:11.000Z") == "25.07 17:32"  # UTC → МСК


def test_automation_stopped_for_client_statuses() -> None:
    """Сценарий 11 ТЗ: клиент in_progress/client — автоматика остановлена."""
    assert automation_stopped({"status": "client"}) is True
    assert automation_stopped({"status": "in_progress"}) is True
    assert automation_stopped({"paused": True}) is True
    assert automation_stopped({"assigned_to": "yulia"}) is True
    assert automation_stopped({"status": "warm", "assigned_to": "ai"}) is False


def test_invalid_log_level_falls_back_to_info(tmp_path: Path) -> None:
    """Опечатка в LOG_LEVEL не роняет бота на старте."""
    setup_logging(tmp_path, "verbose")  # не бросает
    assert get_app_logger().level == logging.INFO
    setup_logging(tmp_path, " info ")  # пробелы тоже не мешают
    assert get_app_logger().level == logging.INFO


def test_weekly_period_across_month_boundary() -> None:
    """Неделя на стыке месяцев: «27 июля – 2 августа», а не «27–2 августа»."""
    data = report_data(
        date_from="2026-07-27",
        date_to="2026-08-02",
        new_contacts=[{"id": "r", "fields": {"source": "site"}}],
    )
    report = build_report(data)
    assert "27 июля – 2 августа" in report


def test_zero_confidence_counts_in_report() -> None:
    """confidence=0 («Ошибка обработки AI») входит в среднее и в передачи
    по низкой уверенности."""
    data = report_data(
        new_contacts=[{"id": "a", "fields": {"source": "site", "ai_confidence": 0}}],
        handoffs=[
            {"id": "b", "fields": {"ai_confidence": 0}},
            {"id": "c", "fields": {"ai_confidence": 100}},
        ],
    )
    report = build_report(data)
    assert "Передач по низкой уверенности: 1" in report
    assert "Средняя уверенность: 33%" in report  # (0 + 0 + 100) / 3


def test_webhook_secret_derivation() -> None:
    """Секрет вебхука: из конфига, иначе детерминированно из токена."""
    from bot.main import webhook_secret_for

    base = dict(
        telegram_bot_token="42:SECRET",
        telegram_admin_id=1,
        telegram_channel_id=-1,
        telegram_discussion_group_id=-2,
        webhook_url="https://x.example",
        openai_api_key="sk",
        airtable_api_key="pat",
        airtable_base_id="app",
        _env_file=None,
    )
    auto = webhook_secret_for(Config(**base))
    assert auto == webhook_secret_for(Config(**base))  # детерминирован
    assert len(auto) == 32
    explicit = webhook_secret_for(Config(**base, webhook_secret="my-secret"))
    assert explicit == "my-secret"
