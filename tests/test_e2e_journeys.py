"""Сквозные сценарии: полный путь клиента и полный рабочий день Юлии.

Отличие от остальных тестов: здесь ничего не вызывается напрямую. Апдейт
кладётся в реальный ``Dispatcher`` — работают middleware, фильтры, порядок
роутеров, FSM, реальный ``AirtableClient`` (со своей дедупликацией и
таймлайном) и реальный ``AIService`` (со своими retry, валидацией схемы,
проверкой стоп-фраз и правилом смещения). Подменены только три внешние
границы: HTTP к Telegram, к OpenAI и к Airtable.

Кнопки нажимаются те, что клиент реально видит: ``press`` ищет кнопку по
подписи в последнем сообщении с клавиатурой и шлёт её ``callback_data``.
Если подпись кнопки в клавиатуре изменится, тест упадёт — как упадёт и
Юлия, ищущая «Беру» там, где её больше нет.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import count

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    MessageOriginChannel,
    Update,
    User,
    Voice,
)

from bot import texts
from bot.services import ai as ai_module
from bot.services import airtable as airtable_module
from bot.services.ai import AIService
from bot.services.airtable import AirtableClient
from tests.conftest import FakeAirtable, get_shared_dispatcher
from tests.test_block6_qualification import config  # noqa: F401 — фикстура

ADMIN_ID = 999  # совпадает с telegram_admin_id из фикстуры config
GROUP_ID = -200  # telegram_discussion_group_id
CHANNEL_ID = -100

# Каждый сценарий берёт свежие telegram_id: MemoryStorage и роутеры общие
# на процесс, и состояние прошлого клиента не должно течь в следующий тест.
_telegram_ids = count(880_001)


# ── Заглушки трёх внешних границ ──


TASK_MARKERS = (
    ("scenario", "Определи сценарий начала общения"),
    ("qualify", "Проведи квалификацию клиента"),
    ("info", "Человек задал информационный вопрос"),
    ("comment", "Проанализируй комментарий"),
    ("form", "Разбери анкету"),
)


class ScriptedOpenAI:
    """OpenAI по сценарию: очередь ответов на каждую из пяти задач.

    Задача определяется по тексту промпта, а не по порядку вызовов: так тест
    не разваливается, когда хендлер добавляет промежуточный вызов, и сразу
    видно, если вместо квалификации ушёл запрос на анализ комментария.
    """

    def __init__(self) -> None:
        self.queues: dict[str, list] = {name: [] for name, _ in TASK_MARKERS}
        self.calls: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def script(self, task: str, *replies) -> None:
        """Ответы модели: dict (сериализуется), str, int (HTTP-код) или исключение."""
        self.queues[task].extend(replies)

    def last_prompt(self, task: str) -> str:
        """Последний промпт этой задачи — что модель увидела на самом деле."""
        for name, prompt in reversed(self.prompts):
            if name == task:
                return prompt
        raise AssertionError(f"запросов задачи {task!r} не было")

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):  # ping из /health
            return httpx.Response(200, json={"data": []})
        prompt = json.loads(request.content)["messages"][-1]["content"]
        task = next((name for name, marker in TASK_MARKERS if marker in prompt), None)
        assert task is not None, f"неопознанная задача в промпте: {prompt[:150]!r}"
        self.calls.append(task)
        self.prompts.append((task, prompt))
        queue = self.queues[task]
        assert queue, f"сценарий не задал ответ OpenAI для задачи {task!r}"
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "simulated"}})
        if isinstance(item, dict):
            item = json.dumps(item, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": item}}]})


class Outbox(BaseSession):
    """Сессия Telegram: наружу ничего не уходит, всё исходящее запоминается."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.callback_answers: list[str | None] = []
        self.blocked: set[int] = set()  # чаты, где «бот заблокирован»
        self._ids = count(5000)

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            chat_id = int(method.chat_id)
            if chat_id in self.blocked:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            record = {
                "message_id": next(self._ids),
                "chat_id": chat_id,
                "text": method.text,
                "markup": method.reply_markup,
                "reply_to": method.reply_to_message_id,
            }
            self.messages.append(record)
            return Message(
                message_id=record["message_id"],
                date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, EditMessageText):
            self.edits.append(
                {
                    "chat_id": int(method.chat_id or 0),
                    "message_id": method.message_id,
                    "text": method.text,
                }
            )
            return Message(
                message_id=method.message_id or 0,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id or 0), type="private"),
                text=method.text,
            )
        if isinstance(method, EditMessageReplyMarkup):
            self.edits.append(
                {
                    "chat_id": int(method.chat_id or 0),
                    "message_id": method.message_id,
                    "markup": method.reply_markup,
                }
            )
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.callback_answers.append(method.text)
            return True
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def close(self):
        pass


class Person:
    """Участник: клиент или Юлия. Знает свой telegram_id и свою переписку."""

    def __init__(self, stage: "Stage", telegram_id: int, name: str, username: str | None):
        self.stage = stage
        self.id = telegram_id
        first, _, last = name.partition(" ")
        self.tg = User(
            id=telegram_id,
            is_bot=False,
            first_name=first,
            last_name=last or None,
            username=username,
        )
        self.name = name

    # ── что человек делает ──

    async def start(self, param: str = "") -> None:
        await self.stage.feed_message(self, f"/start {param}".strip())

    async def says(self, text: str) -> None:
        await self.stage.feed_message(self, text)

    async def sends_voice(self) -> None:
        await self.stage.feed_message(
            self, None, voice=Voice(file_id="v1", file_unique_id="v1u", duration=3)
        )

    async def presses(self, label: str) -> None:
        await self.stage.press(self, label)

    # ── что человек видит ──

    @property
    def inbox(self) -> list[str]:
        return [m["text"] for m in self.stage.session.messages if m["chat_id"] == self.id]

    @property
    def last(self) -> str:
        assert self.inbox, f"в чат {self.id} не пришло ни одного сообщения"
        return self.inbox[-1]

    @property
    def buttons(self) -> list[str]:
        """Подписи кнопок под последним сообщением с клавиатурой."""
        for record in reversed(self.stage.session.messages):
            if record["chat_id"] == self.id and record["markup"] is not None:
                return [b.text for row in record["markup"].inline_keyboard for b in row]
        return []


class Stage:
    """Сцена: боевой диспетчер и три подменённые внешние границы."""

    def __init__(self, config) -> None:
        self.config = config
        self.crm = FakeAirtable()
        airtable_module._client = AirtableClient(
            api_key="pat-test",
            base_id="appE2E",
            retry_delays=(0, 0),
            rate_window=0.001,
            transport=httpx.MockTransport(self.crm.handler),
        )
        self.openai = ScriptedOpenAI()
        ai_module._service = AIService(
            api_key="sk-test",
            retry_delays=(0, 0, 0),
            confidence_threshold=config.ai_confidence_threshold,
            transport=httpx.MockTransport(self.openai.handler),
        )
        self.session = Outbox()
        self.bot = Bot(token="42:TEST", session=self.session)
        self.dp = get_shared_dispatcher(config)
        self._updates = count(1)
        self._messages = count(1)
        self.yulia = Person(self, ADMIN_ID, "Юлия Гейкина", "geikina")

    async def aclose(self) -> None:
        await airtable_module._client.close()
        await ai_module._service.close()

    def client(self, name: str = "Анна Петрова", username: str | None = "anna") -> Person:
        return Person(self, next(_telegram_ids), name, username)

    # ── ввод ──

    async def feed_message(self, person: Person, text: str | None, **extra) -> None:
        message = Message(
            message_id=next(self._messages),
            date=datetime.now(timezone.utc),
            chat=Chat(id=person.id, type="private", first_name=person.name),
            from_user=person.tg,
            text=text,
            **extra,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), message=message))

    async def press(self, person: Person, label: str) -> None:
        """Нажимает кнопку с такой подписью в последней клавиатуре этого чата."""
        for record in reversed(self.session.messages):
            if record["chat_id"] != person.id or record["markup"] is None:
                continue
            for row in record["markup"].inline_keyboard:
                for button in row:
                    if label in button.text:
                        await self._feed_callback(person, button.callback_data, record)
                        return
        raise AssertionError(f"кнопка {label!r} не найдена в чате {person.id}")

    async def press_raw(self, person: Person, data: str) -> None:
        """Нажатие по callback_data напрямую — для проверки чужих нажатий."""
        record = next((m for m in reversed(self.session.messages) if m["markup"] is not None), None)
        assert record is not None, "нет ни одного сообщения с клавиатурой"
        await self._feed_callback(person, data, record)

    async def _feed_callback(self, person: Person, data: str, record: dict) -> None:
        card = Message(
            message_id=record["message_id"],
            date=datetime.now(timezone.utc),
            chat=Chat(id=record["chat_id"], type="private"),
            from_user=User(id=42, is_bot=True, first_name="bot"),
            text=record["text"],
        ).as_(self.bot)
        callback = CallbackQuery(
            id=str(next(self._updates)),
            from_user=person.tg,
            chat_instance="e2e",
            message=card,
            data=data,
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=next(self._updates), callback_query=callback)
        )

    async def comment_in_group(
        self, person: Person, text: str, *, post_id: int = 77, post_text: str = "Пост о методе"
    ) -> None:
        """Комментарий под автопересланным постом канала (Блок 8)."""
        forwarded = Message(
            message_id=post_id + 1000,
            date=datetime.now(timezone.utc),
            chat=Chat(id=GROUP_ID, type="supergroup"),
            text=post_text,
            is_automatic_forward=True,
            forward_origin=MessageOriginChannel(
                type="channel",
                date=datetime.now(timezone.utc),
                chat=Chat(id=CHANNEL_ID, type="channel", username="geikina_channel"),
                message_id=post_id,
            ),
        )
        message = Message(
            message_id=next(self._messages),
            date=datetime.now(timezone.utc),
            chat=Chat(id=GROUP_ID, type="supergroup"),
            from_user=person.tg,
            text=text,
            reply_to_message=forwarded,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), message=message))

    # ── чтение CRM ──

    def contact(self, person: Person) -> dict:
        for record in self.crm.tables.get("Contacts", []):
            if record["fields"].get("telegram_id") == person.id:
                return record["fields"]
        raise AssertionError(f"контакт {person.id} не заведён в CRM")

    def table(self, name: str) -> list[dict]:
        return [r["fields"] for r in self.crm.tables.get(name, [])]

    def touches(self, person: Person) -> list[str]:
        return [
            f["type"] for f in self.table("Touches") if f.get("contact_telegram_id") == person.id
        ]


@pytest.fixture
async def stage(config):  # noqa: F811
    scene = Stage(config)
    yield scene
    await scene.aclose()


# ── Готовые ответы модели ──


def handed_off(person: Person) -> bool:
    """Клиенту сказали о передаче и следом рассказали, что его ждёт."""
    return person.inbox[-2:] == [texts.HANDOFF_MESSAGE, texts.HANDOFF_FOLLOWUP]


def scenario(kind: str, confidence: int = 95) -> dict:
    return {"scenario": kind, "confidence": confidence, "reason": "признаки из сообщения"}


def qualification(**overrides) -> dict:
    data = {
        "summary": "Повторяющаяся ситуация в отношениях, ищет причину.",
        "key_phrases": ["уже полгода одно и то же"],
        "status": "warm",
        "status_reason": "описала проблему, интересуется методом",
        "awareness": "medium",
        "readiness": "medium",
        "urgency": "low",
        "confidence": 90,
        "next_action": "предложить диагностику",
        "needs_yulia": False,
        "needs_yulia_reason": None,
        "product_interest": "diagnostics",
        "interests": ["повторяющиеся сценарии"],
        "bot_response": "Понимаю. Расскажите, что для вас сейчас важнее всего?",
    }
    data.update(overrides)
    return data


def info_answer(text: str = "Диагностика длится до 60 минут.", **overrides) -> dict:
    data = {"answer": text, "needs_yulia": False, "reason": None}
    data.update(overrides)
    return data


def comment_analysis(**overrides) -> dict:
    data = {
        "topic": "повторяющиеся ситуации",
        "emotion": "interested",
        "key_problem": "ощущение тупика",
        "interest_level": 4,
        "is_potential_client": True,
        "needs_reply": True,
        "request_detected": "хочет разобраться",
        "suggested_reply": "Спасибо за отклик. Если хотите разобраться — напишите мне в личные.",
        "should_invite_to_bot": True,
        "confidence": 88,
    }
    data.update(overrides)
    return data


def form_analysis(**overrides) -> dict:
    data = {
        "main_request": "Хочет понять, почему ситуация повторяется.",
        "summary": "Клиентка описывает повторяющийся сценарий в отношениях.",
        "key_phrases": ["одно и то же по кругу"],
        "preliminary_status": "warm",
        "topics_to_clarify": ["как давно длится ситуация"],
        "confidence": 86,
    }
    data.update(overrides)
    return data


# ══════════════════════════════════════════════════════════════════════
#                    ЧАСТЬ 1. ПУТЬ КЛИЕНТА
# ══════════════════════════════════════════════════════════════════════


async def test_scenario_a_ready_reaches_yulia_in_two_questions(stage: Stage) -> None:
    """A «Готовность записаться»: ровно два вопроса и немедленная передача.

    Проверяется весь путь: deep link → приветствие → сценарий → два вопроса
    сценария A (их формулировки отличаются от вопросов B!) → карточка Юлии
    с четырьмя кнопками, задача, остановка автоматики.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("A_ready"))
    stage.openai.script("qualify", qualification(status="hot", confidence=96))

    await anna.start("site")
    await anna.says("Хочу записаться на диагностику")
    await anna.says("Постоянно теряю деньги в бизнесе")
    await anna.says("Хочу выйти на стабильный доход")

    assert anna.inbox == [
        texts.GREETING,
        texts.A_INTRO,
        texts.A_QUESTION_2,
        texts.HANDOFF_MESSAGE,
        texts.HANDOFF_FOLLOWUP,
    ]

    card = stage.yulia.last
    assert "🔥 ГОРЯЧИЙ ЛИД" in card
    assert "Хочу выйти на стабильный доход" in card, "ответ клиента не попал в секцию «ЧЕГО ХОЧЕТ»"
    assert stage.yulia.buttons == ["✅ Беру", "🔄 Прогрев", "⛔ Нецелевой", "⏸ Пауза"]

    fields = stage.contact(anna)
    assert fields["status"] == "hot"
    assert fields["paused"] is True and fields["assigned_to"] == "yulia"
    assert fields["source"] == "site"
    assert fields["qualification_completed"] is True
    assert "handoff" in stage.touches(anna)
    assert any(t["action"].startswith("Связаться с") for t in stage.table("Tasks"))


async def test_scenario_b_stops_asking_when_information_is_enough(stage: Stage) -> None:
    """B «Описывает проблему»: квалификация завершается досрочно.

    Первое сообщение — уже ответ на вопрос 1. После ответа на вопрос 2
    информации достаточно (confidence ≥ порога) — вопросы 3–5 не задаются.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify", *[qualification(confidence=92, bot_response="Понимаю вас.")] * 4
    )

    await anna.start("telegram_channel")
    await anna.says("У меня всё время повторяется одна и та же ситуация")
    await anna.says("Года полтора")
    await anna.says("Сильнее всего беспокоит, что я не понимаю причину")
    await anna.says("Пробовала психолога")
    await anna.says("Хочу перестать это повторять")

    assert anna.inbox == [
        texts.GREETING,
        texts.QUESTION_DURATION,
        texts.QUESTION_2,
        texts.QUESTION_3,
        texts.QUESTION_4,
        "Понимаю вас.",
    ]
    assert texts.QUESTION_5 not in anna.inbox, "пятый вопрос задан, хотя информации хватало"
    assert stage.yulia.inbox == [], "тёплый клиент не должен беспокоить Юлию"

    fields = stage.contact(anna)
    assert fields["status"] == "warm"
    assert fields["qualification_completed"] is True
    assert fields["next_step"] == "предложить диагностику"
    assert fields.get("next_action_date"), "шаг без срока не попадёт ни в один фильтр Юлии"
    assert fields.get("paused") is not True, "тёплый клиент остаётся в диалоге с ботом"


async def test_scenario_b_asks_all_five_questions_when_unclear(stage: Stage) -> None:
    """B: пока информации мало, задаются вопросы 3, 4 и 5 — по одному за раз."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    # Три промежуточные квалификации с низкой уверенностью, затем финальная
    stage.openai.script(
        "qualify",
        qualification(confidence=40),
        qualification(confidence=45),
        qualification(confidence=50),
        qualification(confidence=60),
        qualification(confidence=93, bot_response="Спасибо, теперь картина яснее."),
    )

    await anna.start("referral")
    await anna.says("Не понимаю, почему всё рушится")
    await anna.says("Уже второй год")
    await anna.says("Беспокоит ощущение бессилия")
    await anna.says("Пробовала психологов")
    await anna.says("Хочу стабильности")
    await anna.says("Сейчас, потому что дальше некуда")

    assert anna.inbox == [
        texts.GREETING,
        texts.QUESTION_DURATION,
        texts.QUESTION_2,
        texts.QUESTION_3,
        texts.QUESTION_4,
        texts.QUESTION_5,
        "Спасибо, теперь картина яснее.",
    ]
    # Низкая уверенность в промежуточном шаге — это «мало информации»,
    # а не повод дёргать Юлию
    assert stage.yulia.inbox == []


async def test_low_confidence_at_the_end_goes_to_yulia_with_a_warning(stage: Stage) -> None:
    """Финальная квалификация ниже 85% — решает Юлия, а не AI (порог из ТЗ)."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    # Четыре промежуточных шага «информации мало» и финальный с той же оценкой
    stage.openai.script("qualify", *[qualification(confidence=60)] * 5)

    await anna.start("site")
    await anna.says("Что-то не так, но не могу объяснить")
    await anna.says("Давно уже")
    await anna.says("Не знаю даже, с чего начать")
    await anna.says("Наверное, всё сразу")
    await anna.says("Сложно сказать")
    await anna.says("Просто устала")

    assert handed_off(anna)
    card = stage.yulia.last
    assert "ТРЕБУЕТСЯ ЭКСПЕРТНАЯ ОЦЕНКА" in card
    assert "уверенность AI 60%" in card
    assert stage.contact(anna)["assigned_to"] == "yulia"


async def test_shift_rule_sends_a_warm_client_to_yulia(stage: Stage) -> None:
    """Правило смещения: warm при высокой готовности и срочности → hot.

    Модель вернула warm — код обязан поднять статус сам, иначе горячий
    клиент молча остаётся в прогреве.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(
            status="warm",
            readiness="high",
            urgency="high",
            readiness_signal="deadline",
            confidence=95,
        ),
    )

    await anna.start("site")
    await anna.says("Ситуация повторяется, и решать надо прямо сейчас")
    # Названный срок — наблюдаемый признак готовности: цепочка вопросов
    # обрывается законно, ждать остальных ответов незачем
    await anna.says("Больше года, но решить надо до конца месяца")

    assert handed_off(anna)
    assert "🔥 ГОРЯЧИЙ ЛИД" in stage.yulia.last
    assert "смещени" in stage.yulia.last.lower(), "причина передачи не названа"
    assert stage.contact(anna)["status"] == "hot"


async def test_describing_a_situation_is_not_a_hot_lead(stage: Stage) -> None:
    """Человек описал ситуацию и ни слова не сказал о записи — это не «горячий».

    Случай Елены 27.07: модель выдала hot с осями high/high/high, и Юлия
    получила карточку «🔥 ГОРЯЧИЙ ЛИД» на клиентку, которая всего лишь
    рассказала о бизнесе. Ложный ярлык задаёт Юлии неверную рамку разговора,
    поэтому статус понижается — но передача остаётся, решение за ней.
    """
    elena = stage.client(name="Елена Иванова", username="elena")
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        *[
            qualification(
                status="hot",
                awareness="high",
                readiness="high",
                urgency="high",
                readiness_signal="none",
                confidence=92,
            )
        ]
        * 4,
    )

    await elena.start("telegram_channel")
    await elena.says("Развиваю бизнес-проект, есть неуверенность в формате работы")
    await elena.says("Больше года")
    await elena.says("Очень многое зависит от меня, каждый этап закрываю лично")
    await elena.says("Пробовала делегировать")
    await elena.says("Хочу выстроить систему")

    card = stage.yulia.last
    assert "ГОРЯЧИЙ ЛИД" not in card, "выдуманная готовность доехала до карточки"
    assert "ТЁПЛЫЙ ЛИД" in card
    assert "о готовности записаться не говорил" in card
    assert "Готовность: средняя" in card and "Срочность: средняя" in card
    assert stage.contact(elena)["status"] == "warm"


async def test_request_category_reaches_the_card_and_the_crm(stage: Stage) -> None:
    """Тема запроса классифицируется и попадает в карточку и в CRM.

    Словарь тем — «Возможные направления» из продуктовой линейки Юлии,
    а не выдуманный: статистика должна складываться в те категории,
    которыми она сама описывает практику.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(
            status="hot",
            readiness_signal="booking",
            request_category="делегирование",
            confidence=93,
        ),
    )

    await anna.start("site")
    await anna.says("Не могу передать задачи команде")
    await anna.says("Всё замыкается на мне")

    assert "Тема: делегирование" in stage.yulia.last
    assert stage.contact(anna)["request_category"] == "делегирование"


async def test_invented_request_category_is_rejected(stage: Stage) -> None:
    """Категория вне словаря — брак схемы: в CRM не должно появиться мусора."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script("qualify", qualification(request_category="карьера и успех"))

    await anna.start("site")
    await anna.says("Не могу передать задачи команде")
    await anna.says("Всё замыкается на мне")

    assert anna.last == texts.TECH_ERROR
    assert "request_category" not in stage.contact(anna)


async def test_repeated_message_is_not_counted_as_engagement(stage: Stage) -> None:
    """Повтор одной и той же реплики не попадает в промпт дважды.

    Клиент дублирует сообщение, когда ответа не видно; модель же считала это
    за «взаимодействовал трижды» и поднимала статус.
    """
    elena = stage.client(name="Елена Иванова", username="elena")
    stage.crm.seed(
        "Contacts",
        {
            "telegram_id": elena.id,
            "name": elena.name,
            "status": "cold",
            "scenario": "B_problem",
            "conversation_history": history(
                ("client", "Всё держится на мне"),
                ("bot", texts.QUESTION_2),
                ("client", "Каждый этап закрываю лично"),
            ),
        },
    )
    stage.openai.script("qualify", qualification(confidence=50))

    await elena.says("Каждый этап закрываю лично")  # тот же текст ещё раз

    prompt = stage.openai.last_prompt("qualify")
    assert prompt.count("Каждый этап закрываю лично") == 1, "повтор ушёл в модель дважды"


async def test_non_target_is_closed_politely_and_automation_stops(stage: Stage) -> None:
    """Уверенный вердикт «нецелевой»: корректное завершение, автоматика стоп."""
    anna = stage.client()
    closing = "Спасибо, что написали! Этот запрос вне специализации Юлии."
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(status="non_target", confidence=95, bot_response=closing),
    )

    await anna.start("other")
    await anna.says("Продаёте рекламу в канале?")
    await anna.says("Нужен прайс на размещение")

    assert anna.last == closing
    fields = stage.contact(anna)
    assert fields["status"] == "non_target"
    assert fields["paused"] is True
    assert stage.yulia.inbox == [], "нецелевое обращение не требует внимания Юлии"


async def test_unsure_non_target_is_never_closed_by_ai(stage: Stage) -> None:
    """Неуверенный вердикт «нецелевой» не имеет права закрыть диалог.

    Ошибка здесь стоит живого клиента: AI на 55% решил, что человек не наш,
    и попрощался. По ТЗ решение уходит Юлии.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(status="non_target", confidence=55, bot_response="До свидания."),
    )

    await anna.start("site")
    await anna.says("Я по поводу обучения, но не уверена")
    await anna.says("Хочу понять, подходит ли мне это")

    # Передача есть, но рассказа о диагностике нет: вердикт «нецелевой»
    # под вопросом, и предлагать формат работы преждевременно
    assert anna.last == texts.HANDOFF_MESSAGE, "AI закрыл диалог, не будучи уверенным"
    assert stage.yulia.inbox, "Юлия не узнала о спорном случае"
    assert stage.contact(anna)["paused"] is True


async def test_scenario_c_answers_questions_without_qualifying(stage: Stage) -> None:
    """C «Информационный интерес»: отвечаем по базе знаний, не квалифицируем."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("C_info"), scenario("C_info"))
    stage.openai.script(
        "info",
        info_answer("Системная диагностика — это встреча до 60 минут."),
        info_answer("Работа проходит в формате онлайн."),
    )

    await anna.start("site")
    await anna.says("Что такое системная диагностика?")
    await anna.says("А как проходит работа?")

    assert anna.inbox[1:] == [
        "Системная диагностика — это встреча до 60 минут.",
        "Работа проходит в формате онлайн.",
    ]
    assert "qualify" not in stage.openai.calls, "запущена квалификация вместо простого ответа"
    assert stage.yulia.inbox == []


async def test_scenario_c_switches_to_b_when_a_request_appears(stage: Stage) -> None:
    """C → B: у человека появился собственный запрос — начинаем квалификацию."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("C_info"), scenario("B_problem"))
    stage.openai.script("info", info_answer("Метод помогает увидеть систему целиком."))

    await anna.start("site")
    await anna.says("Расскажите о методе")
    await anna.says("У меня как раз повторяется одна ситуация")

    assert anna.last == texts.QUESTION_DURATION, "переключение на сценарий B не произошло"
    assert stage.contact(anna)["scenario"] == "B_problem"


async def test_scenario_c_question_beyond_knowledge_goes_to_yulia(stage: Stage) -> None:
    """Вопроса нет в базе знаний → AI не выдумывает, а передаёт Юлии."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("C_info"))
    stage.openai.script(
        "info",
        info_answer("Этот вопрос требует уточнения.", needs_yulia=True, reason="нет в базе"),
    )
    stage.openai.script("qualify", qualification(confidence=88))

    await anna.start("site")
    await anna.says("Вы работаете с юрлицами по договору?")

    assert handed_off(anna)
    assert stage.yulia.inbox, "вопрос вне базы знаний не дошёл до Юлии"


async def test_source_buttons_when_there_is_no_deep_link(stage: Stage) -> None:
    """/start без метки: пять кнопок из ТЗ, выбор пишется в источник контакта."""
    anna = stage.client()

    await anna.start()

    assert anna.last == texts.SOURCE_QUESTION
    assert anna.buttons == ["Telegram-канал", "Facebook / VK", "Рекомендация", "Сайт", "Другое"]

    await anna.presses("Сайт")

    assert anna.last == texts.GREETING
    assert stage.contact(anna)["source"] == "site"


async def test_message_instead_of_pressing_a_button_is_not_lost(stage: Stage) -> None:
    """Клиент проигнорировал кнопки и написал текстом — диалог не встаёт."""
    anna = stage.client()

    await anna.start()
    await anna.says("Пришла из подкаста")

    assert anna.last == texts.GREETING
    fields = stage.contact(anna)
    assert fields["source"] == "telegram_dm"
    assert "Пришла из подкаста" in str(stage.table("Touches"))


async def test_deep_link_carries_the_utm_tag(stage: Stage) -> None:
    """«источник__метка»: источник распознан, метка записана в контакт."""
    anna = stage.client()

    await anna.start("site__spring2026")

    fields = stage.contact(anna)
    assert fields["source"] == "site"
    assert fields["utm"] == "spring2026"
    assert anna.last == texts.GREETING


async def test_repeat_start_mid_dialog_does_not_reset_the_conversation(stage: Stage) -> None:
    """Повторный /start посреди диалога: состояние FSM сохраняется."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script("qualify", qualification(confidence=91, bot_response="Понимаю."))

    await anna.start("site")
    await anna.says("Повторяется одна и та же ситуация")
    await anna.start("site")  # клиент нажал /start ещё раз

    assert anna.last == texts.CONTINUE_DIALOG
    assert stage.contact(anna)["touches_count"] >= 2

    # Состояние сохранилось: бот ждёт ответ на заданный вопрос, а не начинает заново
    await anna.says("Года полтора")
    assert anna.last == texts.QUESTION_2, "диалог продолжился не с того места"


async def test_client_handed_over_keeps_a_reference_assistant(stage: Stage) -> None:
    """Переданный Юлии клиент получает справки, но не квалификацию.

    Решение Юлии 2026-07-29: до её подключения бот остаётся ассистентом и
    отвечает на организационные вопросы по базе знаний. Останавливается
    именно воронка — статус не меняется, передача не повторяется.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("A_ready"))
    stage.openai.script("qualify", qualification(status="hot", confidence=96))
    stage.openai.script("info", info_answer("Диагностика длится до 60 минут."))

    await anna.start("site")
    await anna.says("Хочу записаться")
    await anna.says("Проблемы в бизнесе")
    await anna.says("Хочу роста")
    handed_over = len(anna.inbox)

    await anna.says("А сколько длится диагностика?")

    new_messages = anna.inbox[handed_over:]
    assert new_messages == ["Диагностика длится до 60 минут."], "справка не дана"
    assert texts.HANDOFF_MESSAGE not in new_messages, "передача повторилась"
    assert texts.HANDOFF_FOLLOWUP not in new_messages, "описание диагностики повторилось"
    assert stage.openai.queues["qualify"] == [], "квалификация запускалась повторно"
    forwarded = [m for m in stage.yulia.inbox if "написал(а): А сколько длится" in m]
    assert forwarded, "сообщение переданного клиента не переслано Юлии"

    fields = stage.contact(anna)
    assert fields["status"] == "hot" and fields["paused"] is True


async def test_handed_over_client_without_a_question_is_told_once(stage: Stage) -> None:
    """Не вопрос, а дополнение: подтверждаем приём, не повторяя передачу."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("A_ready"))
    stage.openai.script("qualify", qualification(status="hot", confidence=96))
    # На реплику без вопроса база знаний ответа не даёт
    stage.openai.script(
        "info",
        info_answer("", needs_yulia=True, reason="не вопрос"),
        info_answer("", needs_yulia=True, reason="не вопрос"),
    )

    await anna.start("site")
    await anna.says("Хочу записаться")
    await anna.says("Проблемы в бизнесе")
    await anna.says("Хочу роста")
    handed_over = len(anna.inbox)

    await anna.says("Забыла сказать: команда из пяти человек")
    await anna.says("И ещё филиал в другом городе")

    new_messages = anna.inbox[handed_over:]
    assert new_messages == [texts.ALREADY_WITH_YULIA, texts.INFO_PASSED_TO_YULIA]
    assert texts.HANDOFF_MESSAGE not in new_messages, "передача повторилась"
    assert len([m for m in stage.yulia.inbox if "написал(а)" in m]) == 2


async def test_repeat_start_after_handoff_does_not_restart_automation(stage: Stage) -> None:
    """/start от переданного клиента: одно сообщение и выход, без приветствия."""
    anna = stage.client()
    stage.crm.seed(
        "Contacts",
        {"telegram_id": anna.id, "name": anna.name, "assigned_to": "yulia", "paused": True},
    )

    await anna.start("site")

    assert anna.inbox == [texts.ALREADY_WITH_YULIA]
    assert texts.GREETING not in anna.inbox


async def test_voice_message_gets_a_polite_request_for_text(stage: Stage) -> None:
    """Голосовое в диалоге: просим текст, состояние не двигаем, ничего не теряем."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify", *[qualification(confidence=91, bot_response="Понимаю.")] * 4
    )

    await anna.start("site")
    await anna.says("Повторяется одна ситуация")
    await anna.sends_voice()

    assert anna.last == texts.ASK_TEXT_PLEASE
    assert "<voice>" in str(stage.table("Touches")), "нетекстовое сообщение не зафиксировано"

    # Голосовое не сдвинуло состояние: диалог продолжается с того же вопроса
    await anna.says("Года два")
    assert anna.last == texts.QUESTION_2, "после голосового диалог сбился"
    await anna.says("Беспокоит непонимание")
    await anna.says("Пробовала разное")
    await anna.says("Хочу ясности")
    assert anna.last == "Понимаю."


async def test_openai_failure_never_shows_a_traceback_to_the_client(stage: Stage) -> None:
    """Окончательный сбой OpenAI: клиенту вежливый текст, Юлии — задача."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script("qualify", 500, 500)  # 5xx + retry по таблице ТЗ

    await anna.start("site")
    await anna.says("Повторяется одна ситуация")
    await anna.says("Беспокоит непонимание причины")

    assert anna.last == texts.TECH_ERROR
    tasks = [t["action"] for t in stage.table("Tasks")]
    assert any("Проверить диалог" in action for action in tasks), "Юлия не узнала о сбое"


async def test_stop_phrase_from_the_model_never_reaches_the_client(stage: Stage) -> None:
    """Модель нарушила запрет — ответ подменяется, клиент уходит к Юлии."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(confidence=95, bot_response="Я гарантирую вам результат после диагностики."),
    )

    await anna.start("site")
    await anna.says("Повторяется одна ситуация")
    await anna.says("Хочу понять причину")

    assert "гарантиру" not in " ".join(anna.inbox).lower(), "стоп-фраза дошла до клиента"
    assert handed_off(anna)
    assert stage.yulia.inbox, "Юлия не уведомлена о срабатывании стоп-фразы"


async def test_client_matures_in_open_dialog_and_is_handed_over(stage: Stage) -> None:
    """Дозревание после квалификации: тёплый клиент сам доходит до готовности."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        # Четыре шага цепочки вопросов: тёплый, признака готовности нет
        *[qualification(confidence=92, bot_response="Понимаю вас.")] * 4,
        # Клиент сам вернулся с готовностью — теперь признак прозвучал
        qualification(
            status="hot",
            confidence=94,
            readiness_signal="booking",
            needs_yulia=True,
            needs_yulia_reason="Клиент готов записаться",
        ),
    )

    await anna.start("site")
    await anna.says("Повторяется одна ситуация")
    await anna.says("Больше года")
    await anna.says("Беспокоит непонимание")
    await anna.says("Пробовала сама")
    await anna.says("Хочу разобраться")
    assert anna.last == "Понимаю вас."

    await anna.says("Я подумала — давайте записываться")

    assert handed_off(anna)
    assert "🔥 ГОРЯЧИЙ ЛИД" in stage.yulia.last


async def test_message_after_restart_without_state_is_not_ignored(stage: Stage) -> None:
    """Рестарт бота: FSM пуст, но клиент посреди диалога не получает тишину."""
    anna = stage.client()
    stage.crm.seed("Contacts", {"telegram_id": anna.id, "name": anna.name, "status": "cold"})
    stage.openai.script("scenario", scenario("C_info"))
    stage.openai.script("info", info_answer("Диагностика длится до 60 минут."))

    await anna.says("Сколько длится диагностика?")  # без /start и без состояния

    assert anna.last == "Диагностика длится до 60 минут."


def history(*turns: tuple[str, str]) -> str:
    """conversation_history в том виде, в каком её пишет бот."""
    return json.dumps(
        [{"role": role, "text": text, "date": "2026-07-27T15:55:00+00:00"} for role, text in turns],
        ensure_ascii=False,
    )


async def test_restart_mid_dialog_does_not_repeat_the_question(stage: Stage) -> None:
    """Рестарт посреди квалификации: разговор продолжается, а не начинается.

    Случай 27.07: клиентка ответила на вопрос 2 ровно в момент перезапуска,
    состояние FSM погибло вместе с процессом, и она услышала тот же вопрос
    ещё раз. До исправления диалог заходил на второй круг с определения
    сценария; теперь позиция восстанавливается по переписке из CRM.
    """
    elena = stage.client(name="Елена Иванова", username="elena")
    stage.crm.seed(
        "Contacts",
        {
            "telegram_id": elena.id,
            "name": elena.name,
            "status": "cold",
            "scenario": "B_problem",
            "conversation_history": history(
                ("client", "Развиваю бизнес-проект, есть неуверенность в формате работы"),
                ("bot", texts.QUESTION_2),
            ),
        },
    )
    stage.openai.script("qualify", qualification(confidence=50))

    await elena.says("Очень многое зависит от меня, каждый этап закрываю лично")

    assert elena.last != texts.QUESTION_2, "бот повторил вопрос, на который уже получил ответ"
    assert elena.last == texts.QUESTION_3, "диалог не продолжился со следующего шага"
    assert "scenario" not in stage.openai.calls, "разговор пошёл на второй круг"


async def test_restart_keeps_the_answers_for_yulias_card(stage: Stage) -> None:
    """Ответы, данные до рестарта, доходят до карточки, а не теряются с FSM."""
    elena = stage.client(name="Елена Иванова", username="elena")
    stage.crm.seed(
        "Contacts",
        {
            "telegram_id": elena.id,
            "name": elena.name,
            "status": "cold",
            "scenario": "B_problem",
            "conversation_history": history(
                ("client", "Не растёт бизнес"),
                ("bot", texts.QUESTION_2),
                ("client", "Всё держится на мне"),
                ("bot", texts.QUESTION_3),
                ("client", "Пробовала нанимать помощников"),
                ("bot", texts.QUESTION_4),
            ),
        },
    )
    stage.openai.script("qualify", qualification(status="hot", confidence=95))

    await elena.says("Хочу выйти из операционки")

    card = stage.yulia.last
    assert "Пробовала нанимать помощников" in card, "потеряна секция «ЧТО УЖЕ ПРОБОВАЛ(А)»"
    assert "Хочу выйти из операционки" in card, "потеряна секция «ЧЕГО ХОЧЕТ»"


async def test_restart_in_scenario_a_resumes_at_the_right_question(stage: Stage) -> None:
    """Сценарий A после рестарта не спрашивает про ситуацию заново."""
    anna = stage.client()
    stage.crm.seed(
        "Contacts",
        {
            "telegram_id": anna.id,
            "name": anna.name,
            "status": "cold",
            "scenario": "A_ready",
            "conversation_history": history(
                ("client", "Хочу записаться"),
                ("bot", texts.A_INTRO),
            ),
        },
    )

    await anna.says("Не растёт бизнес")

    assert anna.last == texts.A_QUESTION_2


async def test_conversation_history_keeps_both_sides(stage: Stage) -> None:
    """Полная переписка пишется в CRM — иначе карточка Юлии пуста (ТЗ, Часть 3)."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script("qualify", qualification(confidence=92, bot_response="Понимаю вас."))

    await anna.start("site")
    await anna.says("Повторяется одна ситуация")
    await anna.says("Беспокоит непонимание")

    history = json.loads(stage.contact(anna)["conversation_history"])
    roles = [turn["role"] for turn in history]
    assert roles == ["client", "bot", "client", "bot"]
    assert history[0]["text"] == "Повторяется одна ситуация"
    assert history[1]["text"] == texts.QUESTION_DURATION
    assert history[-1]["text"] == texts.QUESTION_2


# ══════════════════════════════════════════════════════════════════════
#                    ЧАСТЬ 2. РАБОЧИЙ ДЕНЬ ЮЛИИ
# ══════════════════════════════════════════════════════════════════════


async def handed_over_client(stage: Stage) -> Person:
    """Клиент, доведённый до карточки у Юлии, — исходная точка её сценариев."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("A_ready"))
    stage.openai.script("qualify", qualification(status="hot", confidence=96))
    await anna.start("site")
    await anna.says("Хочу записаться")
    await anna.says("Не растёт бизнес")
    await anna.says("Хочу стабильный доход")
    return anna


async def test_yulia_takes_the_client(stage: Stage) -> None:
    """«✅ Беру»: клиент за Юлией, задача создана, клиент предупреждён."""
    anna = await handed_over_client(stage)

    await stage.yulia.presses("Беру")

    fields = stage.contact(anna)
    assert fields["assigned_to"] == "yulia" and fields["paused"] is True
    assert fields.get("handoff_date")
    assert anna.last == texts.YULIA_WILL_CONTACT
    assert any(t["created_by"] == "yulia" for t in stage.table("Tasks"))
    assert any("✅ Взято в работу" in e.get("text", "") for e in stage.session.edits)


async def test_yulia_returns_the_client_to_nurturing(stage: Stage) -> None:
    """«🔄 Прогрев»: автоматика включается обратно, диалог продолжается."""
    anna = await handed_over_client(stage)

    await stage.yulia.presses("Прогрев")

    fields = stage.contact(anna)
    assert fields["status"] == "warm"
    assert fields["paused"] is False and fields["assigned_to"] == "ai"

    # Главное — бот снова разговаривает с этим человеком. Квалификация уже
    # пройдена, поэтому это свободный диалог, а не заход с определения сценария
    stage.openai.script("qualify", qualification(confidence=90, bot_response="Работа онлайн."))
    await anna.says("А как проходит работа?")
    assert anna.last == "Работа онлайн."


async def test_yulia_marks_the_client_as_non_target(stage: Stage) -> None:
    """«⛔ Нецелевой»: корректное прощание клиенту, автоматика стоп."""
    anna = await handed_over_client(stage)

    await stage.yulia.presses("Нецелевой")

    fields = stage.contact(anna)
    assert fields["status"] == "non_target"
    assert fields["paused"] is True and fields["result"] == "declined"
    assert anna.last == texts.NON_TARGET_CLOSING


async def test_pause_button_says_nothing_to_the_client(stage: Stage) -> None:
    """«⏸ Пауза»: тихая остановка — клиент ничего не получает (ТЗ, Блок 7)."""
    anna = await handed_over_client(stage)
    before = list(anna.inbox)

    await stage.yulia.presses("Пауза")

    assert anna.inbox == before, "клиент получил сообщение о паузе"
    assert stage.contact(anna)["paused"] is True


async def test_yulias_manual_status_is_not_overridden_by_ai(stage: Stage) -> None:
    """Приоритет решения Юлии: AI не переопределяет статус, выставленный ею."""
    anna = await handed_over_client(stage)
    await stage.yulia.presses("Прогрев")  # решение Юлии: warm, by=yulia

    stage.openai.script(
        "qualify", qualification(status="cold", confidence=95, bot_response="Понимаю.")
    )
    await anna.says("Наверное, мне это пока не нужно")

    assert stage.contact(anna)["status"] == "warm", "AI перебил решение Юлии"


async def test_stranger_cannot_press_yulias_buttons(stage: Stage) -> None:
    """Чужое нажатие админ-кнопки: отказ и никаких изменений в CRM."""
    anna = await handed_over_client(stage)
    intruder = stage.client(name="Чужой", username="stranger")

    await stage.press_raw(intruder, f"adm:reject:{anna.id}")

    assert "Недоступно" in stage.session.callback_answers
    assert stage.contact(anna)["status"] == "hot", "чужак изменил статус клиента"


async def test_stranger_cannot_run_admin_commands(stage: Stage) -> None:
    """Admin-команда от постороннего: явный отказ, данные не раскрываются."""
    intruder = stage.client(name="Чужой", username="stranger")

    await intruder.says("/stats")

    assert intruder.last == "Команда недоступна."


async def test_yulia_reviews_and_manages_a_client(stage: Stage) -> None:
    """Обычный набор команд Юлии по одному клиенту — от карточки до заметки."""
    anna = await handed_over_client(stage)

    await stage.yulia.says(f"/info {anna.id}")
    assert "ЗАПРОС" in stage.yulia.last and "ИСТОРИЯ КАСАНИЙ" in stage.yulia.last

    await stage.yulia.says(f"/timeline {anna.id}")
    assert "—" in stage.yulia.last

    await stage.yulia.says(f"/status {anna.id} in_progress")
    assert "Статус изменён" in stage.yulia.last
    assert stage.contact(anna)["status"] == "in_progress"

    await stage.yulia.says(f"/note {anna.id} Договорились на вторник")
    assert stage.yulia.last == "Заметка добавлена."
    assert "Договорились на вторник" in stage.contact(anna)["notes"]

    await stage.yulia.says("/hot")
    await stage.yulia.says("/tasks")
    assert "Открытые задачи" in stage.yulia.last

    await stage.yulia.says("/stats")
    assert "Всего контактов" in stage.yulia.last


async def test_pause_and_resume_switch_the_automation(stage: Stage) -> None:
    """/pause и /resume действительно останавливают и возвращают бота."""
    anna = await handed_over_client(stage)
    await stage.yulia.says(f"/resume {anna.id}")
    await stage.yulia.says(f"/assign_ai {anna.id}")
    assert stage.contact(anna)["paused"] is False

    await stage.yulia.says(f"/pause {anna.id}")
    assert stage.contact(anna)["paused"] is True

    await anna.says("Я всё ещё жду")
    assert anna.last == texts.ALREADY_WITH_YULIA, "бот отвечает клиенту на паузе"


async def test_unknown_client_id_is_reported_not_crashed(stage: Stage) -> None:
    """Команда с чужим id: понятный ответ вместо ошибки."""
    await stage.yulia.says("/info 12345")
    assert "не найден" in stage.yulia.last


async def test_health_shows_all_systems(stage: Stage) -> None:
    """/health: доступность Airtable, OpenAI, база знаний, логи, uptime."""
    await stage.yulia.says("/health")

    report = stage.yulia.last
    for marker in ("Airtable:", "OpenAI:", "База знаний:", "Логи:", "Uptime:"):
        assert marker in report, f"в /health нет строки {marker!r}"


# ── Анкета «Точка сбоя» ──


async def test_questionnaire_full_cycle(stage: Stage) -> None:
    """Полный цикл анкеты: команда Юлии → семь вопросов → согласие → отчёт.

    Ключевое: анкета доходит до клиента, для которого автоматика остановлена.
    Без исключения в pause_check ответы проглатывались бы как сообщения
    переданного клиента, и Юлия не получила бы ничего.
    """
    anna = await handed_over_client(stage)
    assert stage.contact(anna)["paused"] is True, "исходная точка — клиент на паузе"
    stage.openai.script("form", form_analysis())

    await stage.yulia.says(f"/anketa {anna.id}")
    assert "Анкета отправлена" in stage.yulia.last

    assert anna.inbox[-2] == texts.QUESTIONNAIRE_INTRO
    assert anna.last == texts.Q_FORM_1

    answers = [
        "Повторяется один и тот же сценарий",
        "Сейчас, потому что стало невыносимо",
        "Беспокоит ощущение бессилия",
        "Пробовала терапию, помогло частично",
        "Хочу понять механизм",
        "Важно, потому что это влияет на семью",
        "Важно знать, что я уже работала с психологом",
    ]
    expected = [
        texts.Q_FORM_2,
        texts.Q_FORM_3,
        texts.Q_FORM_4,
        texts.Q_FORM_5,
        texts.Q_FORM_6,
        texts.Q_FORM_7,
        texts.Q_FORM_CONSENT,
    ]
    for answer, question in zip(answers, expected):
        await anna.says(answer)
        assert anna.last == question, f"после ответа {answer!r} задан не тот вопрос"

    assert anna.buttons == ["Да", "Нет"]
    await anna.presses("Да")

    assert anna.last == texts.QUESTIONNAIRE_DONE
    report = stage.yulia.last
    assert "📋 АНКЕТА ЗАПОЛНЕНА" in report
    assert "Согласие на запись встречи: да" in report
    for section in ("ПОЧЕМУ ИМЕННО СЕЙЧАС", "ЧТО УЖЕ ПРОБОВАЛ(А)", "ЖЕЛАЕМЫЙ РЕЗУЛЬТАТ"):
        assert section in report, f"в отчёте нет раздела {section!r}"
    assert "Пробовала терапию, помогло частично" in report

    saved = stage.table("Diagnostics")
    assert len(saved) == 1
    assert saved[0]["recording_consent"] is True
    stored = json.loads(saved[0]["questionnaire"])
    assert len(stored) == 7 and stored[0]["answer"] == answers[0]


async def test_questionnaire_report_survives_openai_outage(stage: Stage) -> None:
    """OpenAI недоступен — отчёт всё равно уходит: анкета не пропадает."""
    anna = await handed_over_client(stage)
    stage.openai.script("form", 500, 500)

    await stage.yulia.says(f"/anketa {anna.id}")
    for text in ("раз", "два", "три", "четыре", "пять", "шесть", "семь"):
        await anna.says(text)
    await anna.presses("Нет")

    report = stage.yulia.last
    assert "📋 АНКЕТА ЗАПОЛНЕНА" in report
    assert "Согласие на запись встречи: нет" in report
    assert "AI-разбор анкеты недоступен" in report
    assert stage.table("Diagnostics"), "анкета не сохранена при сбое OpenAI"


async def test_second_anketa_needs_an_explicit_force(stage: Stage) -> None:
    """Повторная анкета не стирает прежние ответы молча."""
    anna = await handed_over_client(stage)
    stage.openai.script("form", form_analysis())

    await stage.yulia.says(f"/anketa {anna.id}")
    for text in ("раз", "два", "три", "четыре", "пять", "шесть", "семь"):
        await anna.says(text)
    await anna.presses("Да")

    await stage.yulia.says(f"/anketa {anna.id}")
    assert "уже есть заполненная анкета" in stage.yulia.last

    await stage.yulia.says(f"/anketa_force {anna.id}")
    assert "отправлена повторно" in stage.yulia.last
    assert anna.last == texts.Q_FORM_1


async def test_questionnaire_to_a_blocked_client_is_reported(stage: Stage) -> None:
    """Клиент заблокировал бота: Юлия узнаёт об этом, а не думает, что всё ок."""
    anna = await handed_over_client(stage)
    stage.session.blocked.add(anna.id)

    await stage.yulia.says(f"/anketa {anna.id}")

    assert "Не удалось отправить анкету" in stage.yulia.last


# ── Комментарии под постами ──


async def test_comment_becomes_a_lead_and_yulia_publishes_the_reply(stage: Stage) -> None:
    """Комментарий → анализ → карточка Юлии → публикация ответа от бота.

    Правило MVP: никакой автопубликации — ответ уходит только после нажатия.
    """
    maria = stage.client(name="Мария Иванова", username="maria")
    stage.openai.script("comment", comment_analysis())

    await stage.comment_in_group(maria, "Как раз про меня. Не понимаю, почему повторяется")

    card = stage.yulia.last
    assert "💬 ПОТЕНЦИАЛЬНЫЙ КЛИЕНТ В КОММЕНТАРИЯХ" in card
    assert "Мария Иванова" in card and "ПРЕДЛОЖЕННЫЙ ОТВЕТ" in card
    assert stage.yulia.buttons == ["✅ Опубликовать", "✏️ Изменить", "⏭ Пропустить"]

    published_before = [m for m in stage.session.messages if m["chat_id"] == GROUP_ID]
    assert published_before == [], "ответ опубликован без утверждения Юлией"

    await stage.yulia.presses("Опубликовать")

    published = [m for m in stage.session.messages if m["chat_id"] == GROUP_ID]
    assert len(published) == 1
    assert published[0]["text"] == comment_analysis()["suggested_reply"]
    assert published[0]["reply_to"] is not None, "ответ опубликован не реплаем"

    comment = stage.table("Comments")[0]
    assert comment["reply_status"] == "sent" and comment["processed"] is True
    # Комментатор заведён контактом с правильным источником
    assert stage.contact(maria)["source"] == "telegram_comment"
    assert "comment" in stage.touches(maria)


async def test_yulia_edits_the_suggested_reply_before_publishing(stage: Stage) -> None:
    """«✏️ Изменить»: публикуется текст Юлии, а не предложение модели."""
    maria = stage.client(name="Мария Иванова", username="maria")
    stage.openai.script("comment", comment_analysis())
    await stage.comment_in_group(maria, "Очень откликается, что делать?")

    await stage.yulia.presses("Изменить")
    await stage.yulia.says("Мария, напишите мне в личные сообщения — разберём вашу ситуацию.")

    published = [m for m in stage.session.messages if m["chat_id"] == GROUP_ID]
    assert len(published) == 1
    assert published[0]["text"].startswith("Мария, напишите мне")
    assert stage.table("Comments")[0]["reply_status"] == "edited"


async def test_yulia_skips_a_comment(stage: Stage) -> None:
    """«⏭ Пропустить»: ничего не публикуется, комментарий закрыт."""
    maria = stage.client(name="Мария Иванова", username="maria")
    stage.openai.script("comment", comment_analysis())
    await stage.comment_in_group(maria, "Спасибо за пост!")

    await stage.yulia.presses("Пропустить")

    assert [m for m in stage.session.messages if m["chat_id"] == GROUP_ID] == []
    comment = stage.table("Comments")[0]
    assert comment["reply_status"] == "skipped" and comment["processed"] is True


async def test_ordinary_comment_does_not_disturb_yulia(stage: Stage) -> None:
    """Обычный отклик без запроса: в CRM пишется, Юлию не дёргаем."""
    maria = stage.client(name="Мария Иванова", username="maria")
    stage.openai.script(
        "comment",
        comment_analysis(is_potential_client=False, needs_reply=False, suggested_reply=None),
    )

    await stage.comment_in_group(maria, "❤️")

    assert stage.yulia.inbox == []
    assert stage.table("Comments"), "комментарий не сохранён"
    assert stage.table("Comments")[0]["processed"] is True


async def test_two_comments_under_one_post_do_not_duplicate_the_post(stage: Stage) -> None:
    """Дедупликация постов: две записи под одним постом — одна строка в Posts."""
    maria = stage.client(name="Мария Иванова", username="maria")
    oleg = stage.client(name="Олег Смирнов", username="oleg")
    stage.openai.script(
        "comment",
        comment_analysis(is_potential_client=False, needs_reply=False),
        comment_analysis(is_potential_client=False, needs_reply=False),
    )

    await stage.comment_in_group(maria, "Отличный текст")
    await stage.comment_in_group(oleg, "Согласен")

    posts = stage.table("Posts")
    assert len(posts) == 1, f"пост продублирован: {len(posts)} записей"
    assert posts[0]["comments_count"] == 2


async def test_comment_analysis_failure_still_saves_and_warns(stage: Stage) -> None:
    """Сбой AI на комментарии: запись сохраняется, Юлия предупреждена."""
    maria = stage.client(name="Мария Иванова", username="maria")
    stage.openai.script("comment", 500, 500)

    await stage.comment_in_group(maria, "Не понимаю, почему это со мной")

    assert "Ошибка обработки AI" in stage.yulia.last
    assert stage.table("Comments"), "комментарий потерян при сбое AI"
