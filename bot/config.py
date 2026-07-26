"""Конфигурация приложения.

Все настройки читаются из переменных окружения (файл ``.env``).
Секреты не имеют значений по умолчанию: при отсутствии обязательной переменной
приложение падает на старте с перечнем недостающих имён (ТЗ, Блок 1),
а не в рантайме посреди диалога с клиентом.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Настройки бота. Имена полей соответствуют переменным окружения (ТЗ, Часть 5)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Telegram ──
    telegram_bot_token: str
    telegram_admin_id: int  # Telegram ID Юлии — сюда идут карточки и уведомления
    telegram_channel_id: int  # ID канала
    telegram_discussion_group_id: int  # ID linked group, куда падают комментарии
    webhook_url: str  # публичный базовый URL сервера, без пути
    webhook_path: str = "/webhook"
    # Секрет вебхука: Telegram шлёт его в заголовке каждого апдейта, чужие
    # POST на /webhook отбрасываются. Пусто — выводится из токена бота.
    webhook_secret: str | None = None
    webapp_host: str = "0.0.0.0"
    webapp_port: int = 8080

    # ── OpenAI ──
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"  # модель заменяема через конфиг (ТЗ, Часть 2)
    openai_timeout: int = 30
    openai_max_retries: int = 2

    # ── Airtable ──
    airtable_api_key: str
    airtable_base_id: str
    airtable_contacts_table: str = "Contacts"
    airtable_touches_table: str = "Touches"
    airtable_comments_table: str = "Comments"
    airtable_posts_table: str = "Posts"
    airtable_tasks_table: str = "Tasks"

    # ── Логика ──
    # Порог 85% из документа «Критерии квалификации», п. 9 —
    # ниже него решение принимает Юлия, а не AI.
    ai_confidence_threshold: int = 85
    timeout_reminder_hours: int = 24  # Блок 6: одно мягкое напоминание
    timeout_cold_hours: int = 72  # Блок 6: перевод в cold, result=no_response
    nurturing_review_days: int = 7  # Блок 6: задача «Решить по {имя}: прогрев или закрытие»

    # ── Прочее ──
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    knowledge_dir: Path = Path("bot/knowledge")
    timezone: str = "Europe/Moscow"

    @property
    def webhook_full_url(self) -> str:
        """Полный URL вебхука: публичный базовый URL + путь."""
        return f"{self.webhook_url.rstrip('/')}{self.webhook_path}"


def load_config(env_file: str | None = ".env") -> Config:
    """Загружает конфигурацию из окружения и ``env_file``.

    При отсутствии или некорректности обязательных переменных завершает процесс
    с кодом 1 и человекочитаемым перечнем проблем.
    """
    try:
        return Config(_env_file=env_file)
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            var = str(err["loc"][0]).upper()
            if err["type"] == "missing":
                problems.append(f"  {var} — не задана")
            else:
                problems.append(f"  {var} — некорректное значение ({err['msg']})")
        print(
            "ОШИБКА КОНФИГУРАЦИИ — бот не запущен.\n"
            "Проблемы с переменными окружения:\n"
            + "\n".join(problems)
            + "\nЗаполните .env по образцу .env.example и перезапустите бота.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
