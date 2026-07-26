"""Тесты Блока 8: мониторинг комментариев.

Критерии: пост фиксируется в Posts; AI анализирует комментарий; запись в
Comments; контакт без дублей; касание; счётчики; уведомление Юлии;
все 3 кнопки; автопубликации нет; повторный комментарий — без дубля.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from bot.handlers import comments as com
from bot.services import ai as ai_module
from bot.services import airtable
from bot.states import AdminFlow
from tests.test_block6_qualification import FakeBot, config  # noqa: F401

GROUP_ID = -200  # из config
AUTHOR_ID = 555123


class FakeChat(SimpleNamespace):
    pass


class FakeGroupMessage(SimpleNamespace):
    """Сообщение в discussion group."""


def make_comment_message(text="Это прямо про меня", author_id=AUTHOR_ID):
    post = FakeGroupMessage(
        is_automatic_forward=True,
        text="Почему одни и те же ситуации повторяются",
        caption=None,
        forward_origin=SimpleNamespace(
            message_id=42, chat=SimpleNamespace(username="geikina_channel")
        ),
    )
    return FakeGroupMessage(
        chat=FakeChat(id=GROUP_ID, type="supergroup"),
        is_automatic_forward=False,
        reply_to_message=post,
        from_user=User(id=author_id, is_bot=False, first_name="Мария", username="maria_x"),
        text=text,
        caption=None,
        content_type="text",
        date=datetime(2026, 7, 25, 14, 12, tzinfo=timezone.utc),
        message_id=1001,
    )


class CommentsCRM:
    def __init__(self, monkeypatch):
        self.posts: dict[str, dict] = {}
        self.counters: list[tuple[str, str]] = []
        self.comments: list[dict] = []
        self.contacts: list[tuple[int, dict]] = []
        self.touches: list[tuple] = []
        self.updated_comments: list[tuple[str, dict]] = []
        self.comment_record = {"id": "recC1", "fields": {"suggested_reply": "Спасибо за отклик!"}}

        async def upsert_post(post_id, data):
            self.posts.setdefault(post_id, {}).update(data)
            return {"id": "recP", "fields": {"post_id": post_id}}

        async def increment_post_counter(post_id, field):
            self.counters.append((post_id, field))
            return {}

        async def create_comment(data):
            self.comments.append(data)
            return {"id": "recC1", "fields": data}

        async def upsert_contact(tid, data):
            self.contacts.append((tid, data))
            return {"id": "recK", "fields": {"telegram_id": tid}}

        async def add_touch(tid, type, description, **kwargs):
            self.touches.append((tid, type, description, kwargs))
            return {}

        async def get_comments_by_author(tid):
            return [{"id": "old"}, {"id": "new"}]

        async def get_comment(record_id):
            return self.comment_record

        async def update_comment(record_id, data):
            self.updated_comments.append((record_id, data))
            return {}

        for name, fn in [
            ("upsert_post", upsert_post),
            ("increment_post_counter", increment_post_counter),
            ("create_comment", create_comment),
            ("upsert_contact", upsert_contact),
            ("add_touch", add_touch),
            ("get_comments_by_author", get_comments_by_author),
            ("get_comment", get_comment),
            ("update_comment", update_comment),
        ]:
            monkeypatch.setattr(airtable, name, fn)


class FakeAI:
    def __init__(self, analysis=None):
        self.analysis = analysis

    async def analyze_comment(self, *args, **kwargs):
        return self.analysis


def make_analysis(**over):
    data = {
        "topic": "повторяющиеся ситуации",
        "emotion": "interested",
        "key_problem": "ощущение тупика",
        "interest_level": 4,
        "is_potential_client": True,
        "needs_reply": True,
        "request_detected": "хочет разобраться",
        "suggested_reply": "Мария, спасибо за отклик. Если хотите разобраться — напишите мне.",
        "should_invite_to_bot": True,
        "confidence": 88,
    }
    data.update(over)
    return data


async def test_comment_full_pipeline(monkeypatch, config):
    """Полный конвейер: пост, счётчики, Comments, контакт, касание, карточка."""
    crm = CommentsCRM(monkeypatch)
    monkeypatch.setattr(ai_module, "_service", FakeAI(make_analysis()))
    bot = FakeBot()

    await com.discussion_message(make_comment_message(), bot, config)

    assert "42" in crm.posts  # пост зафиксирован
    assert ("42", "comments_count") in crm.counters
    assert ("42", "potential_clients_count") in crm.counters
    saved = crm.comments[0]
    assert saved["author_telegram_id"] == AUTHOR_ID
    assert saved["reply_status"] == "pending"
    assert saved["is_potential_client"] is True
    assert crm.contacts[0][1]["source"] == "telegram_comment"
    assert crm.touches[0][1] == "comment"
    # Уведомление Юлии с предложенным ответом и кнопками
    assert bot.sent and bot.sent[0][0] == 999
    assert "ПОТЕНЦИАЛЬНЫЙ КЛИЕНТ" in bot.sent[0][1]
    assert "ПРЕДЛОЖЕННЫЙ ОТВЕТ" in bot.sent[0][1]


async def test_non_potential_comment_no_notification(monkeypatch, config):
    """Сценарий 1 из ТЗ: общий комментарий → сохранён, уведомления нет."""
    crm = CommentsCRM(monkeypatch)
    monkeypatch.setattr(
        ai_module,
        "_service",
        FakeAI(make_analysis(is_potential_client=False, needs_reply=False, interest_level=1)),
    )
    bot = FakeBot()

    await com.discussion_message(make_comment_message("Спасибо, интересно!"), bot, config)

    assert bot.sent == []
    assert ("42", "potential_clients_count") not in crm.counters
    assert crm.comments[0]["reply_status"] == "skipped"
    assert crm.comments[0]["processed"] is True


async def test_channel_post_recorded(monkeypatch, config):
    crm = CommentsCRM(monkeypatch)
    monkeypatch.setattr(ai_module, "_service", FakeAI(None))
    post = FakeGroupMessage(
        chat=FakeChat(id=GROUP_ID, type="supergroup"),
        is_automatic_forward=True,
        text="Новый пост о выгорании",
        caption=None,
        forward_origin=SimpleNamespace(
            message_id=77, chat=SimpleNamespace(username="geikina_channel")
        ),
        date=datetime.now(timezone.utc),
    )
    await com.discussion_message(post, FakeBot(), config)
    assert crm.posts["77"]["text"] == "Новый пост о выгорании"
    assert crm.posts["77"]["link"] == "https://t.me/geikina_channel/77"


async def test_other_group_ignored(monkeypatch, config):
    crm = CommentsCRM(monkeypatch)
    message = make_comment_message()
    message.chat = FakeChat(id=-999, type="supergroup")
    await com.discussion_message(message, FakeBot(), config)
    assert crm.posts == {} and crm.comments == []


async def test_ai_failure_saves_comment_and_alerts(monkeypatch, config):
    """Ошибка AI: комментарий сохранён без анализа, Юлия предупреждена."""
    crm = CommentsCRM(monkeypatch)
    monkeypatch.setattr(ai_module, "_service", FakeAI(None))
    bot = FakeBot()

    await com.discussion_message(make_comment_message(), bot, config)

    assert crm.comments and crm.comments[0]["ai_confidence"] == 0
    assert any("Ошибка обработки AI" in text for _, text in bot.sent)


# ── Кнопки ──


def make_callback(data, user_id=999):
    async def answer(*a, **k):
        pass

    async def edit_text(*a, **k):
        pass

    return SimpleNamespace(
        from_user=User(id=user_id, is_bot=False, first_name="Юлия"),
        data=data,
        message=SimpleNamespace(text="УВЕДОМЛЕНИЕ", edit_text=edit_text),
        answer=answer,
    )


def make_state(user_id=999) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def test_publish_button_sends_reply_as_bot(monkeypatch, config):
    """✅ Опубликовать: ответ уходит реплаем в группу, reply_status=sent."""
    crm = CommentsCRM(monkeypatch)
    bot = FakeBot()
    callback = make_callback("cmt:pub:recC1:1001")

    await com.comment_action(callback, bot, config, make_state())

    assert bot.sent == [(GROUP_ID, "Спасибо за отклик!")]
    record_id, data = crm.updated_comments[0]
    assert record_id == "recC1"
    assert data["reply_status"] == "sent" and data["processed"] is True


async def test_edit_button_then_text_publishes_edited(monkeypatch, config):
    """✏️ Изменить: вариант Юлии публикуется, reply_status=edited."""
    crm = CommentsCRM(monkeypatch)
    bot = FakeBot()
    state = make_state()
    callback = make_callback("cmt:edit:recC1:1001")
    await com.comment_action(callback, bot, config, state)
    assert await state.get_state() == AdminFlow.waiting_comment_reply.state

    reply = SimpleNamespace(
        from_user=User(id=999, is_bot=False, first_name="Юлия"),
        text="Мой личный вариант ответа",
        answer=make_callback("x").answer,
    )
    await com.edited_reply_from_yulia(reply, bot, config, state)

    assert bot.sent == [(GROUP_ID, "Мой личный вариант ответа")]
    assert crm.updated_comments[0][1]["reply_status"] == "edited"
    assert await state.get_state() is None


async def test_skip_button(monkeypatch, config):
    crm = CommentsCRM(monkeypatch)
    callback = make_callback("cmt:skip:recC1:1001")
    await com.comment_action(callback, FakeBot(), config, make_state())
    assert crm.updated_comments[0][1]["reply_status"] == "skipped"


async def test_comment_buttons_denied_for_non_admin(monkeypatch, config):
    crm = CommentsCRM(monkeypatch)
    bot = FakeBot()
    callback = make_callback("cmt:pub:recC1:1001", user_id=12345)
    await com.comment_action(callback, bot, config, make_state(12345))
    assert bot.sent == [] and crm.updated_comments == []


async def test_no_autopublish_anywhere(monkeypatch, config):
    """Критерий: автопубликации нет — конвейер комментария сам ничего
    не постит в группу, только уведомляет Юлию."""
    CommentsCRM(monkeypatch)
    monkeypatch.setattr(ai_module, "_service", FakeAI(make_analysis()))
    bot = FakeBot()
    await com.discussion_message(make_comment_message(), bot, config)
    group_messages = [chat_id for chat_id, _ in bot.sent if chat_id == GROUP_ID]
    assert group_messages == []
