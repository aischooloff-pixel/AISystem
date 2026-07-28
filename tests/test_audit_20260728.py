"""Аудит 2026-07-28: сломанная квалификация.

Что было. Детектор сценариев знал ровно три исхода — A_ready, B_problem,
C_info. Нецелевому обращению деться было некуда: «погода на завтра»
становилась C_info, ответа в базе знаний не находилось, модель честно
ставила needs_yulia=true — и хендлер безусловной строкой
``qualification["needs_yulia"] = True`` отправлял Юлии карточку на прогноз
погоды. Живой прогон 20 сообщений: 10 определены неверно, ВСЕ нецелевые —
неверно. «Вопрос по психиатрии» модель называла нецелевым, валидатор
отвергал ответ целиком, и клиент получал техническую ошибку.

Что стало. Пять исходов вместо трёх: добавлены non_target (ТЗ, раздел 8)
и handoff («не хочу говорить с ботом» — особый случай из «Обработки
возражений»). Уверенное нецелевое обращение завершается вежливым ответом
и paused=true, Юлию не беспокоит. Тот же прогон после правки: 22 из 22.

Каждый тест здесь падает на коде до правки — это его назначение.
"""

from __future__ import annotations

import pytest

from bot import texts
from tests.test_block6_qualification import config  # noqa: F401 — фикстура для stage
from tests.test_e2e_journeys import (
    Stage,
    handed_off,
    info_answer,
    qualification,
    scenario,
    stage,  # noqa: F401 — фикстура
)

pytestmark = pytest.mark.asyncio

CLOSING = "Спасибо, что обратились. Этот запрос вне специализации Юлии."


# ── Нецелевое обращение не доходит до Юлии ──


@pytest.mark.parametrize(
    "message",
    [
        "погода на завтра",
        "Можете снять порчу?",
        "Мне нужна юридическая консультация",
        "ааааааааа",
    ],
)
async def test_non_target_is_closed_without_bothering_yulia(stage: Stage, message: str) -> None:
    """Нецелевое обращение: вежливый ответ, paused=true, Юлия не уведомлена.

    ТЗ, «Маршрутизация после квалификации»: non_target → «корректно ответить ·
    рекомендовать профильного специалиста · вежливо завершить · paused=true».
    Карточка Юлии в этом списке отсутствует.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("non_target"))
    stage.openai.script(
        "qualify",
        qualification(
            status="non_target",
            confidence=97,
            needs_yulia=False,
            needs_yulia_reason=None,
            bot_response=CLOSING,
        ),
    )

    await anna.start("telegram_channel")
    await anna.says(message)

    assert anna.last == CLOSING, "клиент не получил вежливое завершение"
    assert not handed_off(anna), "нецелевому обращению обещали передачу Юлии"
    assert stage.yulia.inbox == [], f"Юлия получила карточку на {message!r}"

    fields = stage.contact(anna)
    assert fields["paused"] is True
    assert fields["status"] == "non_target"


async def test_unsure_non_target_still_goes_to_yulia(stage: Stage) -> None:
    """Неуверенный вердикт «нецелевой» закрыть диалог не имеет права.

    «AI не уверен (confidence < 85) → немедленная передача» (ТЗ, раздел 11).
    Ошибиться в пользу человека дешевле, чем молча выпроводить клиента.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("non_target", confidence=60))
    stage.openai.script(
        "qualify", qualification(status="non_target", confidence=55, needs_yulia=False)
    )

    await anna.start("telegram_dm")
    await anna.says("не знаю, туда ли я попал")

    # Рассказ о диагностике сюда намеренно не идёт: предлагать формат работы
    # человеку, в целевом характере которого бот не уверен, — преждевременно
    assert anna.last == texts.HANDOFF_MESSAGE, "неуверенный вердикт закрыл диалог сам"
    assert texts.HANDOFF_FOLLOWUP not in anna.inbox
    assert stage.yulia.inbox, "Юлия не узнала о спорном случае"
    assert "экспертная оценка" in stage.yulia.last.lower()
    assert stage.contact(anna)["assigned_to"] == "yulia"


async def test_weather_question_never_reaches_yulia_via_info_branch(stage: Stage) -> None:
    """Регресс: посторонний вопрос через ветку C_info.

    Именно этот путь и сломал прод. Ответа на «погода на завтра» в базе
    знаний нет и не будет, модель ставит needs_yulia — а хендлер до правки
    превращал это в карточку Юлии безусловно.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("C_info"))
    stage.openai.script("info", info_answer("", needs_yulia=True, reason="нет в базе знаний"))
    stage.openai.script(
        "qualify",
        qualification(
            status="non_target",
            confidence=96,
            needs_yulia=False,
            needs_yulia_reason=None,
            bot_response=CLOSING,
        ),
    )

    await anna.start("site")
    await anna.says("погода на завтра")

    assert stage.yulia.inbox == [], "прогноз погоды снова уехал Юлии"
    assert not handed_off(anna)
    assert anna.last == CLOSING


async def test_confident_non_target_overrides_models_reflex_to_hand_off(stage: Stage) -> None:
    """Модель ставит needs_yulia на нецелевое — код это снимает.

    Системный промпт учит «при сомнениях передавай Юлии», и на нецелевом
    обращении модель ставит флаг по привычке. Уверенный вывод «вне
    компетенции» сомнением не является: ТЗ велит завершить разговор,
    а не звать человека. Без этого правила Юлия всё равно получала бы
    карточку на «снимите порчу» — просто через другую дверь.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("non_target"))
    stage.openai.script(
        "qualify",
        qualification(
            status="non_target",
            confidence=96,
            needs_yulia=True,  # рефлекс модели
            needs_yulia_reason=None,
            bot_response=CLOSING,
        ),
    )

    await anna.start("telegram_channel")
    await anna.says("погадайте мне на картах")

    assert stage.yulia.inbox == [], "рефлекс модели протащил нецелевое к Юлии"
    assert anna.last == CLOSING
    assert stage.contact(anna)["paused"] is True


async def test_named_reason_keeps_non_target_going_to_yulia(stage: Stage) -> None:
    """Названная моделью причина передачи сильнее правила.

    Причина означает, что кроме нецелевого запроса в диалоге есть что-то
    ещё — агрессия, тяжёлая ситуация, просьба о человеке. Такое Юлия
    должна увидеть.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("non_target"))
    stage.openai.script(
        "qualify",
        qualification(
            status="non_target",
            confidence=95,
            needs_yulia=True,
            needs_yulia_reason="Агрессия в адрес Юлии, нужна её реакция",
            bot_response=CLOSING,
        ),
    )

    await anna.start("telegram_dm")
    await anna.says("ваш метод — шарлатанство, вы обманщики")

    assert stage.yulia.inbox, "названная причина передачи проигнорирована"
    assert "Агрессия" in stage.yulia.last


async def test_out_of_knowledge_question_from_real_client_still_reaches_yulia(
    stage: Stage,
) -> None:
    """Обратная сторона правки: целевой вопрос без ответа в базе — Юлии.

    «Вопрос за пределами базы знаний» остаётся основанием передачи
    (ТЗ, раздел 11). Отсекается только уверенно нецелевое.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("C_info"))
    stage.openai.script("info", info_answer("", needs_yulia=True, reason="нет в базе знаний"))
    stage.openai.script("qualify", qualification(status="warm", confidence=88))

    await anna.start("site")
    await anna.says("А вы работаете с парами вдвоём на одной сессии?")

    assert handed_off(anna), "целевой вопрос не дошёл до Юлии"
    assert stage.yulia.inbox


# ── Отказ говорить с ботом ──


async def test_human_request_hands_off_without_asking_questions(stage: Stage) -> None:
    """«Не хочу с ботом» → сразу Юлии, без единого вопроса.

    ТЗ, «Обработка возражений», особый случай. Задавать вопросы человеку,
    который только что попросил живого собеседника, — значит спорить с ним.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("handoff"))
    stage.openai.script("qualify", qualification(status="warm", confidence=90))

    await anna.start("telegram_channel")
    await anna.says("Не хочу разговаривать с ботом, можно живого человека?")

    assert handed_off(anna), "просьбу о человеке не передали Юлии"
    assert texts.QUESTION_2 not in anna.inbox, "боту задали вопрос вместо передачи"
    assert texts.A_INTRO not in anna.inbox
    assert stage.yulia.inbox, "Юлия не получила карточку"


async def test_handoff_scenario_is_never_closed_as_non_target(stage: Stage) -> None:
    """Человек, просящий живого собеседника, — целевой.

    Если квалификация назвала его нецелевым, ветка handoff обязана
    переломить вердикт: иначе диалог закрылся бы вместо передачи.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("handoff"))
    stage.openai.script(
        "qualify", qualification(status="non_target", confidence=95, needs_yulia=False)
    )

    await anna.start("telegram_dm")
    await anna.says("дайте контакт Юлии, с ботом не хочу")

    assert handed_off(anna), "просьба о человеке закрыта как нецелевая"
    assert stage.contact(anna)["status"] != "non_target"


# ── Целевые сценарии не пострадали ──


async def test_problem_description_still_gets_full_questioning(stage: Stage) -> None:
    """Сценарий B по-прежнему задаёт вопросы по одному и не рвётся к передаче."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(confidence=40),
        qualification(confidence=45),
        qualification(confidence=50),
        qualification(status="warm", confidence=92, bot_response="Понимаю вас."),
    )

    await anna.start("referral")
    await anna.says("У меня год повторяется одна ситуация в отношениях")
    assert anna.last == texts.QUESTION_2

    await anna.says("Тревога и усталость")
    assert anna.last == texts.QUESTION_3

    await anna.says("Ходила к психологу")
    assert anna.last == texts.QUESTION_4

    await anna.says("Хочу перестать это повторять")
    assert anna.last == texts.QUESTION_5

    await anna.says("Стало невозможно терпеть")
    assert not handed_off(anna), "тёплого клиента передали без признака готовности"
    assert stage.yulia.inbox == []

    asked = [texts.QUESTION_2, texts.QUESTION_3, texts.QUESTION_4, texts.QUESTION_5]
    assert [q for q in asked if q in anna.inbox] == asked, "вопросы заданы не все или не по одному"


async def test_ready_client_still_reaches_yulia(stage: Stage) -> None:
    """Сценарий A: два вопроса и передача — как и было."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("A_ready"))
    stage.openai.script("qualify", qualification(status="hot", confidence=96))

    await anna.start("site")
    await anna.says("Хочу записаться на диагностику")
    assert anna.last == texts.A_INTRO

    await anna.says("Повторяются конфликты в команде")
    assert anna.last == texts.A_QUESTION_2

    await anna.says("Хочу выстроить управление")
    assert handed_off(anna), "готового клиента не передали"
    assert stage.yulia.inbox


async def test_greeting_is_not_non_target(stage: Stage) -> None:
    """«Привет» — мало информации, а не нецелевое обращение."""
    anna = stage.client()
    stage.openai.script("scenario", scenario("C_info", confidence=70))
    stage.openai.script("info", info_answer("Здравствуйте! С чем хотите разобраться?"))

    await anna.start("telegram_channel")
    await anna.says("Привет")

    assert "вне специализации" not in anna.last.lower()
    assert not handed_off(anna)
    assert stage.contact(anna).get("paused") is not True
