"""Точка входа: инициализация бота, webhook-сервер, graceful shutdown.

Запуск: ``python -m bot.main`` из корня проекта.
"""

from __future__ import annotations

import hashlib

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from bot.config import Config, load_config
from bot.handlers import admin, callbacks, comments, qualification, start
from bot.middlewares.pause_check import PauseCheckMiddleware
from bot.services.ai import init_ai
from bot.services.airtable import init_airtable
from bot.services.knowledge import load_knowledge
from bot.utils.logger import get_app_logger, setup_logging

logger = get_app_logger()


def create_dispatcher(config: Config | None = None) -> Dispatcher:
    """Создаёт Dispatcher, регистрирует роутеры и middleware.

    Порядок роутеров важен: admin-команды раньше квалификации, чтобы
    команды Юлии не попадали в FSM диалога. ``pause_check`` — outer
    middleware, выполняется до всех хендлеров (ТЗ, Блок 7).
    """
    # events_isolation сериализует обработку апдейтов одного пользователя:
    # двойной тап по кнопке / два быстрых /start не выполняются параллельно
    # (вторая линия защиты от дублей вместе с замком в upsert_contact)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    if config is not None:
        dispatcher.message.outer_middleware(PauseCheckMiddleware(config))
    dispatcher.include_router(comments.router)
    dispatcher.include_router(start.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(callbacks.router)
    dispatcher.include_router(qualification.router)
    return dispatcher


def webhook_secret_for(config: Config) -> str:
    """Секрет вебхука: из конфига или детерминированно из токена бота.

    Telegram присылает его в заголовке X-Telegram-Bot-Api-Secret-Token
    каждого апдейта — POST на /webhook от посторонних (без секрета)
    отбрасываются aiogram'ом. Иначе любой, узнавший URL, мог бы слать
    поддельные апдейты, включая «команды Юлии».
    """
    if config.webhook_secret:
        return config.webhook_secret
    return hashlib.sha256(config.telegram_bot_token.encode()).hexdigest()[:32]


async def on_startup(bot: Bot, config: Config) -> None:
    """Устанавливает webhook. Без вебхука бот не получает сообщений —
    при неудаче падаем, systemd перезапустит через 10 секунд."""
    try:
        await bot.set_webhook(
            config.webhook_full_url,
            drop_pending_updates=False,
            secret_token=webhook_secret_for(config),
        )
        logger.info("Webhook установлен: %s", config.webhook_full_url)
    except Exception:
        logger.exception("Не удалось установить webhook — останавливаемся")
        raise


async def on_shutdown(bot: Bot) -> None:
    """Останавливает бота. Webhook намеренно не снимаем: Telegram хранит
    недоставленные апдейты и дошлёт их после рестарта — сообщения клиентов
    не теряются при перезапуске."""
    from bot.services import ai as ai_service
    from bot.services import airtable as airtable_service

    for closer in (ai_service._service, airtable_service._client):
        if closer is not None:
            try:
                await closer.close()
            except Exception:
                logger.exception("Ошибка закрытия HTTP-клиента при остановке")
    await bot.session.close()
    logger.info("Бот остановлен")


def build_app(config: Config, bot: Bot) -> web.Application:
    """Собирает боевое приложение: сервисы, диспетчер, webhook-сервер.

    Вынесено из ``main()`` без изменений логики, чтобы e2e-тест поднимал
    ровно ту же сборку (с подменённым Telegram API-сервером).
    """
    # База знаний читается на старте: состав и оценка токенов уходят в лог (ТЗ, Блок 2),
    # дальше AI-сервис работает с кэшем через get_knowledge().
    load_knowledge(config.knowledge_dir)
    init_airtable(config)
    init_ai(config)

    dispatcher = create_dispatcher(config)
    dispatcher["config"] = config
    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dispatcher, bot=bot, secret_token=webhook_secret_for(config)
    ).register(app, path=config.webhook_path)
    setup_application(app, dispatcher, bot=bot)
    return app


def main() -> None:
    """Точка входа: конфиг, логирование, приложение, webhook-сервер."""
    config = load_config()
    setup_logging(config.log_dir, config.log_level)
    logger.info("Запуск бота (host=%s, port=%s)...", config.webapp_host, config.webapp_port)
    # Без parse_mode: во всех текстах — обычный текст, а HTML-режим молча
    # ронял бы доставку карточек с «<» в цитатах клиентов (TelegramBadRequest)
    bot = Bot(token=config.telegram_bot_token)
    app = build_app(config, bot)
    web.run_app(app, host=config.webapp_host, port=config.webapp_port)


if __name__ == "__main__":
    main()
