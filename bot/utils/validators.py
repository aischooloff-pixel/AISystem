"""Валидация ответов AI (Блок 4, обязательная).

Порядок из ТЗ: распарсить JSON → обязательные ключи → допустимость enum →
confidence 0–100 → стоп-фразы в тексте ответа. Провал парсинга даёт один
retry с уточнением формата (это делает ``services/ai.py``); окончательный
провал — передача Юлии с пометкой «Ошибка обработки AI».

Стоп-фразы проверяются по нормализованному тексту (нижний регистр, без
пунктуации, схлопнутые пробелы) — иначе модель обходит проверку запятой
или восклицательным знаком.
"""

from __future__ import annotations

import json
import re
import unicodedata

# Запрещённые фразы: «Работа AI с сомнениями клиента v1.0» (8) +
# «FAQ бренда» / ТЗ Блок 4 (дополнительные). Дословно из документов.
STOP_PHRASES: tuple[str, ...] = (
    # «Работа AI с сомнениями клиента v1.0»
    "Если не купите сейчас — ничего не изменится",
    "У вас точно родовая программа",
    "Вам срочно нужна диагностика",
    "Я знаю причину вашей проблемы",
    "Это единственный способ решить ситуацию",
    "Без моей помощи вы не справитесь",
    "После работы всё обязательно изменится",
    "Вы сами виноваты",
    # «FAQ бренда» (запрещённые ответы AI, ТЗ Блок 2/4)
    "После диагностики всё изменится",
    "Вам обязательно нужна эта услуга",
    "Я гарантирую результат",
    "Это точно родовая проблема",
    "Вам поможет только этот метод",
    # «Принципы продаж», п. 6 и «Продуктовая линейка»: недопустимая формулировка
    "Вам обязательно нужна диагностика",
)


def _normalize(text: str) -> str:
    """Нижний регистр, без пунктуации и «ё», один пробел между словами."""
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_NORMALIZED_STOP = tuple((phrase, _normalize(phrase)) for phrase in STOP_PHRASES)

# Смысловые шаблоны — вторая линия к дословному списку.
# Дословное сравнение ловит только цитату из документа, а модель, нарушившая
# инструкцию промпта, скажет своими словами: «Я гарантирую ВАМ результат»,
# «Вы сами В ЭТОМ виноваты». Шаблоны бьют по смыслу запрета, а не по фразе.
# Ложное срабатывание здесь дёшево: лишняя передача Юлии — ровно то, что
# предписывает принцип безопасности ТЗ («лучше лишняя передача человеку»).
# Применяются к нормализованному тексту, поэтому без пунктуации и «ё».
_STOP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"гарантиру\w*(?:\s+\w+){0,3}\s+результат"),
        "обещание гарантированного результата",
    ),
    (re.compile(r"\bвы\b(?:\s+\w+){0,2}\s+сам\w*(?:\s+\w+){0,2}\s+виноват"), "обвинение клиента"),
    (
        re.compile(r"без\s+(?:моей|нашей)\s+помощи(?:\s+\w+){0,3}\s+не\s+справ"),
        "давление беспомощностью",
    ),
    (
        re.compile(r"(?:точно|наверняка)\s+знаю(?:\s+\w+){0,2}\s+причин"),
        "присвоение экспертного вывода",
    ),
    (
        re.compile(
            r"после\s+(?:диагностики|работы|встречи)(?:\s+\w+){0,4}\s+(?:все|всё)\s+изменит"
        ),
        "обещание изменений после работы",
    ),
    (
        re.compile(r"единственн\w+\s+(?:способ|метод|путь|вариант)"),
        "навязывание единственного пути",
    ),
    (
        re.compile(
            r"(?:обязательно|срочно)\s+нужн\w*(?:\s+\w+){0,2}\s+(?:диагностик|услуг|сессия)"
        ),
        "искусственная срочность",
    ),
)


def find_stop_phrase(text: str | None) -> str | None:
    """Первая запрещённая фраза в тексте или ``None``.

    Сначала дословный список (13 фраз ТЗ), затем смысловые шаблоны —
    перефразированный запрет так же недопустим, как процитированный.
    """
    if not text or not isinstance(text, str):
        # Не-строка от модели — дело схемной валидации; здесь не падаем
        return None
    normalized = _normalize(text)
    for phrase, normalized_phrase in _NORMALIZED_STOP:
        if normalized_phrase in normalized:
            return phrase
    for pattern, label in _STOP_PATTERNS:
        if pattern.search(normalized):
            return label
    return None


def normalize_for_match(text: str | None) -> str:
    """Текст для сверки цитаты с репликами клиента.

    Модель цитирует со своей пунктуацией и регистром, поэтому сравниваем
    по нормализованной форме — той же, что и для стоп-фраз.
    """
    return _normalize(text) if isinstance(text, str) else ""


def sanitize_user_text(text: str) -> str:
    """Готовит пользовательский текст к вставке в промпт.

    Текст вставляется в блок, ограждённый тройными кавычками — последовательность
    из трёх и более кавычек внутри текста закрыла бы разделитель, и остаток
    сообщения встал бы в промпте в позицию инструкции (prompt injection).
    Схлопываем такие последовательности до одной кавычки.
    """
    if not isinstance(text, str):
        return ""
    return re.sub(r'["\'`]{3,}', '"', text)


def parse_ai_json(raw: str | None) -> dict | None:
    """JSON из ответа модели. Терпит обёртку в ```-блок; ``None`` — не парсится."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ── Схемы трёх функций AI (ТЗ, Блок 4) ──

LEVELS = {"high", "medium", "low"}
STATUSES = {"hot", "warm", "cold", "non_target"}
# Пять исходов, а не три: нецелевому обращению и отказу говорить с ботом
# нужны собственные имена. Пока их не было, «погода на завтра» становилась
# C_info, ответа в базе знаний не находилось — и мусорный лид уходил Юлии
# (аудит 2026-07-28). «Вопрос по психиатрии» модель честно называла нецелевым,
# а валидатор отвергал ответ целиком и слал клиенту техническую ошибку.
SCENARIOS = {"A_ready", "B_problem", "C_info", "non_target", "handoff"}
EMOTIONS = {"positive", "neutral", "negative", "interested"}
PRODUCT_INTERESTS = {
    "diagnostics",
    "session",
    "constellation",
    "business_constellation",
    "strategic",
    "support",
    "b2b",
    "education",
    "unknown",
}

# Признаки горячего клиента — «Критерии квалификации», раздел «Горячий клиент».
# Статус hot без названного признака кодом понижается до warm (см. AIService).
READINESS_SIGNALS = {
    "booking",
    "price_and_dates",
    "payment",
    "personal_contact",
    "explicit_confirmation",
    # Ось срочности определена там же через «указывает на временные рамки» —
    # без этого признака правило смещения не могло бы сработать законно
    "deadline",
    "none",
}

# Основания немедленной передачи (ТЗ, раздел 11) — только НАБЛЮДАЕМЫЕ события.
# Намеренно отсутствуют «AI не уверен» и «сложный запрос»: это суждения, а не
# события, и модель выносит их при любой нехватке информации. Живой прод
# 28.07: на «хочу увеличить доход» модель вернула needs_yulia=true с
# confidence 85 — и человек уходил Юлии после одного вопроса. Нехватку
# информации закрывает порог 85% в конце цепочки, а не обрыв квалификации.
# Основания, где клиент ЯВНО что-то сделал или попросил. Их видно из текста
# дословно, спорить тут не с чем — квалификация прерывается сразу: доспрашивать
# человека, который попросил живого собеседника или сказал «хочу записаться»,
# бессмысленно и невежливо.
EXPLICIT_HANDOFF_TRIGGERS = {
    "personal_contact",  # просит личное общение с Юлией
    "booking_request",  # хочет записаться
    "support_question",  # спрашивает о сопровождении
    "payment_ready",  # готов оплатить
    "negative_to_ai",  # негативная реакция на AI, не хочет говорить с ботом
    "b2b",  # любой B2B-запрос
    "education",  # обучение, партнёрство, работа в ITC
    # Тяжёлое состояние остаётся здесь по прямому требованию ТЗ (раздел 11:
    # «ситуация эмоционально тяжёлая» → немедленная передача). Доспрашивать
    # человека в остром состоянии нельзя. Ложные срабатывания на теме запроса
    # («выгорание», «проблемы в семье») лечатся промптом, а не отсрочкой.
    "heavy_situation",
}

# Основания, которые модель ВЫВОДИТ, а не читает в словах клиента. Цепочку
# вопросов они не прерывают: флаг поднимается, и Юлия получает карточку
# в конце — с собранной картиной, а не после первого вопроса. Прерывать
# разговор из-за оценки, которую человек не подтверждал, значит терять
# квалификацию на ровном месте.
INFERRED_HANDOFF_TRIGGERS = {
    "conflict",  # оценка тона разговора
    "beyond_knowledge",  # оценка полноты базы знаний
}

# Вопрос о деньгах — это ВОПРОС, а не готовность. Прод 29.07: человек посреди
# квалификации спросил «цена консультации?» и вместо цены получил «Передал
# информацию Юлии». Стоимость диагностики бот называть вправе («Продуктовая
# линейка»), поэтому такой запрос не прерывает разговор: бот отвечает
# и возвращается к своему вопросу.
ANSWER_FIRST_TRIGGER = "price_question"

HANDOFF_TRIGGERS = (
    EXPLICIT_HANDOFF_TRIGGERS | INFERRED_HANDOFF_TRIGGERS | {ANSWER_FIRST_TRIGGER, "none"}
)

# Слова острого состояния. Нужны потому, что оценке модели здесь верить
# нельзя: практика Юлии вся про трудные ситуации, и модель, обученная быть
# осторожной, читает как кризис любую тему. Живой прогон 29.07 дал
# heavy_situation на «полное выгорание» и «постоянная тревога» — обычные
# целевые запросы, ради которых и задаются вопросы.
# Поэтому heavy_situation прерывает квалификацию (ТЗ, раздел 11) только когда
# человек сказал что-то из этого СВОИМИ словами. Список намеренно узкий:
# пропустить кризис дороже, чем задать лишний вопрос, но и обрывать каждого
# второго клиента нельзя. Сравнение по нормализованному тексту.
CRISIS_MARKERS: tuple[str, ...] = (
    "не хочу жить",
    "не хочется жить",
    "хочу умереть",
    "думаю о смерти",
    "мысли о смерти",
    "покончить",
    "суицид",
    "свести счеты с жизнью",
    "не вижу выхода",
    "не могу больше",
    "очень плохо",
    "совсем плохо",
    "паническ",
    "тяжелое событие",
    "умер",
    "умерла",
    "погиб",
    "потерял близк",
    "потеряла близк",
    "смерть близк",
)


def find_crisis_marker(text: str | None) -> str | None:
    """Слово острого состояния в тексте клиента или ``None``."""
    normalized = normalize_for_match(text)
    return next((marker for marker in CRISIS_MARKERS if marker in normalized), None)

# Тема запроса — «Возможные направления» из продуктовой линейки Юлии.
# Берём её словарь, а не свой: статистика должна складываться в те же
# категории, которыми она описывает практику.
REQUEST_CATEGORIES = {
    "отношения",
    "денежные сценарии",
    "самоценность",
    "границы",
    "внутренняя устойчивость",
    "профессиональные изменения",
    "бизнес и управление",
    "делегирование",
    "масштабирование",
    "переход от ручного управления к системной модели",
    "другое",
}


def _check_confidence(data: dict, problems: list[str]) -> None:
    value = data.get("confidence")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        problems.append(f"confidence: не число ({value!r})")
    elif not 0 <= value <= 100:
        problems.append(f"confidence: вне диапазона 0–100 ({value!r})")


def _check_enum(data: dict, key: str, allowed: set[str], problems: list[str]) -> None:
    value = data.get(key)
    if value not in allowed:
        problems.append(f"{key}: недопустимое значение {value!r}")


def _check_required(data: dict, keys: tuple[str, ...], problems: list[str]) -> None:
    for key in keys:
        if key not in data:
            problems.append(f"нет обязательного ключа: {key}")


def validate_scenario(data: dict) -> list[str]:
    """Проблемы ответа функции «определение сценария»; пустой список — валидно."""
    problems: list[str] = []
    _check_required(data, ("scenario", "confidence", "reason"), problems)
    if "scenario" in data:
        _check_enum(data, "scenario", SCENARIOS, problems)
    if "confidence" in data:
        _check_confidence(data, problems)
    return problems


QUALIFICATION_REQUIRED = (
    "summary",
    "key_phrases",
    "status",
    "status_reason",
    "awareness",
    "readiness",
    "urgency",
    "confidence",
    "next_action",
    "needs_yulia",
    "needs_yulia_reason",
    "product_interest",
    "interests",
    "bot_response",
)


def validate_qualification(data: dict) -> list[str]:
    """Проблемы ответа функции «квалификация»; пустой список — валидно."""
    problems: list[str] = []
    _check_required(data, QUALIFICATION_REQUIRED, problems)
    if "status" in data:
        _check_enum(data, "status", STATUSES, problems)
    for axis in ("awareness", "readiness", "urgency"):
        if axis in data:
            _check_enum(data, axis, LEVELS, problems)
    if "product_interest" in data:
        _check_enum(data, "product_interest", PRODUCT_INTERESTS, problems)
    # Два поля добавлены позже остальных и намеренно необязательны: модель,
    # забывшая новое поле, не должна ронять весь ответ и гнать клиента
    # на «Ошибку обработки AI» — отсутствие трактуется как «признака нет»
    # и «категория не определена».
    if "readiness_signal" in data and data["readiness_signal"] is not None:
        _check_enum(data, "readiness_signal", READINESS_SIGNALS, problems)
    if "request_category" in data and data["request_category"] is not None:
        _check_enum(data, "request_category", REQUEST_CATEGORIES, problems)
    if "handoff_trigger" in data and data["handoff_trigger"] is not None:
        _check_enum(data, "handoff_trigger", HANDOFF_TRIGGERS, problems)
    if "confidence" in data:
        _check_confidence(data, problems)
    if "needs_yulia" in data and not isinstance(data["needs_yulia"], bool):
        problems.append(f"needs_yulia: не булево ({data['needs_yulia']!r})")
    for list_key in ("key_phrases", "interests"):
        if list_key in data and not isinstance(data[list_key], list):
            problems.append(f"{list_key}: не список ({data[list_key]!r})")
    if "bot_response" in data and not isinstance(data["bot_response"], str):
        problems.append("bot_response: не строка")
    return problems


INFO_ANSWER_REQUIRED = ("answer", "needs_yulia", "reason")


def validate_info_answer(data: dict) -> list[str]:
    """Проблемы ответа функции «информационный ответ» (сценарий C)."""
    problems: list[str] = []
    _check_required(data, INFO_ANSWER_REQUIRED, problems)
    if "answer" in data and not isinstance(data["answer"], str):
        problems.append(f"answer: не строка ({data['answer']!r})")
    if "needs_yulia" in data and not isinstance(data["needs_yulia"], bool):
        problems.append(f"needs_yulia: не булево ({data['needs_yulia']!r})")
    return problems


COMMENT_REQUIRED = (
    "topic",
    "emotion",
    "key_problem",
    "interest_level",
    "is_potential_client",
    "needs_reply",
    "request_detected",
    "suggested_reply",
    "should_invite_to_bot",
    "confidence",
)


def validate_comment_analysis(data: dict) -> list[str]:
    """Проблемы ответа функции «анализ комментария»; пустой список — валидно."""
    problems: list[str] = []
    _check_required(data, COMMENT_REQUIRED, problems)
    if "emotion" in data:
        _check_enum(data, "emotion", EMOTIONS, problems)
    level = data.get("interest_level")
    if "interest_level" in data and (
        not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 5
    ):
        problems.append(f"interest_level: не целое 1–5 ({level!r})")
    if "confidence" in data:
        _check_confidence(data, problems)
    for flag in ("is_potential_client", "needs_reply", "should_invite_to_bot"):
        if flag in data and not isinstance(data[flag], bool):
            problems.append(f"{flag}: не булево ({data[flag]!r})")
    # suggested_reply уходит в find_stop_phrase и в карточку Юлии.
    # null допустим: при needs_reply=false отвечать не на что, и модель честно
    # возвращает пустоту. Потребители к этому готовы — «or '—'» в карточке,
    # «or ''» при публикации. Отвергать такой ответ значило бы гнать на ручную
    # передачу каждый комментарий, не требующий реакции.
    if "suggested_reply" in data and not isinstance(data["suggested_reply"], (str, type(None))):
        problems.append(f"suggested_reply: не строка и не null ({data['suggested_reply']!r})")
    return problems


QUESTIONNAIRE_REQUIRED = (
    "main_request",
    "summary",
    "key_phrases",
    "preliminary_status",
    "topics_to_clarify",
    "confidence",
)


def validate_questionnaire_analysis(data: dict) -> list[str]:
    """Проблемы разбора анкеты «Точка сбоя» (Блок 11); пустой список — валидно."""
    problems: list[str] = []
    _check_required(data, QUESTIONNAIRE_REQUIRED, problems)
    if "preliminary_status" in data:
        _check_enum(data, "preliminary_status", STATUSES, problems)
    if "confidence" in data:
        _check_confidence(data, problems)
    for key in ("main_request", "summary"):
        if key in data and not isinstance(data[key], str):
            problems.append(f"{key}: не строка ({data[key]!r})")
    # Списки уходят прямо в отчёт Юлии — строки, иначе форматирование падает
    for key in ("key_phrases", "topics_to_clarify"):
        value = data.get(key)
        if key in data and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            problems.append(f"{key}: не список строк ({value!r})")
    return problems
