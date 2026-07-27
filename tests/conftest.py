"""Общая инфраструктура тестов: имитация Airtable REST API.

FakeAirtable хранит записи в памяти и понимает ровно те запросы, которые
делает AirtableClient: постраничный список с filterByFormula из простых
равенств, чтение по id, POST и PATCH. Очередь ``fail_with`` позволяет
имитировать 429/5xx и сетевые ошибки для проверки retry (критерий Блока 3).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from itertools import count

import httpx
import pytest

from bot.services.airtable import AirtableClient
from bot.utils.logger import APP_LOGGER_NAME


@pytest.fixture
def app_caplog(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """caplog, который действительно видит логгер ``app``.

    ``setup_logging()`` ставит ``app.propagate = False`` — иначе записи двоились
    бы через root. Но caplog слушает именно root, поэтому после любого теста,
    поднявшего логирование, обычный caplog для ``app`` молча пуст, и проверка
    логов даёт ложно-зелёный результат в зависимости от порядка тестов.
    Вешаем обработчик caplog прямо на логгер ``app``.
    """
    logger = logging.getLogger(APP_LOGGER_NAME)
    previous_propagate = logger.propagate
    # Обработчик caplog вешаем прямо на логгер, а propagate гасим: иначе, пока
    # setup_logging() ещё не вызывался и propagate=True, запись пришла бы и
    # напрямую, и через root — и каждая строка посчиталась бы дважды.
    logger.propagate = False
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate = previous_propagate


# {field}=123 · {field}="строка" · TRUE()/FALSE() не используются в простых равенствах
_EQ_RE = re.compile(r'\{(\w+)\}=("(?:[^"\\]|\\.)*"|-?\d+)')
_UNSUPPORTED = ("DATETIME_DIFF", "NOT(", "IS_AFTER", "IS_BEFORE")


class FakeAirtable:
    """Мини-эмулятор Airtable для httpx.MockTransport."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}
        self.fail_with: list[int | Exception] = []  # очередь сбоев перед успехом
        self.create_calls = 0
        self.request_times: list[float] = []
        self._ids = count(1)

    def seed(self, table: str, fields: dict) -> dict:
        """Заводит запись напрямую, минуя API (подготовка данных теста)."""
        record = {
            "id": f"rec{next(self._ids):014d}",
            "createdTime": "2026-07-26T00:00:00.000Z",
            "fields": dict(fields),
        }
        self.tables.setdefault(table, []).append(record)
        return record

    # ── обработчик httpx.MockTransport ──

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request_times.append(time.monotonic())
        if self.fail_with:
            failure = self.fail_with.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, json={"error": "simulated"})

        # /v0/{base_id}/{table}[/{record_id}]
        parts = request.url.path.strip("/").split("/")
        table, record_id = parts[2], (parts[3] if len(parts) > 3 else None)
        records = self.tables.setdefault(table, [])

        if request.method == "GET" and record_id:
            for record in records:
                if record["id"] == record_id:
                    return httpx.Response(200, json=record)
            return httpx.Response(404, json={"error": "NOT_FOUND"})

        if request.method == "GET":
            result = self._filtered(records, request.url.params)
            return httpx.Response(200, json={"records": result})

        if request.method == "POST":
            import json as _json

            self.create_calls += 1
            fields = _json.loads(request.content)["fields"]
            record = self.seed(table, fields)
            return httpx.Response(200, json=record)

        if request.method == "PATCH" and record_id:
            import json as _json

            fields = _json.loads(request.content)["fields"]
            for record in records:
                if record["id"] == record_id:
                    record["fields"].update(fields)
                    return httpx.Response(200, json=record)
            return httpx.Response(404, json={"error": "NOT_FOUND"})

        return httpx.Response(400, json={"error": f"unsupported {request.method}"})

    def _filtered(self, records: list[dict], params) -> list[dict]:
        formula = params.get("filterByFormula", "")
        result = records
        if formula and not any(marker in formula for marker in _UNSUPPORTED):
            conditions = []
            for field, raw in _EQ_RE.findall(formula):
                if raw.startswith('"'):
                    value: object = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                else:
                    value = int(raw)
                conditions.append((field, value))
            if conditions:
                result = [r for r in records if all(r["fields"].get(f) == v for f, v in conditions)]
        sort_field = params.get("sort[0][field]")
        if sort_field:
            reverse = params.get("sort[0][direction]") == "desc"
            result = sorted(
                result, key=lambda r: str(r["fields"].get(sort_field, "")), reverse=reverse
            )
        max_records = params.get("maxRecords")
        if max_records:
            result = result[: int(max_records)]
        return result


@pytest.fixture
def fake() -> FakeAirtable:
    return FakeAirtable()


# Роутеры aiogram — модульные синглтоны: create_dispatcher можно вызвать
# только один раз за процесс (как и в проде). Единый диспетчер для всех
# тестов, которым нужна полная сборка.
_shared_dispatcher = None


def get_shared_dispatcher(config):
    global _shared_dispatcher
    if _shared_dispatcher is None:
        from bot.main import create_dispatcher

        _shared_dispatcher = create_dispatcher(config)
        _shared_dispatcher["config"] = config
    return _shared_dispatcher


@pytest.fixture
async def client(fake: FakeAirtable):
    """AirtableClient поверх FakeAirtable: без задержек retry, без rate limit."""
    airtable = AirtableClient(
        api_key="pat-test",
        base_id="appTEST00000000",
        retry_delays=(0, 0),
        rate_window=0.001,
        transport=httpx.MockTransport(fake.handler),
    )
    yield airtable
    await airtable.close()
