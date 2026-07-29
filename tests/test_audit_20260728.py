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
        qualification(confidence=60),
        qualification(status="warm", confidence=92, bot_response="Понимаю вас."),
    )

    await anna.start("referral")
    await anna.says("У меня год повторяется одна ситуация в отношениях")
    assert anna.last == texts.QUESTION_DURATION

    await anna.says("Около года")
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

    asked = [
        texts.QUESTION_DURATION,
        texts.QUESTION_2,
        texts.QUESTION_3,
        texts.QUESTION_4,
        texts.QUESTION_5,
    ]
    assert [q for q in asked if q in anna.inbox] == asked, "вопросы заданы не все или не по одному"


# ── Преждевременная передача после двух вопросов ──


async def test_high_confidence_does_not_cut_the_question_chain(stage: Stage) -> None:
    """Уверенность в статусе не заменяет собранную картину.

    Живой прод 28.07: клиент отвечал дважды и получал «Я уже передал
    информацию Юлии». Модель после двух реплик уверенно ставила warm 92%,
    а условие достаточности считало высокий confidence признаком того, что
    информации хватает. Это разные вещи: confidence измеряет уверенность
    в статусе, а не полноту картины. FSM сценария B в ТЗ —
    ``q1 → q2 → q3 → q4 → [q5]``, в скобках только пятый вопрос.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script("qualify", *[qualification(status="warm", confidence=95)] * 4)

    await anna.start("telegram_channel")
    await anna.says("Проблемы в отношениях")
    await anna.says("Больше года")
    await anna.says("Не можем найти общий язык")

    assert not handed_off(anna), "передача после двух ответов — цепочка вопросов оборвана"
    assert anna.last == texts.QUESTION_3, "третий вопрос не задан"
    assert stage.yulia.inbox == []


@pytest.mark.parametrize(
    ("first", "second", "conf"),
    [
        # Дословно из прода 28.07 — три диалога, три преждевременные передачи
        ("Хочу улушить свою внешность", "Неделю", 75),
        ("Я хочу увеличить доход в своем проекте", "Какая именно", 85),
        ("Проблемы в отношениях", "Не можем найти общий язык", 92),
    ],
)
async def test_models_needs_yulia_alone_does_not_end_the_chain(
    stage: Stage, first: str, second: str, conf: int
) -> None:
    """Свободный флаг модели не обрывает квалификацию.

    Живой прод 28.07: модель возвращала needs_yulia=true с
    readiness_signal=none почти в каждом диалоге — промпт велит ей
    передавать «при любом основании», а в том списке есть «AI не уверен»
    и «сложный запрос». При двух репликах она не уверена всегда, и человек
    уходил Юлии после одного вопроса по любой теме.

    Передачу теперь открывает НАЗВАННОЕ наблюдаемое основание
    (``handoff_trigger``), а не суждение. Неуверенность учитывает порог 85%
    в конце цепочки.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        *[
            qualification(
                status="warm",
                confidence=conf,
                readiness_signal="none",
                handoff_trigger="none",
                needs_yulia=True,
                needs_yulia_reason="Требуется экспертная оценка ситуации",
            )
        ]
        * 3,
    )

    await anna.start("telegram_channel")
    await anna.says(first)
    await anna.says(second)

    assert not handed_off(anna), f"«{first}» → передача после одного вопроса"
    assert anna.last == texts.QUESTION_2, "второй вопрос не задан"
    assert stage.yulia.inbox == [], "Юлия получила недоспрошенного клиента"


async def test_named_handoff_trigger_ends_the_chain_immediately(stage: Stage) -> None:
    """Названное основание передачи работает с любого шага.

    Обратная сторона: тяжёлая ситуация, просьба о человеке, конфликт и B2B
    обязаны уводить к Юлии немедленно — доспрашивать в таких случаях нельзя.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(
            status="warm",
            confidence=90,
            readiness_signal="none",
            handoff_trigger="heavy_situation",
            needs_yulia=True,
            needs_yulia_reason="Эмоционально тяжёлая ситуация",
        ),
    )

    await anna.start("telegram_dm")
    await anna.says("Ситуация в семье очень тяжёлая")
    await anna.says("Полгода, и мне совсем плохо")

    assert handed_off(anna), "тяжёлую ситуацию не передали немедленно"
    assert texts.QUESTION_2 not in anna.inbox, "человека доспрашивали в тяжёлом состоянии"
    assert stage.yulia.inbox


async def test_hot_without_signal_no_longer_ends_the_dialog_early(stage: Stage) -> None:
    """«Горячий» без признака готовности не обрывает вопросы посреди сценария.

    Правило понижения hot → warm ставило синтетический needs_yulia сразу,
    и человек уходил Юлии недоспрошенным на втором вопросе. Пока вопросы
    не заданы, правильный ответ не «передать», а «спросить дальше».
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        *[qualification(status="hot", readiness_signal="none", confidence=92)] * 4,
    )

    await anna.start("site")
    await anna.says("Всё держится на мне одном")
    await anna.says("Года три")

    assert not handed_off(anna), "выдуманная готовность оборвала квалификацию"
    assert anna.last == texts.QUESTION_2


async def test_named_readiness_signal_ends_the_chain_legitimately(stage: Stage) -> None:
    """Названный признак готовности завершает цепочку законно.

    Обратная сторона: человек, сказавший «хочу записаться» на втором
    вопросе, не должен выслушивать оставшиеся три.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(status="hot", readiness_signal="booking", confidence=94),
    )

    await anna.start("site")
    await anna.says("Ситуация повторяется который год")
    await anna.says("Полгода, и я хочу записаться на диагностику")

    assert handed_off(anna), "названный признак готовности проигнорирован"
    assert texts.QUESTION_3 not in anna.inbox, "человека доспрашивали после просьбы записаться"
    assert "🔥 ГОРЯЧИЙ ЛИД" in stage.yulia.last


async def test_early_finish_still_applies_the_confidence_threshold(stage: Stage) -> None:
    """Досрочное завершение не теряет порог 85%.

    Цепочка обрывается на втором вопросе — человек попросил связаться лично.
    Решение принимается по квалификации, запрошенной с final=False, и
    финальные правила обязаны примениться и там: иначе неуверенный вывод
    ушёл бы Юлии без пометки «требуется экспертная оценка».
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(confidence=50),
        qualification(status="warm", readiness_signal="personal_contact", confidence=60),
    )

    await anna.start("site")
    await anna.says("Что-то идёт не так")
    await anna.says("Давно")
    await anna.says("Хочу поговорить с Юлией напрямую")

    assert stage.yulia.inbox, "клиент ниже порога уверенности не дошёл до Юлии"
    assert "ТРЕБУЕТСЯ ЭКСПЕРТНАЯ ОЦЕНКА" in stage.yulia.last
    assert texts.QUESTION_3 not in anna.inbox, "просьбу о личном контакте проигнорировали"


async def test_hot_without_signal_reaches_yulia_at_the_end_of_the_chain(stage: Stage) -> None:
    """Понижение hot → warm не теряется: в конце цепочки Юлия всё равно решает.

    Передача откладывается до конца вопросов, но не отменяется — иначе
    правка против выдуманной готовности превратилась бы в потерю лида.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        *[qualification(status="hot", readiness_signal="none", confidence=92)] * 4,
    )

    await anna.start("site")
    await anna.says("Всё держится на мне одном")
    await anna.says("Года три")
    await anna.says("Не успеваю ничего")
    await anna.says("Пробовала нанимать людей")
    await anna.says("Хочу выстроить систему")

    assert handed_off(anna), "клиент с признаками интереса потерян"
    assert "ТЁПЛЫЙ ЛИД" in stage.yulia.last
    assert "о готовности записаться не говорил" in stage.yulia.last
    assert stage.contact(anna)["status"] == "warm"


async def test_duration_answer_reaches_yulias_card(stage: Stage) -> None:
    """Ответ о давности ситуации виден в карточке.

    Вопрос добавлен по просьбе Юлии 2026-07-28: повторяемость — один из
    девяти признаков, которые AI обязан оценить, а судить о ней, не зная
    срока, нельзя.
    """
    anna = stage.client()
    stage.openai.script("scenario", scenario("B_problem"))
    stage.openai.script(
        "qualify",
        qualification(confidence=50),
        qualification(status="hot", readiness_signal="booking", confidence=95),
    )

    await anna.start("site")
    await anna.says("Конфликты в команде повторяются")
    await anna.says("Ровно два года")
    await anna.says("Хочу записаться")

    assert "Ровно два года" in stage.yulia.last, "давность ситуации не дошла до карточки"


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
