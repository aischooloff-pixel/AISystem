"""Живые диалоги от лица клиента — прогон боевой логики на настоящей модели.

Отличие от tests/: там OpenAI подменён, и проверяется код. Здесь модель
настоящая, и проверяется поведение — то, что увидит человек в Telegram.
Telegram и Airtable подменены, чтобы прогон не трогал ни клиентов, ни CRM.

Запуск:  python -m scripts.live_journeys
         python -m scripts.live_journeys "тёплый клиент"   # один сценарий
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from itertools import count

import httpx
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update, User

from bot import texts
from bot.config import load_config
from bot.main import create_dispatcher
from bot.services import ai as ai_module
from bot.services import airtable as airtable_module
from bot.services.ai import AIService
from bot.services.airtable import AirtableClient
from bot.services.knowledge import load_knowledge
from tests.conftest import FakeAirtable

ADMIN_ID = 999
_ids = count(770_001)
_dispatcher = None


def get_dispatcher(config):
    """Диспетчер один на процесс, хранилище FSM — своё на каждый прогон.

    Роутеры aiogram — модульные синглтоны, второй ``create_dispatcher``
    падает с «Router is already attached». А состояние между диалогами
    течь не должно, иначе следующий клиент продолжит чужой разговор.
    """
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = create_dispatcher(config)
    _dispatcher["config"] = config
    _dispatcher.fsm.storage = MemoryStorage()
    return _dispatcher


class Outbox(BaseSession):
    """Telegram наружу не уходит: всё исходящее складывается сюда."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict] = []
        self._mid = count(1)

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            self.messages.append({"chat_id": int(method.chat_id), "text": method.text})
            return Message(
                message_id=next(self._mid),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup, AnswerCallbackQuery)):
            return True
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def close(self) -> None:
        pass


class Client:
    """Один человек в Telegram: пишет боту и читает ответы."""

    def __init__(self, dispatcher, bot: Bot, outbox: Outbox, name: str) -> None:
        self.dp, self.bot, self.outbox = dispatcher, bot, outbox
        self.id = next(_ids)
        self.user = User(id=self.id, is_bot=False, first_name=name, username="probe")
        self._updates = count(1)
        self._read = 0

    async def says(self, text: str) -> list[str]:
        message = Message(
            message_id=next(self._updates),
            date=datetime.now(timezone.utc),
            chat=Chat(id=self.id, type="private", first_name="Проба"),
            from_user=self.user,
            text=text,
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=next(self._updates), message=message)
        )
        mine = [m["text"] for m in self.outbox.messages if m["chat_id"] == self.id]
        fresh, self._read = mine[self._read :], len(mine)
        return fresh

    @property
    def yulia_got(self) -> list[str]:
        return [m["text"] for m in self.outbox.messages if m["chat_id"] == ADMIN_ID]


# ── Сценарии: что пишет человек и что должно получиться ──

JOURNEYS: dict[str, dict] = {
    "тёплый клиент": {
        "replies": [
            "/start site",
            "проблемы в отношениях",
            "больше года",
            "постоянные упрёки и молчание",
            "ходили к семейному психологу, не помогло",
            "хочу перестать это повторять",
        ],
        "expect": "4+ вопроса, статус warm, БЕЗ передачи Юлии",
        "handoff": False,
    },
    "холодный клиент": {
        "replies": ["/start site", "вроде интересно", "давно", "не знаю", "ничего", "хз"],
        "expect": "прогрев, БЕЗ передачи Юлии",
        "handoff": False,
    },
    "горячий: хочет записаться": {
        "replies": ["/start site", "хочу записаться на диагностику", "конфликты в команде", "выстроить управление"],
        "expect": "ответ КАК записаться, затем передача Юлии",
        "handoff": True,
    },
    "вопрос о цене": {
        "replies": ["/start site", "сколько стоит диагностика"],
        "expect": "названа цена 15 000 ₽",
        "handoff": None,
    },
    "цена посреди квалификации": {
        "replies": ["/start site", "проблемы в бизнесе", "цена консультации?"],
        "expect": "названа цена, вопрос повторён, БЕЗ передачи",
        "handoff": False,
    },
    "нецелевой: таро": {
        "replies": ["/start site", "хочу расклад на таро"],
        "expect": "вежливое завершение, БЕЗ передачи Юлии",
        "handoff": False,
    },
    "нецелевой, потом целевой": {
        "replies": ["/start site", "хочу расклад на таро", "хорошо, тогда хочу помощь в бизнесе", "год"],
        "expect": "новый разговор, вопрос ПО СУЩЕСТВУ (не про сроки сразу)",
        "handoff": False,
    },
    "просит живого человека": {
        "replies": ["/start site", "не хочу с ботом, свяжите с Юлией"],
        "expect": "немедленная передача, без вопросов",
        "handoff": True,
    },
    "острое состояние": {
        "replies": ["/start site", "мне очень плохо, не вижу выхода"],
        "expect": "немедленная передача Юлии",
        "handoff": True,
    },
    "информационный вопрос": {
        "replies": ["/start site", "что такое системная диагностика?"],
        "expect": "ответ по базе знаний, БЕЗ квалификации",
        "handoff": False,
    },
}


async def run(name: str, plan: dict, config) -> bool:
    outbox = Outbox()
    bot = Bot(token="42:PROBE", session=outbox)
    crm = FakeAirtable()
    airtable_module._client = AirtableClient(
        api_key="pat-probe",
        base_id="appPROBE",
        retry_delays=(0, 0),
        rate_window=0.001,
        transport=httpx.MockTransport(crm.handler),
    )
    ai_module._service = AIService(
        api_key=config.openai_api_key,
        model=config.openai_model,
        confidence_threshold=config.ai_confidence_threshold,
        timeout=120,
    )
    dispatcher = get_dispatcher(config)
    client = Client(dispatcher, bot, outbox, "Проба")

    print(f"\n{'=' * 74}\n{name.upper()}\nОжидание: {plan['expect']}\n{'-' * 74}")
    for reply in plan["replies"]:
        print(f"  Клиент: {reply}")
        for answer in await client.says(reply):
            print(f"  Бот   : {answer}")

    said = [m["text"] for m in outbox.messages if m["chat_id"] == client.id]
    handed = texts.HANDOFF_MESSAGE in said
    fields = next(
        (
            r["fields"]
            for r in crm.tables.get("Contacts", [])
            if str(r["fields"].get("telegram_id")) == str(client.id)
        ),
        {},
    )
    questions = sum(1 for m in said if m.rstrip().endswith("?"))
    print(f"{'-' * 74}")
    print(
        f"  статус={fields.get('status')} · вопросов={questions} · "
        f"передача={'да' if handed else 'нет'} · Юлия получила={len(client.yulia_got)}"
    )

    verdict = True
    if plan["handoff"] is not None and handed != plan["handoff"]:
        print(f"  ✗ ПЕРЕДАЧА: ожидалась {plan['handoff']}, получена {handed}")
        verdict = False
    if plan["handoff"] is False and client.yulia_got:
        print("  ✗ ЮЛИЮ ПОБЕСПОКОИЛИ без основания")
        verdict = False
    if "null" in " ".join(said).lower().split():
        print("  ✗ КЛИЕНТ ПОЛУЧИЛ «null»")
        verdict = False
    repeats = [q for q in said if said.count(q) > 1]
    if repeats:
        print(f"  ✗ ПОВТОР реплики: {repeats[0][:60]!r}")
        verdict = False
    print("  ✓ ок" if verdict else "  ✗ есть замечания")

    await ai_module._service.close()
    await airtable_module._client.close()
    return verdict


async def main() -> None:
    config = load_config()
    load_knowledge(config.knowledge_dir)
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    plans = {k: v for k, v in JOURNEYS.items() if wanted is None or wanted in k}
    results = {}
    for name, plan in plans.items():
        try:
            results[name] = await run(name, plan, config)
        except Exception as error:  # noqa: BLE001 — диагностика, нужен любой сбой
            print(f"  ✗ СБОЙ: {type(error).__name__}: {error}")
            results[name] = False
    print(f"\n{'=' * 74}\nИТОГ: {sum(results.values())}/{len(results)}")
    for name, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {name}")


if __name__ == "__main__":
    asyncio.run(main())
