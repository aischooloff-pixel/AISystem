"""Логирование.

Два независимых логгера (ТЗ, Блок 1):

=============  ========================  ==============================================
Логгер         Файл                      Содержание
=============  ========================  ==============================================
``app``        ``logs/app.log``          Технические события, ошибки, traceback
``ai_decisions``  ``logs/ai_decisions.log``  Все решения AI, структурированно
=============  ========================  ==============================================

Ротация ежедневная, хранение 30 дней. Формат записи решения AI обязателен —
из требования Юлии «возможность увидеть, что отправил AI и какое решение принял».
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

APP_LOGGER_NAME = "app"
DECISIONS_LOGGER_NAME = "ai_decisions"

# ТЗ, Блок 1: ротация ежедневно, хранение 30 дней
LOG_ROTATION_WHEN = "midnight"
LOG_BACKUP_DAYS = 30

_APP_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
_DECISIONS_FORMAT = "[%(asctime)s] %(message)s"  # маркер [DECISION] входит в сообщение
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _make_file_handler(path: Path, fmt: str) -> TimedRotatingFileHandler:
    """Файловый обработчик с ежедневной ротацией и хранением 30 дней."""
    handler = TimedRotatingFileHandler(
        path, when=LOG_ROTATION_WHEN, backupCount=LOG_BACKUP_DAYS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(fmt, datefmt=_DATEFMT))
    return handler


def setup_logging(log_dir: Path, log_level: str = "INFO") -> None:
    """Настраивает оба логгера. Вызывается один раз на старте приложения.

    Повторный вызов пересоздаёт обработчики (нужно для тестов и не даёт
    дублировать записи).
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_logger.setLevel(log_level.upper())
    for handler in app_logger.handlers[:]:
        handler.close()
        app_logger.removeHandler(handler)
    app_logger.addHandler(_make_file_handler(log_dir / "app.log", _APP_FORMAT))
    # Дублируем в stderr: под systemd это попадает в journalctl
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_APP_FORMAT, datefmt=_DATEFMT))
    app_logger.addHandler(console)
    app_logger.propagate = False

    decisions_logger = logging.getLogger(DECISIONS_LOGGER_NAME)
    decisions_logger.setLevel(logging.INFO)
    for handler in decisions_logger.handlers[:]:
        handler.close()
        decisions_logger.removeHandler(handler)
    decisions_logger.addHandler(_make_file_handler(log_dir / "ai_decisions.log", _DECISIONS_FORMAT))
    decisions_logger.propagate = False


def get_app_logger() -> logging.Logger:
    """Логгер технических событий (``logs/app.log``)."""
    return logging.getLogger(APP_LOGGER_NAME)


def get_decisions_logger() -> logging.Logger:
    """Логгер решений AI (``logs/ai_decisions.log``)."""
    return logging.getLogger(DECISIONS_LOGGER_NAME)


def log_ai_decision(
    *,
    telegram_id: int,
    scenario: str,
    status: str,
    confidence: int,
    reason: str,
    awareness: str,
    readiness: str,
    urgency: str,
    next_action: str,
    needs_yulia: bool,
    response_sent: str,
) -> None:
    """Пишет решение AI в ``ai_decisions.log`` в обязательном формате из ТЗ (Блок 1).

    Формат зафиксирован требованием Юлии: любое решение системы должно быть
    разборчиво постфактум — почему статус, почему передача, почему такой ответ.
    """
    message = (
        f"[DECISION] telegram_id={telegram_id}\n"
        f"  scenario={scenario}\n"
        f"  status={status} confidence={confidence}\n"
        f'  reason="{reason}"\n'
        f"  awareness={awareness} readiness={readiness} urgency={urgency}\n"
        f'  next_action="{next_action}"\n'
        f"  needs_yulia={str(needs_yulia).lower()}\n"
        f'  response_sent="{response_sent}"'
    )
    get_decisions_logger().info(message)
