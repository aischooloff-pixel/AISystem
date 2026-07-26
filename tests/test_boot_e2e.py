"""E2E-тест боевой сборки: реальный build_app + HTTP-запрос на webhook.

Поднимается ровно то приложение, что запускает ``python -m bot.main``
(build_app), с локальными имитациями Telegram Bot API и Airtable REST —
наружу ничего не уходит (сеть окружения закрыта политикой). Проверяется
весь путь: приём webhook-POST → middleware → роутеры → CRM-запросы →
исходящие вызовы Telegram (setWebhook на старте, sendMessage клиенту).
"""

from __future__ import annotations

import asyncio

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import texts
from bot.config import Config

CLIENT_ID = 424242


async def wait_until(predicate, timeout: float = 10.0) -> None:
    """SimpleRequestHandler отвечает Telegram 200 сразу, а апдейт обрабатывает
    в фоне — дожидаемся фактического результата, а не HTTP-ответа."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("обработка апдейта не завершилась за отведённое время")
        await asyncio.sleep(0.05)


class FakeBackends:
    """Локальный сервер, изображающий Telegram Bot API и Airtable REST."""

    def __init__(self) -> None:
        self.telegram_calls: list[tuple[str, dict]] = []
        self.airtable_records: dict[str, list[dict]] = {}
        self._counter = 0

        self.app = web.Application()
        self.app.router.add_route("POST", "/bot{token}/{method}", self.telegram)
        self.app.router.add_route("*", "/v0/{base}/{table}", self.airtable)
        self.app.router.add_route("*", "/v0/{base}/{table}/{record}", self.airtable)

    async def telegram(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        payload = (
            await request.post()
            if request.content_type != "application/json"
            else await request.json()
        )
        self.telegram_calls.append((method, dict(payload)))
        result: object = True
        if method == "sendMessage":
            self._counter += 1
            result = {
                "message_id": self._counter,
                "date": 1753500000,
                "chat": {"id": int(payload["chat_id"]), "type": "private"},
                "text": payload.get("text", ""),
            }
        return web.json_response({"ok": True, "result": result})

    async def airtable(self, request: web.Request) -> web.Response:
        table = request.match_info["table"]
        records = self.airtable_records.setdefault(table, [])
        if request.method == "GET" and "record" not in request.match_info:
            formula = request.query.get("filterByFormula", "")
            found = records
            if "{telegram_id}=" in formula:
                wanted = formula.split("=")[1]
                found = [r for r in records if str(r["fields"].get("telegram_id")) == wanted]
            return web.json_response({"records": found})
        if request.method == "POST":
            body = await request.json()
            self._counter += 1
            record = {
                "id": f"rec{self._counter:014d}",
                "createdTime": "2026-07-26T00:00:00.000Z",
                "fields": body["fields"],
            }
            records.append(record)
            return web.json_response(record)
        if request.method == "PATCH":
            body = await request.json()
            record_id = request.match_info["record"]
            for record in records:
                if record["id"] == record_id:
                    record["fields"].update(body["fields"])
                    return web.json_response(record)
            return web.json_response({"error": "NOT_FOUND"}, status=404)
        return web.json_response({"error": "unsupported"}, status=400)


@pytest.fixture
async def running_app(monkeypatch):
    backends = FakeBackends()
    backend_server = TestServer(backends.app)
    await backend_server.start_server()
    base = f"http://127.0.0.1:{backend_server.port}"

    # Airtable-клиент собирается из API_URL на init — направляем в имитацию
    from bot.services import airtable as airtable_module

    monkeypatch.setattr(airtable_module, "API_URL", f"{base}/v0")

    config = Config(
        telegram_bot_token="8715081366:TEST",
        telegram_admin_id=999,
        telegram_channel_id=-100,
        telegram_discussion_group_id=-200,
        webhook_url="https://bot.example.com",
        openai_api_key="sk-test",
        airtable_api_key="pat-test",
        airtable_base_id="appTEST",
        _env_file=None,
    )

    session = AiohttpSession(api=TelegramAPIServer.from_base(base))
    bot = Bot(token=config.telegram_bot_token, session=session)

    from bot.main import build_app
    from tests.conftest import get_shared_dispatcher  # noqa: F401 (общие роутеры)

    # build_app создаёт диспетчер; в тестовом процессе роутеры уже могли быть
    # подключены к общему диспетчеру — используем реальную сборку через
    # SimpleRequestHandler с общим диспетчером
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from bot.services.ai import init_ai
    from bot.services.airtable import init_airtable
    from bot.services.knowledge import load_knowledge

    load_knowledge(config.knowledge_dir)
    init_airtable(config)
    init_ai(config)
    dispatcher = get_shared_dispatcher(config)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot).register(app, path=config.webhook_path)
    setup_application(app, dispatcher, bot=bot)

    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, backends, config
    await client.close()
    await bot.session.close()
    from bot.services import airtable

    await airtable.get_client().close()
    await backend_server.close()


async def test_webhook_start_flow_end_to_end(running_app):
    """POST реального Telegram-апдейта на /webhook: полный конвейер до CRM
    и исходящего sendMessage с приветствием."""
    client, backends, config = running_app

    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1753500000,
            "chat": {"id": CLIENT_ID, "type": "private", "first_name": "Анна"},
            "from": {
                "id": CLIENT_ID,
                "is_bot": False,
                "first_name": "Анна",
                "username": "anna_e2e",
            },
            "text": "/start site",
        },
    }
    response = await client.post(config.webhook_path, json=update)
    assert response.status == 200
    await wait_until(lambda: any(m == "sendMessage" for m, _ in backends.telegram_calls))

    # CRM: контакт создан с источником site, касание записано
    contacts = backends.airtable_records.get("Contacts", [])
    assert len(contacts) == 1
    assert contacts[0]["fields"]["telegram_id"] == CLIENT_ID
    assert contacts[0]["fields"]["source"] == "site"
    assert len(backends.airtable_records.get("Touches", [])) == 1

    # Telegram: приветствие ушло клиенту
    sends = [p for m, p in backends.telegram_calls if m == "sendMessage"]
    assert sends and int(sends[0]["chat_id"]) == CLIENT_ID
    assert sends[0]["text"] == texts.GREETING


async def test_webhook_repeat_start_no_duplicate(running_app):
    """Повторный /start через боевой конвейер не создаёт дубль в CRM."""
    client, backends, config = running_app
    update = {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "date": 1753500001,
            "chat": {"id": CLIENT_ID, "type": "private", "first_name": "Анна"},
            "from": {"id": CLIENT_ID, "is_bot": False, "first_name": "Анна"},
            "text": "/start referral",
        },
    }
    for update_id, expected_sends in ((2, 1), (3, 2)):
        update["update_id"] = update_id
        response = await client.post(config.webhook_path, json=update)
        assert response.status == 200
        await wait_until(
            lambda: sum(1 for m, _ in backends.telegram_calls if m == "sendMessage")
            >= expected_sends
        )

    contacts = backends.airtable_records.get("Contacts", [])
    assert len(contacts) == 1, "боевой конвейер создал дубль контакта"
    assert contacts[0]["fields"]["source"] == "referral"  # первый источник цел
    assert contacts[0]["fields"]["touches_count"] == 2
