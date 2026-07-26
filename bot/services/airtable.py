"""Слой данных Airtable: CRUD всех таблиц, дедупликация, устойчивость к сбоям (Блок 3).

Airtable — единственное хранилище, локальной БД нет (ТЗ, Часть 3).

Правило дедупликации (КРИТИЧНО, ТЗ Часть 3): никогда не создавать запись
без предварительного поиска по ``telegram_id``. Если поиск не удался из-за
сбоя API — запись НЕ создаётся: лучше потерять одно обновление, чем получить
дубль клиента в CRM.

Устойчивость: retry 2 попытки (1 с, 3 с) на 429/5xx/сетевых ошибках;
при окончательной неудаче — лог ERROR и ``None``, исключение наверх не
бросается (ни одна ошибка не роняет бота). Rate limit Airtable 5 req/sec —
семафор, слот освобождается через секунду после захвата.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from bot.config import Config
from bot.utils.logger import get_app_logger

API_URL = "https://api.airtable.com/v0"
# Retry на 429 и 5xx: 2 попытки с задержкой 1 с, 3 с (ТЗ, Блок 3)
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Rate limit Airtable — 5 запросов в секунду (ТЗ, Часть 7)
RATE_LIMIT_PER_SEC = 5

logger = get_app_logger()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _quote(value: str) -> str:
    """Строка для filterByFormula: в двойных кавычках, кавычки экранированы."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class AirtableClient:
    """Асинхронный клиент Airtable REST API для пяти таблиц системы."""

    def __init__(
        self,
        api_key: str,
        base_id: str,
        *,
        contacts_table: str = "Contacts",
        touches_table: str = "Touches",
        comments_table: str = "Comments",
        posts_table: str = "Posts",
        tasks_table: str = "Tasks",
        timezone_name: str = "Europe/Moscow",
        timeout: float = 15.0,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        rate_limit_per_sec: int = RATE_LIMIT_PER_SEC,
        rate_window: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_id = base_id
        self.contacts = contacts_table
        self.touches = touches_table
        self.comments = comments_table
        self.posts = posts_table
        self.tasks = tasks_table
        self._tz = ZoneInfo(timezone_name)
        self._retry_delays = retry_delays
        self._rate_window = rate_window
        self._semaphore = asyncio.Semaphore(rate_limit_per_sec)
        # Пер-пользовательские замки upsert: пара «поиск → создание» не атомарна,
        # два одновременных апдейта одного человека (двойной тап по кнопке)
        # без замка создали бы дубль — нарушение правила «КРИТИЧНО» из ТЗ.
        self._contact_locks: dict[int, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(
            base_url=f"{API_URL}/{base_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def ping(self) -> float | None:
        """Доступность Airtable для /health: задержка в мс или ``None``."""
        import time

        started = time.monotonic()
        result = await self._request("GET", self.contacts, params={"maxRecords": 1})
        if result is None:
            return None
        return (time.monotonic() - started) * 1000

    # ── низкоуровневый запрос: rate limit + retry + логирование ──

    async def _request(
        self,
        method: str,
        table: str,
        path: str = "",
        *,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> dict | None:
        """Один запрос к API со всеми мерами устойчивости.

        Возвращает разобранный JSON или ``None`` при окончательной неудаче —
        исключения наверх не выходят.
        """
        url = f"/{table}{path}"
        attempts = len(self._retry_delays) + 1
        for attempt in range(1, attempts + 1):
            await self._semaphore.acquire()
            # Слот вернётся через rate_window секунд: не больше N запросов в окно
            asyncio.get_running_loop().call_later(self._rate_window, self._semaphore.release)
            try:
                response = await self._http.request(method, url, params=params, json=payload)
                if response.status_code < 400:
                    logger.info("Airtable %s %s — OK (попытка %d)", method, url, attempt)
                    return response.json()
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    delay = self._retry_delays[attempt - 1]
                    logger.warning(
                        "Airtable %s %s — HTTP %d, retry через %.0f с (попытка %d/%d)",
                        method,
                        url,
                        response.status_code,
                        delay,
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "Airtable %s %s — HTTP %d, ответ: %s",
                    method,
                    url,
                    response.status_code,
                    response.text[:500],
                )
                return None
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    delay = self._retry_delays[attempt - 1]
                    logger.warning(
                        "Airtable %s %s — сетевая ошибка %r, retry через %.0f с (попытка %d/%d)",
                        method,
                        url,
                        exc,
                        delay,
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.exception("Airtable %s %s — окончательная сетевая ошибка", method, url)
                return None
        return None  # недостижимо, для полноты

    async def _list_all(self, table: str, params: dict | None = None) -> list[dict] | None:
        """Все записи таблицы с постраничным обходом. ``None`` — при сбое."""
        records: list[dict] = []
        params = dict(params or {})
        while True:
            page = await self._request("GET", table, params=params)
            if page is None:
                return None
            records.extend(page.get("records", []))
            offset = page.get("offset")
            if not offset:
                return records
            params["offset"] = offset

    async def _find_one(self, table: str, formula: str) -> dict | None | bool:
        """Первая запись по формуле. ``False`` — сбой API (отличаем от «не найдено»)."""
        result = await self._request(
            "GET", table, params={"filterByFormula": formula, "maxRecords": 1}
        )
        if result is None:
            return False
        records = result.get("records", [])
        return records[0] if records else None

    async def _create(self, table: str, fields: dict) -> dict | None:
        result = await self._request("POST", table, payload={"fields": fields, "typecast": True})
        return result

    async def _update(self, table: str, record_id: str, fields: dict) -> dict | None:
        return await self._request(
            "PATCH", table, f"/{record_id}", payload={"fields": fields, "typecast": True}
        )

    # ── Contacts ──

    async def find_contact(self, telegram_id: int) -> dict | None:
        """Поиск контакта по ключу дедупликации. ``None`` — не найден или сбой."""
        record = await self._find_one(self.contacts, f"{{telegram_id}}={int(telegram_id)}")
        return record if isinstance(record, dict) else None

    async def find_contact_checked(self, telegram_id: int) -> tuple[bool, dict | None]:
        """Поиск с различением сбоя: ``(ok, запись)``.

        ``ok=False`` — Airtable не ответил, и «клиент не найден» утверждать
        нельзя: маршрутизация (передан Юлии / пауза / новый) должна вести
        себя осторожно, а не считать человека новым (ТЗ, Блок 5, шаг 3.1).
        """
        record = await self._find_one(self.contacts, f"{{telegram_id}}={int(telegram_id)}")
        if record is False:
            return False, None
        return True, record

    async def create_contact(self, data: dict) -> dict | None:
        """Создаёт контакт. Обязательные поля первого касания — по умолчанию."""
        now = _now_iso()
        fields = {
            "first_touch_date": now,
            "last_contact_date": now,
            # До квалификации человек в базе как холодный: статус уточнит AI (Блок 6)
            "status": "cold",
            "status_reason": "Первичное касание, квалификация не проводилась",
            "assigned_to": "ai",
            "paused": False,
            # Человек сам написал боту — согласие на коммуникацию (Конституция, п. 4)
            "consent": True,
            "touches_count": 1,
            "created_at": now,
            "updated_at": now,
            **data,
        }
        return await self._create(self.contacts, fields)

    async def update_contact(self, record_id: str, data: dict) -> dict | None:
        """Обновляет контакт; ``updated_at`` проставляется автоматически."""
        return await self._update(self.contacts, record_id, {**data, "updated_at": _now_iso()})

    # Поля «первого касания» («Карта клиентского пути», п. 2): фиксируются
    # один раз при создании и не перезаписываются повторными событиями —
    # source в Contacts это «Источник ПЕРВОГО касания».
    FIRST_TOUCH_FIELDS = ("source", "source_detail", "utm", "first_action", "first_touch_date")

    async def upsert_contact(self, telegram_id: int, data: dict) -> dict | None:
        """Дедупликация (ТЗ, Часть 3): поиск → обновление, иначе создание.

        При сбое поиска запись НЕ создаётся — иначе появится дубль.
        Пара «поиск → создание» атомарна в рамках процесса: пер-пользовательский
        замок защищает от гонки параллельных апдейтов одного человека.
        """
        lock = self._contact_locks.setdefault(int(telegram_id), asyncio.Lock())
        async with lock:
            return await self._upsert_contact_locked(telegram_id, data)

    async def _upsert_contact_locked(self, telegram_id: int, data: dict) -> dict | None:
        found = await self._find_one(self.contacts, f"{{telegram_id}}={int(telegram_id)}")
        if found is False:
            logger.error(
                "upsert_contact(%s): поиск не удался, создание пропущено во избежание дубля",
                telegram_id,
            )
            return None
        if found is None:
            return await self.create_contact({"telegram_id": int(telegram_id), **data})
        existing = found["fields"]
        updates = {
            k: v for k, v in data.items() if not (k in self.FIRST_TOUCH_FIELDS and existing.get(k))
        }
        updates["last_contact_date"] = _now_iso()
        updates["touches_count"] = int(existing.get("touches_count") or 0) + 1
        return await self.update_contact(found["id"], updates)

    async def get_contacts_by_status(self, status: str) -> list[dict] | None:
        return await self._list_all(
            self.contacts, {"filterByFormula": f"{{status}}={_quote(status)}"}
        )

    async def get_contacts_assigned_to(self, assignee: str) -> list[dict] | None:
        return await self._list_all(
            self.contacts, {"filterByFormula": f"{{assigned_to}}={_quote(assignee)}"}
        )

    async def get_stale_contacts(self, hours: int) -> list[dict] | None:
        """Контакты на AI без паузы, молчащие дольше ``hours`` часов (Блок 6)."""
        formula = (
            'AND({assigned_to}="ai", NOT({paused}), '
            f"DATETIME_DIFF(NOW(), {{last_contact_date}}, 'hours') >= {int(hours)})"
        )
        return await self._list_all(self.contacts, {"filterByFormula": formula})

    async def add_status_change(
        self, record_id: str, old: str, new: str, reason: str, by: str
    ) -> dict | None:
        """Смена статуса с накоплением истории: старые записи не затираются
        («Карта клиентского пути», п. 18: все изменения сохраняются)."""
        current = await self._request("GET", self.contacts, f"/{record_id}")
        if current is None:
            return None
        history_raw = current.get("fields", {}).get("status_history") or "[]"
        try:
            history = json.loads(history_raw)
            if not isinstance(history, list):
                raise ValueError("status_history: ожидался JSON-список")
        except (json.JSONDecodeError, ValueError):
            # Битую историю сохраняем как первый элемент, а не затираем
            logger.warning("status_history %s повреждена, оборачиваю как есть", record_id)
            history = [{"raw": history_raw}]
        history.append({"date": _now_iso(), "from": old, "to": new, "reason": reason, "by": by})
        return await self.update_contact(
            record_id,
            {
                "status": new,
                "status_reason": reason,
                "status_history": json.dumps(history, ensure_ascii=False),
            },
        )

    # ── Touches ──

    async def add_touch(
        self, telegram_id: int, type: str, description: str, **kwargs
    ) -> dict | None:
        """Касание. История касаний никогда не удаляется (ТЗ, Часть 3)."""
        now = _now_iso()
        fields = {
            "contact_telegram_id": int(telegram_id),
            "date": now,
            "type": type,
            "description": description,
            "created_at": now,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        return await self._create(self.touches, fields)

    async def get_touches(self, telegram_id: int) -> list[dict] | None:
        return await self._list_all(
            self.touches,
            {
                "filterByFormula": f"{{contact_telegram_id}}={int(telegram_id)}",
                "sort[0][field]": "date",
                "sort[0][direction]": "asc",
            },
        )

    async def build_timeline(self, telegram_id: int) -> str:
        """Читаемая хронология касаний для карточки клиента (Блок 7).

        Формат из ТЗ: ``15.07 — оставил комментарий``.
        """
        touches = await self.get_touches(telegram_id)
        if touches is None:
            return "История касаний недоступна (ошибка Airtable)."
        if not touches:
            return "Касаний пока нет."
        lines = []
        for touch in touches:
            fields = touch.get("fields", {})
            date_raw = fields.get("date") or fields.get("created_at") or ""
            try:
                moment = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                day = moment.astimezone(self._tz).strftime("%d.%m")
            except ValueError:
                day = "??.??"
            lines.append(f"{day} — {fields.get('description', fields.get('type', '—'))}")
        return "\n".join(lines)

    # ── Comments ──

    async def create_comment(self, data: dict) -> dict | None:
        return await self._create(self.comments, {"created_at": _now_iso(), **data})

    async def update_comment(self, record_id: str, data: dict) -> dict | None:
        return await self._update(self.comments, record_id, data)

    async def get_comments_by_author(self, telegram_id: int) -> list[dict] | None:
        return await self._list_all(
            self.comments,
            {"filterByFormula": f"{{author_telegram_id}}={int(telegram_id)}"},
        )

    async def get_pending_comments(self) -> list[dict] | None:
        return await self._list_all(self.comments, {"filterByFormula": '{reply_status}="pending"'})

    # ── Posts ──

    async def upsert_post(self, post_id: str, data: dict) -> dict | None:
        """Пост канала: поиск по ``post_id`` → обновление, иначе создание."""
        found = await self._find_one(self.posts, f"{{post_id}}={_quote(post_id)}")
        if found is False:
            logger.error("upsert_post(%s): поиск не удался, пропускаю", post_id)
            return None
        if found is None:
            return await self._create(
                self.posts,
                {"post_id": post_id, "created_at": _now_iso(), "comments_count": 0, **data},
            )
        # Не затираем уже заполненные поля пустыми значениями
        updates = {k: v for k, v in data.items() if v not in (None, "")}
        if not updates:
            return found
        return await self._update(self.posts, found["id"], updates)

    async def increment_post_counter(self, post_id: str, field: str) -> dict | None:
        found = await self._find_one(self.posts, f"{{post_id}}={_quote(post_id)}")
        if found is False:
            return None
        if found is None:
            # Пост ещё не заведён — создаём со счётчиком 1, данные догонит upsert_post
            return await self._create(
                self.posts, {"post_id": post_id, field: 1, "created_at": _now_iso()}
            )
        value = int(found["fields"].get(field) or 0) + 1
        return await self._update(self.posts, found["id"], {field: value})

    # ── Tasks ──

    async def create_task(
        self, action: str, assignee: str, due_date: str, reason: str, **kwargs
    ) -> dict | None:
        fields = {
            "action": action,
            "assignee": assignee,
            "due_date": due_date,
            "reason": reason,
            "status": "open",
            "created_by": kwargs.pop("created_by", "ai"),
            "created_at": _now_iso(),
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        return await self._create(self.tasks, fields)

    async def get_open_tasks(self, assignee: str | None = None) -> list[dict] | None:
        formula = '{status}="open"'
        if assignee:
            formula = f"AND({formula}, {{assignee}}={_quote(assignee)})"
        return await self._list_all(self.tasks, {"filterByFormula": formula})

    async def complete_task(self, record_id: str) -> dict | None:
        return await self._update(
            self.tasks, record_id, {"status": "done", "completed_at": _now_iso()}
        )

    # ── Аналитика и экспорт ──

    async def get_report_data(self, date_from: str, date_to: str) -> dict | None:
        """Сырые данные для еженедельного отчёта (Блок 9).

        Периоды — ISO-даты включительно. Метрики, которых система не собирает,
        не выдумываются (ТЗ, Блок 9) — возвращаем только фактические записи.
        """

        def in_range(field: str) -> str:
            return (
                f"AND(IS_AFTER({{{field}}}, DATEADD({_quote(date_from)}, -1, 'days')), "
                f"IS_BEFORE({{{field}}}, DATEADD({_quote(date_to)}, 1, 'days')))"
            )

        new_contacts = await self._list_all(
            self.contacts, {"filterByFormula": in_range("created_at")}
        )
        handoffs = await self._list_all(
            self.contacts, {"filterByFormula": in_range("handoff_date")}
        )
        comments = await self._list_all(self.comments, {"filterByFormula": in_range("created_at")})
        touches = await self._list_all(self.touches, {"filterByFormula": in_range("created_at")})
        all_contacts = await self._list_all(self.contacts)
        open_tasks = await self.get_open_tasks()
        pending_comments = await self.get_pending_comments()
        if None in (
            new_contacts,
            handoffs,
            comments,
            touches,
            all_contacts,
            open_tasks,
            pending_comments,
        ):
            logger.error("get_report_data: часть данных недоступна, отчёт не собран")
            return None
        return {
            "date_from": date_from,
            "date_to": date_to,
            "new_contacts": new_contacts,
            "handoffs": handoffs,
            "comments": comments,
            "touches": touches,
            "all_contacts": all_contacts,
            "open_tasks": open_tasks,
            "pending_comments": pending_comments,
        }

    async def export_table_to_csv(self, table_name: str) -> str | None:
        """Экспорт таблицы в CSV-строку (бэкап, Блок 10). ``None`` — при сбое."""
        records = await self._list_all(table_name)
        if records is None:
            return None
        field_names: list[str] = []
        for record in records:
            for name in record.get("fields", {}):
                if name not in field_names:
                    field_names.append(name)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["record_id", "createdTime", *field_names])
        for record in records:
            fields = record.get("fields", {})
            row = [record.get("id", ""), record.get("createdTime", "")]
            for name in field_names:
                value = fields.get(name, "")
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                row.append(value)
            writer.writerow(row)
        logger.info("Экспорт %s: %d записей", table_name, len(records))
        return buffer.getvalue()


# ── Модульный интерфейс (сигнатуры из ТЗ, Блок 3) ──

_client: AirtableClient | None = None


def init_airtable(config: Config) -> AirtableClient:
    """Создаёт клиент из конфига. Вызывается на старте бота."""
    global _client
    _client = AirtableClient(
        api_key=config.airtable_api_key,
        base_id=config.airtable_base_id,
        contacts_table=config.airtable_contacts_table,
        touches_table=config.airtable_touches_table,
        comments_table=config.airtable_comments_table,
        posts_table=config.airtable_posts_table,
        tasks_table=config.airtable_tasks_table,
        timezone_name=config.timezone,
    )
    return _client


def get_client() -> AirtableClient:
    if _client is None:
        raise RuntimeError("Airtable-клиент не инициализирован: вызовите init_airtable(config)")
    return _client


async def find_contact(telegram_id: int) -> dict | None:
    return await get_client().find_contact(telegram_id)


async def find_contact_checked(telegram_id: int) -> tuple[bool, dict | None]:
    return await get_client().find_contact_checked(telegram_id)


async def create_contact(data: dict) -> dict | None:
    return await get_client().create_contact(data)


async def update_contact(record_id: str, data: dict) -> dict | None:
    return await get_client().update_contact(record_id, data)


async def upsert_contact(telegram_id: int, data: dict) -> dict | None:
    return await get_client().upsert_contact(telegram_id, data)


async def get_contacts_by_status(status: str) -> list[dict] | None:
    return await get_client().get_contacts_by_status(status)


async def get_contacts_assigned_to(assignee: str) -> list[dict] | None:
    return await get_client().get_contacts_assigned_to(assignee)


async def get_stale_contacts(hours: int) -> list[dict] | None:
    return await get_client().get_stale_contacts(hours)


async def add_status_change(
    record_id: str, old: str, new: str, reason: str, by: str
) -> dict | None:
    return await get_client().add_status_change(record_id, old, new, reason, by)


async def add_touch(telegram_id: int, type: str, description: str, **kwargs) -> dict | None:
    return await get_client().add_touch(telegram_id, type, description, **kwargs)


async def get_touches(telegram_id: int) -> list[dict] | None:
    return await get_client().get_touches(telegram_id)


async def build_timeline(telegram_id: int) -> str:
    return await get_client().build_timeline(telegram_id)


async def create_comment(data: dict) -> dict | None:
    return await get_client().create_comment(data)


async def update_comment(record_id: str, data: dict) -> dict | None:
    return await get_client().update_comment(record_id, data)


async def get_comments_by_author(telegram_id: int) -> list[dict] | None:
    return await get_client().get_comments_by_author(telegram_id)


async def get_pending_comments() -> list[dict] | None:
    return await get_client().get_pending_comments()


async def upsert_post(post_id: str, data: dict) -> dict | None:
    return await get_client().upsert_post(post_id, data)


async def increment_post_counter(post_id: str, field: str) -> dict | None:
    return await get_client().increment_post_counter(post_id, field)


async def create_task(
    action: str, assignee: str, due_date: str, reason: str, **kwargs
) -> dict | None:
    return await get_client().create_task(action, assignee, due_date, reason, **kwargs)


async def get_open_tasks(assignee: str | None = None) -> list[dict] | None:
    return await get_client().get_open_tasks(assignee)


async def complete_task(record_id: str) -> dict | None:
    return await get_client().complete_task(record_id)


async def get_report_data(date_from: str, date_to: str) -> dict | None:
    return await get_client().get_report_data(date_from, date_to)


async def export_table_to_csv(table_name: str) -> str | None:
    return await get_client().export_table_to_csv(table_name)
