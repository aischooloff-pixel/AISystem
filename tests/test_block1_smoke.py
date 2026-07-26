"""Smoke-тесты Блока 1: конфигурация, логирование, сборка приложения.

Живой ответ бота на /start проверяется на деплое (нужны реальный токен
и публичный URL) — здесь проверяем всё, что проверяемо офлайн.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from bot.config import load_config
from bot.utils.logger import (
    APP_LOGGER_NAME,
    DECISIONS_LOGGER_NAME,
    LOG_BACKUP_DAYS,
    get_app_logger,
    get_decisions_logger,
    log_ai_decision,
    setup_logging,
)

# Обязательные переменные (без дефолтов в Config)
REQUIRED_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ADMIN_ID",
    "TELEGRAM_CHANNEL_ID",
    "TELEGRAM_DISCUSSION_GROUP_ID",
    "WEBHOOK_URL",
    "OPENAI_API_KEY",
    "AIRTABLE_API_KEY",
    "AIRTABLE_BASE_ID",
]

VALID_ENV = {
    "TELEGRAM_BOT_TOKEN": "42:TEST_TOKEN",
    "TELEGRAM_ADMIN_ID": "111",
    "TELEGRAM_CHANNEL_ID": "-100200",
    "TELEGRAM_DISCUSSION_GROUP_ID": "-100300",
    "WEBHOOK_URL": "https://example.com/",
    "OPENAI_API_KEY": "sk-test",
    "AIRTABLE_API_KEY": "pat-test",
    "AIRTABLE_BASE_ID": "appTEST",
}


@pytest.fixture(autouse=True)
def _clean_required_env(monkeypatch: pytest.MonkeyPatch):
    """Изолирует тесты от реального окружения и .env разработчика."""
    for var in REQUIRED_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _close_log_handlers():
    """Закрывает файловые обработчики после теста — иначе Windows не даст
    pytest удалить tmp_path."""
    yield
    for name in (APP_LOGGER_NAME, DECISIONS_LOGGER_NAME):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_config_fails_fast_without_required_vars(capsys: pytest.CaptureFixture) -> None:
    """Критерий Блока 1: при отсутствии обязательной переменной — понятная
    ошибка на старте, а не падение в рантайме."""
    with pytest.raises(SystemExit) as exc_info:
        load_config(env_file=None)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "ОШИБКА КОНФИГУРАЦИИ" in err
    for var in REQUIRED_VARS:
        assert var in err, f"в сообщении об ошибке нет имени переменной {var}"


def test_config_loads_and_has_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Конфиг читается, дефолты соответствуют ТЗ (Часть 5)."""
    for var, value in VALID_ENV.items():
        monkeypatch.setenv(var, value)
    config = load_config(env_file=None)
    assert config.telegram_admin_id == 111
    assert config.webhook_path == "/webhook"
    assert config.webapp_port == 8080
    # Порог 85% из «Критериев квалификации», п. 9
    assert config.ai_confidence_threshold == 85
    assert config.timeout_reminder_hours == 24
    assert config.timeout_cold_hours == 72
    assert config.openai_model == "gpt-4o-mini"
    # Слэш на конце базового URL не даёт двойного слэша в итоговом
    assert config.webhook_full_url == "https://example.com/webhook"


def test_invalid_value_reported_clearly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Нечисловой admin id — понятная ошибка, не traceback."""
    for var, value in VALID_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("TELEGRAM_ADMIN_ID", "не число")
    with pytest.raises(SystemExit):
        load_config(env_file=None)
    err = capsys.readouterr().err
    assert "TELEGRAM_ADMIN_ID" in err
    assert "некорректное значение" in err


def test_both_log_files_created_and_written(tmp_path: Path) -> None:
    """Критерий Блока 1: оба лог-файла создаются и пишутся."""
    setup_logging(tmp_path, "INFO")
    get_app_logger().info("тестовое событие app")
    log_ai_decision(
        telegram_id=123456789,
        scenario="B_problem",
        status="warm",
        confidence=87,
        reason="описал повторяющуюся ситуацию, задал 2 вопроса о методе",
        awareness="medium",
        readiness="medium",
        urgency="low",
        next_action="предложить диагностику",
        needs_yulia=False,
        response_sent="Понимаю. Если вы хотите разобраться...",
    )

    app_log = (tmp_path / "app.log").read_text(encoding="utf-8")
    decisions_log = (tmp_path / "ai_decisions.log").read_text(encoding="utf-8")
    assert "тестовое событие app" in app_log
    # Формат решения — обязательный, из ТЗ (Блок 1)
    assert "[DECISION] telegram_id=123456789" in decisions_log
    assert "scenario=B_problem" in decisions_log
    assert "status=warm confidence=87" in decisions_log
    assert "needs_yulia=false" in decisions_log
    # Решения не дублируются в технический лог
    assert "[DECISION]" not in app_log


def test_rotation_configured(tmp_path: Path) -> None:
    """Критерий Блока 1: ротация ежедневная, хранение 30 дней."""
    setup_logging(tmp_path, "INFO")
    for logger in (get_app_logger(), get_decisions_logger()):
        file_handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert file_handlers, f"у логгера {logger.name} нет файлового обработчика"
        handler = file_handlers[0]
        assert handler.when.upper() == "MIDNIGHT"
        assert handler.backupCount == LOG_BACKUP_DAYS == 30


def test_repeated_setup_does_not_duplicate_handlers(tmp_path: Path) -> None:
    """Повторная настройка (например, в тестах) не приводит к дублям записей."""
    setup_logging(tmp_path, "INFO")
    setup_logging(tmp_path, "INFO")
    get_app_logger().info("одна запись")
    app_log = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert app_log.count("одна запись") == 1


def test_dispatcher_builds_with_start_router() -> None:
    """Приложение собирается: диспетчер создаётся, роутер /start подключён."""
    from bot.main import create_dispatcher

    dispatcher = create_dispatcher()
    assert any(router.name == "start" for router in dispatcher.sub_routers)
