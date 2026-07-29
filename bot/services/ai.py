"""AI-сервис: OpenAI API для сценария, квалификации и анализа комментариев (Блок 4).

Каждый вызов проходит цепочку: запрос (с retry по таблице ошибок ТЗ) →
парсинг JSON (битый JSON — один retry с уточнением формата) → валидация
схемы → проверка стоп-фраз. Любой окончательный провал возвращает ``None``
или безопасный результат — исключения наверх не выходят, бот не падает.

Логируются каждый запрос к OpenAI и полный ответ (ТЗ, Блок 1).
"""

from __future__ import annotations

import asyncio

import httpx

from bot import texts
from bot.config import Config
from bot.prompts.comment_analyzer import build_comment_prompt
from bot.prompts import qualifier as prompts_qualifier
from bot.prompts.qualifier import build_info_answer_prompt, build_qualification_prompt
from bot.prompts.questionnaire_analyzer import build_questionnaire_prompt
from bot.prompts.scenario_detector import build_scenario_prompt
from bot.prompts.system_prompt import build_system_prompt
from bot.utils.logger import get_app_logger
from bot.utils import validators

OPENAI_URL = "https://api.openai.com/v1"

# Обработка ошибок API — таблица из ТЗ (Блок 4)
TIMEOUT_RETRY_DELAY = 3.0  # таймаут → retry 1 раз через 3 с
RATE_LIMIT_RETRY_DELAY = 10.0  # 429 → retry через 10 с
SERVER_ERROR_RETRY_DELAY = 5.0  # 5xx → retry 1 раз через 5 с

# Уточнение формата при битом JSON (валидация, шаг 1: retry один раз)
JSON_RETRY_NOTE = (
    "Предыдущий ответ не разобрался как JSON или не прошёл проверку схемы. "
    "Ответь ещё раз СТРОГО одним валидным JSON-объектом по заданному формату, "
    "без пояснений, без markdown, без текста вокруг."
)

logger = get_app_logger()


class AIService:
    """Обёртка OpenAI Chat Completions с валидацией по ТЗ."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        *,
        timeout: float = 30.0,
        confidence_threshold: int = 85,
        base_url: str = OPENAI_URL,
        retry_delays: tuple[float, float, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.confidence_threshold = confidence_threshold
        # Заявленная в документации сменяемость модели через OPENAI_MODEL
        # держалась на том, что все модели принимают temperature. Семейство
        # gpt-5 (и рассуждающие o*) принимают только значение по умолчанию и
        # отвечают 400 на 0.2 — смена модели молча убила бы бота целиком:
        # каждый запрос в TECH_ERROR. Пробуем с температурой, а на отказ
        # именно из-за неё переходим на умолчание и больше не пробуем.
        self._send_temperature = True
        delays = retry_delays or (
            TIMEOUT_RETRY_DELAY,
            RATE_LIMIT_RETRY_DELAY,
            SERVER_ERROR_RETRY_DELAY,
        )
        self._timeout_delay, self._rate_delay, self._server_delay = delays
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def ping(self) -> float | None:
        """Доступность OpenAI для /health: задержка в мс или ``None``."""
        import time

        started = time.monotonic()
        try:
            response = await self._http.get("/models", params={"limit": 1})
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        return (time.monotonic() - started) * 1000

    # ── низкий уровень: один вызов Chat Completions с retry по ТЗ ──

    async def _chat(self, messages: list[dict]) -> str | None:
        """Текст ответа модели или ``None`` при окончательной неудаче.

        Retry по таблице ТЗ: таймаут → 1 раз через 3 с; 429 → 1 раз через 10 с;
        5xx → 1 раз через 5 с. Прочие ошибки (401, 400) не ретраятся — это
        не временные сбои.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if self._send_temperature:
            payload["temperature"] = 0.2
        # Логирование запроса полностью (ТЗ, Блок 1). Исключение — системный
        # промпт: в нём вся база знаний (~50К символов, одинакова в каждом
        # запросе), пишем только её размер; остальные реплики — целиком.
        for message in messages:
            if message["role"] == "system":
                logger.info(
                    "OpenAI запрос: model=%s, system=<база знаний, %d символов>",
                    self.model,
                    len(message["content"]),
                )
            else:
                logger.info("OpenAI запрос [%s]: %s", message["role"], message["content"])
        # По одному retry на каждый тип сбоя из таблицы ТЗ — независимо
        # (таймаут после 429 всё ещё имеет свой retry, и наоборот).
        timeout_retried = rate_retried = server_retried = False
        while True:
            try:
                response = await self._http.post("/chat/completions", json=payload)
            except httpx.TimeoutException:
                if timeout_retried:
                    logger.error("OpenAI: повторный таймаут — сдаёмся")
                    return None
                timeout_retried = True
                logger.warning("OpenAI: таймаут, retry через %.0f с", self._timeout_delay)
                await asyncio.sleep(self._timeout_delay)
                continue
            except httpx.HTTPError:
                logger.exception("OpenAI: сетевая ошибка")
                return None

            if response.status_code == 429 and not rate_retried:
                rate_retried = True
                logger.warning("OpenAI: 429, retry через %.0f с", self._rate_delay)
                await asyncio.sleep(self._rate_delay)
                continue
            if response.status_code >= 500 and not server_retried:
                server_retried = True
                logger.warning(
                    "OpenAI: HTTP %d, retry через %.0f с",
                    response.status_code,
                    self._server_delay,
                )
                await asyncio.sleep(self._server_delay)
                continue
            if (
                response.status_code == 400
                and self._send_temperature
                and "temperature" in response.text
            ):
                # Модель принимает только температуру по умолчанию — снимаем
                # параметр и повторяем. Один раз за жизнь процесса: дальше
                # запросы уходят уже без него.
                self._send_temperature = False
                payload.pop("temperature", None)
                logger.info(
                    "Модель %s не принимает temperature — перехожу на значение по умолчанию",
                    self.model,
                )
                continue
            if response.status_code != 200:
                logger.error(
                    "OpenAI: HTTP %d, ответ: %s", response.status_code, response.text[:500]
                )
                return None

            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError, TypeError):
                # TypeError: 200 с choices=null / message=null — тоже не роняет бота
                logger.error("OpenAI: неожиданная структура ответа: %s", response.text[:500])
                return None
            if not isinstance(content, str):
                logger.error("OpenAI: content не строка: %r", content)
                return None
            logger.info("OpenAI ответ (полностью): %s", content)
            return content

    async def _ask_json(
        self,
        task_prompt: str,
        validate,
        *,
        knowledge: str | None = None,
    ) -> dict | None:
        """Запрос → парсинг JSON → валидация схемы (шаги 1–4 из ТЗ).

        Retry с уточнением формата — один раз и ТОЛЬКО если JSON не парсится
        (шаг 1). Распарсился, но не прошёл схему (шаги 2–4) — сразу шаг 6:
        ``None`` → передача Юлии «Ошибка обработки AI». Так требует ТЗ;
        молча расширять retry на провалы схемы нельзя (Часть 0, п. 7).
        """
        messages = [
            {"role": "system", "content": build_system_prompt(knowledge)},
            {"role": "user", "content": task_prompt},
        ]
        for attempt in (1, 2):
            raw = await self._chat(messages)
            if raw is None:
                return None  # сбой API — retry формата не поможет
            data = validators.parse_ai_json(raw)
            if data is None:
                logger.warning("Ответ AI не парсится как JSON (попытка %d)", attempt)
                if attempt == 1:
                    # Шаг 1 валидации: один retry с уточнением формата
                    messages = messages + [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": f"{JSON_RETRY_NOTE}\n\n{task_prompt}"},
                    ]
                    continue
                break
            problems = validate(data)
            if not problems:
                return data
            logger.error("Ответ AI не прошёл схему: %s — передача Юлии", "; ".join(problems))
            return None
        logger.error("Ответ AI не прошёл валидацию после retry — передача Юлии")
        return None

    # ── Функция 1: определение сценария ──

    async def detect_scenario(self, message: str, *, knowledge: str | None = None) -> dict | None:
        """Сценарий A_ready / B_problem / C_info по первому сообщению.

        ``None`` — сбой API или невалидный ответ: хендлер передаёт Юлии
        с пометкой «Ошибка обработки AI».
        """
        return await self._ask_json(
            build_scenario_prompt(message), validators.validate_scenario, knowledge=knowledge
        )

    # ── Функция 2: квалификация ──

    async def qualify(
        self,
        conversation: str | list[dict],
        *,
        final: bool = True,
        knowledge: str | None = None,
        question_topic: str | None = None,
    ) -> dict | None:
        """Квалификация по диалогу. Применяет пост-правила ТЗ:

        - ``confidence < порога`` → ``needs_yulia=true`` с пометкой
          «Требуется экспертная оценка» (Критерии квалификации, п. 9).
          Применяется только при ``final=True``: промежуточные проверки
          достаточности (Блок 6, «завершать квалификацию сразу после
          получения достаточной информации») низкую уверенность трактуют
          как «информации мало, задай следующий вопрос», а не как передачу;
        - стоп-фраза в ``bot_response`` → WARNING, нейтральный шаблон,
          ``needs_yulia=true`` (валидация, шаг 5) — всегда.
        """
        data = await self._ask_json(
            build_qualification_prompt(conversation, question_topic=question_topic),
            validators.validate_qualification,
            knowledge=knowledge,
        )
        if data is None:
            return None

        self._verify_handoff_trigger(data, conversation)

        # Передача требует ПОДТВЕРЖДЁННОГО основания, а не флага модели.
        # Промпт велит ей передавать «при любом основании», и в том списке
        # есть «AI не уверен» и «сложный запрос» — она ставит needs_yulia
        # почти всегда. Живой прогон 29.07: тёплый клиент с confidence 85
        # после четырёх ответов всё равно получал «Передал информацию Юлии»
        # вместо ответа по существу. Ниже флаг взводят только правила ТЗ:
        # порог 85%, стоп-фраза, горячий без признака, названное основание.
        if data.get("needs_yulia") and data.get("handoff_trigger") in (None, "none"):
            logger.info(
                "needs_yulia без подтверждённого основания (%s) — снимаю, решаю по правилам",
                data.get("needs_yulia_reason") or "причина не названа",
            )
            data["needs_yulia"] = False
            data["needs_yulia_reason"] = None
        elif data.get("handoff_trigger") not in (None, "none"):
            data["needs_yulia"] = True

        if data.get("readiness_signal") in (None, "none"):
            # «Критерии квалификации» определяют и статус, и обе оси через
            # наблюдаемые признаки: горячий — просит записаться / спрашивает
            # цену и даты / готов оплатить / просит связаться лично / прямо
            # подтверждает готовность; высокая срочность — просит связаться,
            # готов оплачивать, называет сроки. Если не прозвучало ничего,
            # высокими оси быть не могут, каким бы подходящим человек ни
            # выглядел. 27.07 модель подняла до горячего клиентку, которая
            # лишь описала ситуацию, — и правило смещения закрепило это
            # на выдуманных осях.
            downgraded = [axis for axis in ("readiness", "urgency") if data.get(axis) == "high"]
            for axis in downgraded:
                data[axis] = "medium"
            if data.get("status") == "hot":
                data["status"] = "warm"
                # Пометку «решение за вами» ставит finalize(): пока вопросы
                # не заданы, правильный ответ не «передать», а «спросить
                # дальше». Раньше этот флаг взводился сразу и уводил Юлии
                # недоспрошенного человека на втором вопросе.
                data["_hot_without_signal"] = True
                logger.info("Статус hot без признака готовности → warm")
            elif downgraded:
                logger.info(
                    "Оси %s снижены: признак готовности в диалоге не прозвучал",
                    ", ".join(downgraded),
                )

        if (
            data.get("status") == "warm"
            and data.get("readiness") == "high"
            and data.get("urgency") == "high"
        ):
            # «Правило смещения» (ТЗ, Блок 4): warm при высокой готовности и
            # срочности → hot и передача. Требование детерминированное, поэтому
            # проверяется кодом, а не только просьбой в промпте: модель может
            # вернуть warm с двумя high, и тогда горячий клиент молча остался бы
            # в прогреве. Применяется и к промежуточной квалификации — признаки
            # уже налицо, ждать финального шага незачем.
            logger.info("Правило смещения: warm + readiness/urgency=high → hot, передаю Юлии")
            data["status"] = "hot"
            data["needs_yulia"] = True
            if not data.get("needs_yulia_reason"):
                data["needs_yulia_reason"] = (
                    "Правило смещения: признаки warm при высокой готовности и срочности"
                )

        if (
            data.get("status") == "non_target"
            and data["confidence"] >= self.confidence_threshold
            and not data.get("needs_yulia_reason")
        ):
            # ТЗ, «Маршрутизация после квалификации»: нецелевое обращение
            # завершается вежливым ответом и paused=true — Юлии оно не идёт.
            # Модель, привыкшая к «при сомнениях передай», ставит needs_yulia
            # и здесь; уверенный вердикт «вне компетенции» сомнением не
            # является. Названную моделью причину передачи уважаем: она
            # означает, что кроме нецелевого запроса в диалоге есть что-то
            # ещё (агрессия, тяжёлая ситуация, просьба о человеке).
            if data.get("needs_yulia"):
                logger.info(
                    "Уверенное нецелевое обращение (%d%%) — завершаю без передачи Юлии",
                    int(data["confidence"]),
                )
            data["needs_yulia"] = False

        stop_phrase = validators.find_stop_phrase(data.get("bot_response"))
        if stop_phrase:
            logger.warning(
                "Стоп-фраза в ответе AI: %r — заменяю нейтральным шаблоном, передаю Юлии",
                stop_phrase,
            )
            data["bot_response"] = texts.NEUTRAL_FALLBACK
            data["needs_yulia"] = True
            # Решение кода, а не модели: квалификацию оно обрывает всегда.
            # Продолжать разговор, в котором AI уже нарушил запрет бренда,
            # нельзя — дальше человека ведёт Юлия.
            data["_forced_handoff"] = True
            if not data.get("needs_yulia_reason"):
                data["needs_yulia_reason"] = f"Стоп-фраза в ответе AI: {stop_phrase}"

        return self.finalize(data) if final else data

    @staticmethod
    def _verify_handoff_trigger(data: dict, conversation: str | list[dict]) -> None:
        """Снимает основание передачи, не подтверждённое словами клиента.

        ``handoff_trigger`` обрывает квалификацию, поэтому выдуманное
        основание стоит дорого. Прод 29.07: в промпт попала склейка четырёх
        разговоров одного человека, среди них «Ктт такая юлия» — модель
        вернула personal_contact, и новое обращение оборвалось на первом
        вопросе, хотя о личном контакте никто не просил.

        Требуем цитату и проверяем, что она действительно есть в репликах
        КЛИЕНТА текущего обращения. Проверка по нормализованному тексту:
        модель цитирует со своей пунктуацией и падежами.
        """
        trigger = data.get("handoff_trigger")
        if trigger in (None, "none"):
            return
        if isinstance(conversation, str):
            client_text = conversation
        else:
            client_text = " ".join(
                turn.get("text", "")
                for turn in prompts_qualifier.current_cycle(conversation)
                if turn.get("role") in ("client", "user")
            )
        quote = data.get("handoff_quote")
        haystack = validators.normalize_for_match(client_text)
        needle = validators.normalize_for_match(quote) if isinstance(quote, str) else ""
        if not needle or needle not in haystack:
            logger.info(
                "Основание передачи %r не подтверждено словами клиента (цитата: %r) — снимаю",
                trigger,
                quote,
            )
            data["handoff_trigger"] = "none"
            return
        if trigger == "heavy_situation" and not validators.find_crisis_marker(client_text):
            # ТЗ, раздел 11: тяжёлая ситуация → немедленная передача. Но
            # оценке модели здесь верить нельзя — практика Юлии вся про
            # трудные темы, и «выгорание» с «тревогой» прилетали как кризис.
            # Признаём острое состояние только словами самого человека.
            logger.info("heavy_situation без слов острого состояния — снимаю основание")
            data["handoff_trigger"] = "none"
            return
        if trigger in validators.INFERRED_HANDOFF_TRIGGERS:
            # Оценка модели, а не слова клиента: квалификацию не обрывает,
            # но Юлия о ней узнает — флаг ставится, карточка придёт в конце
            logger.info("Основание %r — оценка модели: продолжаю вопросы", trigger)

    def finalize(self, data: dict) -> dict:
        """Пост-правила, применимые только когда решение принимается.

        Вынесены из ``qualify()``, потому что «последний вопрос задан» и
        «решение принимается» — разные события. Сценарий B может завершиться
        досрочно на четвёртом вопросе (пятый по ТЗ условный), и тогда решение
        принимается по квалификации, запрошенной с ``final=False``. Без этого
        разделения досрочно завершённый диалог терял и порог 85%, и пометку
        «решение за вами» у горячего без признака готовности.

        Вызывать повторно безопасно: маркер снимается, пометка не удваивается.
        """
        if data.pop("_hot_without_signal", False):
            # «Критерии квалификации»: горячим человек становится по
            # наблюдаемому признаку. Признака не прозвучало — статус понижен
            # до warm, и передачи НЕ происходит.
            #
            # 27.07 здесь стояла передача «на решение Юлии»: тогда проблемой
            # был ложный ярлык «горячий» в карточке. Оказалось, что модель
            # объявляет горячим почти каждого — 29.07 клиент, сказавший
            # «у нас плохое общение», ушёл Юлии именно так. Тёплый остаётся
            # в разговоре с ботом и попадёт к Юлии, когда скажет о готовности.
            logger.info("Статус hot без признака готовности → warm, разговор продолжается")

        if data.get("status") == "cold" and data.get("confidence", 100) < self.confidence_threshold:
            # Холодный лид Юлии не передаётся: «Маршрутизация после
            # квалификации» отправляет его в прогрев. Звать человека к тому,
            # кого AI сам назвал холодным, нечего — а порог 85% делал ровно
            # это: клиент, отвечавший «хз» и «не знаю», получал «передал
            # информацию Юлии» вместо материалов канала (прод 29.07).
            logger.info("Холодный лид с уверенностью %s — прогрев, не передача", data["confidence"])
        elif data.get("confidence", 100) < self.confidence_threshold:
            # Порог 85% из «Критериев квалификации», п. 9: ниже — решает Юлия.
            # Пометка «Требуется экспертная оценка» обязательна ВСЕГДА (ТЗ);
            # причину, названную моделью, сохраняем после пометки.
            data["needs_yulia"] = True
            mark = "Требуется экспертная оценка"
            model_reason = data.get("needs_yulia_reason")
            if isinstance(model_reason, str) and model_reason.strip() and mark not in model_reason:
                data["needs_yulia_reason"] = f"{mark}: {model_reason}"
            elif not (isinstance(model_reason, str) and mark in model_reason):
                data["needs_yulia_reason"] = mark
        return data

    # ── Ответ на информационный вопрос (сценарий C, Блок 6) ──

    async def answer_info(
        self, question: str, *, history: str = "", knowledge: str | None = None
    ) -> dict | None:
        """Ответ на информационный вопрос без квалификации (сценарий C).

        Стоп-фраза в ответе → нейтральный шаблон + ``needs_yulia=true``.
        """
        data = await self._ask_json(
            build_info_answer_prompt(question, history),
            validators.validate_info_answer,
            knowledge=knowledge,
        )
        if data is None:
            return None
        stop_phrase = validators.find_stop_phrase(data.get("answer"))
        if stop_phrase:
            logger.warning(
                "Стоп-фраза в информационном ответе: %r — нейтральный шаблон", stop_phrase
            )
            data["answer"] = texts.NEUTRAL_FALLBACK
            data["needs_yulia"] = True
        return data

    # ── Функция 3: анализ комментариев ──

    async def analyze_comment(
        self,
        comment_text: str,
        author_name: str,
        *,
        post_topic: str = "тема неизвестна",
        post_text: str | None = None,
        knowledge: str | None = None,
    ) -> dict | None:
        """Анализ комментария. Стоп-фраза в suggested_reply → WARNING и пустое
        предложение (Юлия напишет свой вариант); автопубликации всё равно нет.
        """
        data = await self._ask_json(
            build_comment_prompt(comment_text, author_name, post_topic, post_text),
            validators.validate_comment_analysis,
            knowledge=knowledge,
        )
        if data is None:
            return None
        stop_phrase = validators.find_stop_phrase(data.get("suggested_reply"))
        if stop_phrase:
            logger.warning("Стоп-фраза в suggested_reply: %r — предложение очищено", stop_phrase)
            data["suggested_reply"] = ""
        return data

    async def analyze_questionnaire(
        self, answers: list[str], *, knowledge: str | None = None
    ) -> dict | None:
        """Разбор анкеты «Точка сбоя» (Блок 11).

        ``None`` при любом сбое — вызывающая сторона обязана всё равно
        отправить отчёт Юлии из сырых ответов: анкета не должна пропасть
        из-за недоступности OpenAI.
        """
        return await self._ask_json(
            build_questionnaire_prompt(answers),
            validators.validate_questionnaire_analysis,
            knowledge=knowledge,
        )


# ── Модульный интерфейс ──

_service: AIService | None = None


def init_ai(config: Config) -> AIService:
    """Создаёт AI-сервис из конфига. Вызывается на старте бота."""
    global _service
    _service = AIService(
        api_key=config.openai_api_key,
        model=config.openai_model,
        timeout=config.openai_timeout,
        confidence_threshold=config.ai_confidence_threshold,
    )
    return _service


def get_ai() -> AIService:
    if _service is None:
        raise RuntimeError("AI-сервис не инициализирован: вызовите init_ai(config)")
    return _service
