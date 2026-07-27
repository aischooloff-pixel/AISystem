"""Регрессионные тесты аудита перед сдачей (2026-07-27).

Каждый тест закрывает находку из ``docs/AUDIT_2026-07-27.md`` и падает
на коде, каким он был до исправления.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from bot import texts
from bot.utils import validators
from tests.test_ai import KNOWLEDGE, FakeOpenAI, make_qualification, make_service

KNOWLEDGE_DIR = Path("bot/knowledge")


# ── Находка 1: правило смещения (ТЗ, Блок 4) ──


async def test_shift_rule_promotes_warm_to_hot() -> None:
    """warm при высокой готовности и срочности → hot и передача Юлии.

    До исправления правило существовало только просьбой в промпте: модель
    могла вернуть warm с двумя high, и горячий клиент молча оставался
    в прогреве, не доходя до Юлии.
    """
    # Признак готовности назван: без него обе оси высокими быть не могут
    # и правило смещения не имеет права сработать (см. следующий тест)
    payload = make_qualification(
        status="warm",
        readiness="high",
        urgency="high",
        readiness_signal="deadline",
        needs_yulia=False,
        confidence=95,
    )
    fake = FakeOpenAI([json.dumps(payload, ensure_ascii=False)])
    result = await make_service(fake).qualify("диалог", knowledge=KNOWLEDGE)

    assert result is not None
    assert result["status"] == "hot", "правило смещения не сработало"
    assert result["needs_yulia"] is True
    assert "смещени" in result["needs_yulia_reason"].lower()


@pytest.mark.parametrize(
    "readiness,urgency",
    [("high", "medium"), ("medium", "high"), ("low", "low")],
)
async def test_shift_rule_does_not_fire_on_single_axis(readiness: str, urgency: str) -> None:
    """Смещение требует ОБЕИХ высоких осей — одной мало, иначе в hot
    уезжал бы каждый второй тёплый клиент."""
    payload = make_qualification(
        status="warm",
        readiness=readiness,
        urgency=urgency,
        readiness_signal="deadline",
        needs_yulia=False,
        confidence=95,
    )
    fake = FakeOpenAI([json.dumps(payload, ensure_ascii=False)])
    result = await make_service(fake).qualify("диалог", knowledge=KNOWLEDGE)

    assert result is not None
    assert result["status"] == "warm"


# ── Находка 6: перефразированные стоп-фразы ──


def test_paraphrased_stop_phrases_are_caught() -> None:
    """Модель, нарушившая промпт, скажет своими словами, а не цитатой.

    Все варианты ниже проходили дословную проверку насквозь.
    """
    for evasion in (
        "Я гарантирую вам результат",
        "Гарантирую результат",
        "Я гарантирую хороший результат",
        "Я точно знаю причину вашей проблемы",
        "Вы сами в этом виноваты",
        "После диагностики у вас всё изменится",
        "Без моей помощи вы просто не справитесь",
        "Это единственный способ решить вашу ситуацию",
    ):
        assert validators.find_stop_phrase(evasion), f"пропущена перефразировка: {evasion!r}"


def test_stop_patterns_do_not_fire_on_allowed_answers() -> None:
    """Разрешённые формулировки из документов Юлии не должны отбраковываться.

    Первые две — дословные ответы из FAQ и «Работы с сомнениями»; если
    смысловой шаблон их ловит, бот замолчит на штатном вопросе о гарантиях.
    """
    for allowed in (
        "Никто не может гарантировать конкретный результат.",
        "Юлия помогает исследовать систему и сопровождать процесс изменений.",
        "Диагностика помогает понять механизм происходящего.",
        "Формат определяется индивидуально.",
        "После диагностики появляется более ясное понимание происходящего.",
    ):
        assert validators.find_stop_phrase(allowed) is None, f"ложное срабатывание: {allowed!r}"


# ── Находка 3 (раздел 13): suggested_reply = null ──


def test_comment_analysis_accepts_null_suggested_reply() -> None:
    """needs_reply=false → отвечать не на что, null допустим.

    До исправления такой ответ модели считался невалидным и гнал
    комментарий на ручную передачу — 23% комментариев на живом трафике.
    """
    data = {
        "topic": "тема",
        "emotion": "neutral",
        "key_problem": None,
        "interest_level": 2,
        "is_potential_client": False,
        "needs_reply": False,
        "request_detected": None,
        "suggested_reply": None,
        "should_invite_to_bot": False,
        "confidence": 80,
    }
    assert validators.validate_comment_analysis(data) == []


def test_comment_analysis_still_rejects_wrong_type() -> None:
    """Послабление касается только null: число или список по-прежнему брак."""
    for wrong in (42, ["ответ"], {"text": "ответ"}):
        data = {
            "topic": "тема",
            "emotion": "neutral",
            "key_problem": None,
            "interest_level": 2,
            "is_potential_client": False,
            "needs_reply": True,
            "request_detected": None,
            "suggested_reply": wrong,
            "should_invite_to_bot": False,
            "confidence": 80,
        }
        assert validators.validate_comment_analysis(data), f"пропущен тип {type(wrong)}"


# ── Находка 4: FAQ перенесён полностью ──


def test_faq_contains_all_nine_sections() -> None:
    """До исправления в faq.md были только названия вопросов без ответов."""
    faq = (KNOWLEDGE_DIR / "faq.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## Раздел (\d)", faq, re.M)
    assert [int(s) for s in sections] == list(range(1, 10)), "перенесены не все 9 разделов"
    assert "НЕПОЛНАЯ ВЕРСИЯ" not in faq, "остался маркер заглушки"


def test_faq_contains_answers_not_only_questions() -> None:
    """Ключевые факты из документа — именно ответы, а не перечень тем.

    Формулировки взяты из редакции FAQ, присланной 2026-07-27 (девять файлов
    «Раздел N»); прежняя редакция из PDF была короче и с другими разделами.
    """
    faq = re.sub(r"\s+", " ", (KNOWLEDGE_DIR / "faq.md").read_text(encoding="utf-8"))
    for fact in (
        "продолжительностью до 60 минут",  # сколько длится диагностика
        "отделить факты от интерпретаций",  # суть подхода
        "Более 15 лет",  # опыт Юлии
        "не рассчитывает стоимость самостоятельно",  # правило про цены
        "формате онлайн",  # как проходит работа
    ):
        assert fact.lower() in faq.lower(), f"в faq.md нет ответа: {fact!r}"


# ── Пакет документов заказчика от 2026-07-27 ──


def test_faq_has_nine_sections_of_the_new_edition() -> None:
    """Присланная 2026-07-27 редакция FAQ — другая, чем прежняя из PDF:
    девять разделов с иными заголовками и в семь раз больший объём.
    """
    faq = (KNOWLEDGE_DIR / "faq.md").read_text(encoding="utf-8")
    titles = re.findall(r"^## Раздел \d+\. (.+)$", faq, re.M)
    assert len(titles) == 9, f"разделов не девять: {titles}"
    assert titles[0].startswith("Кто такая Юлия"), titles[0]
    assert "не подойти" in titles[6], titles[6]
    assert len(faq) > 40_000, "FAQ подозрительно короткий — вероятно, обрезан"


@pytest.mark.parametrize(
    "filename,marker",
    [
        ("tone_of_voice.md", "Tone of Voice"),
        ("glossary.md", "Системная диагностика"),
        ("cases.md", "Кейс 1"),
        ("routes.md", "AI не продаёт услуги"),
        ("content.md", "публикации Telegram"),
        ("brand_architecture.md", "Конституция бренда"),
    ],
)
def test_new_knowledge_documents_transferred(filename: str, marker: str) -> None:
    """Каждый документ пакета на месте и содержит опорную формулировку."""
    path = KNOWLEDGE_DIR / filename
    assert path.is_file(), f"нет файла: {filename}"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 3_000, f"{filename} подозрительно короткий: {len(text)}"
    assert marker in text, f"в {filename} нет опорной формулировки {marker!r}"


def test_cases_carry_the_no_analogy_warning() -> None:
    """Кейсы нельзя предъявлять как доказательство: похожие запросы имеют
    разные механизмы. Требование самого документа — теряться не должно."""
    cases = re.sub(r"\s+", " ", (KNOWLEDGE_DIR / "cases.md").read_text(encoding="utf-8"))
    assert "не используется" in cases and "доказательств" in cases


# ── Находка 5: вопросы анкеты дословно ──


def test_questionnaire_questions_match_document() -> None:
    """Вторые предложения вопросов 4–7 задают глубину ответа и питают
    предвстречный отчёт Юлии — до исправления они были обрезаны."""
    for question, tail in (
        (texts.Q_FORM_4, "для решения этой ситуации"),
        (texts.Q_FORM_5, "Да, именно этого я хотел(а)"),
        (texts.Q_FORM_6, "в вашей жизни, отношениях или бизнесе"),
        (texts.Q_FORM_7, "поможет лучше понять вашу ситуацию"),
    ):
        assert tail in question, f"вопрос обрезан, потеряно: {tail!r}"


# ── Находка 2: UTM из deep link ──


def test_deep_link_utm_is_parsed() -> None:
    """«источник__метка»: источник распознаётся, метка не теряется.

    ТЗ (Часть 3) требует фиксировать UTM при первом касании; до исправления
    параметр сверялся со справочником целиком, и такой deep link уводил
    клиента на кнопки выбора, а метку терял.
    """
    from bot.handlers.start import VALID_SOURCES

    for param, expected_source, expected_utm in (
        ("site__spring2026", "site", "spring2026"),
        ("qr__flyer_msk", "qr", "flyer_msk"),
        ("referral", "referral", ""),
    ):
        source, _, utm = param.partition("__")
        assert source in VALID_SOURCES
        assert source == expected_source
        assert utm == expected_utm


async def test_start_writes_utm_to_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Метка доходит до записи в Contacts, а не только парсится."""
    from bot.handlers import start as start_module

    written: dict = {}

    async def fake_upsert(telegram_id, data):
        written.update(data)
        return {"id": "rec1", "fields": {"telegram_id": telegram_id, **data}}

    async def fake_find_checked(telegram_id):
        return True, None

    async def fake_add_touch(*args, **kwargs):
        return {"id": "tch1"}

    monkeypatch.setattr(start_module.airtable, "upsert_contact", fake_upsert)
    monkeypatch.setattr(start_module.airtable, "find_contact_checked", fake_find_checked)
    monkeypatch.setattr(start_module.airtable, "add_touch", fake_add_touch)

    class FakeState:
        async def set_state(self, *_):
            return None

        async def get_state(self):
            return None

        async def update_data(self, **_):
            return None

        async def get_data(self):
            return {}

    class FakeUser:
        id = 555
        full_name = "Тест"
        username = None

    class FakeMessage:
        text = "/start site__spring2026"
        from_user = FakeUser()

        async def answer(self, *_args, **_kwargs):
            return None

    await start_module.cmd_start(FakeMessage(), FakeState())
    assert written.get("source") == "site"
    assert written.get("utm") == "spring2026", "UTM-метка не записана в контакт"


# ── Находка 3 (раздел 1): next_action_date ──


def test_next_action_date_is_written_with_next_step() -> None:
    """Шаг без срока не попадёт ни в один фильтр Юлии."""
    source = Path("bot/handlers/qualification.py").read_text(encoding="utf-8")
    assert "next_action_date" in source, "next_action_date по-прежнему не пишется"


# ── Служебное: заглушка httpx нужна тестам выше ──


def test_fake_openai_harness_available() -> None:
    assert issubclass(FakeOpenAI, object) and callable(make_service)
    assert isinstance(httpx.MockTransport, type)
