"""Промпт квалификации клиента → строгий JSON (Блок 4, функция 2).

Критерии статусов, оси оценки, запрет дискриминации и главный принцип —
в базе знаний (``qualification.md``), которая целиком входит в системный
промпт. Здесь — задача, формат ответа и правила заполнения полей.
"""

from __future__ import annotations

from bot.utils.validators import sanitize_user_text

QUALIFICATION_JSON_FORMAT = """{
  "summary": "Краткое нейтральное резюме, 2–3 предложения. Без интерпретаций.",
  "key_phrases": ["дословные цитаты клиента"],
  "status": "hot|warm|cold|non_target",
  "status_reason": "Конкретные наблюдаемые признаки, а не мнение",
  "awareness": "high|medium|low",
  "readiness": "high|medium|low",
  "urgency": "high|medium|low",
  "confidence": 92,
  "next_action": "Рекомендованный следующий шаг",
  "needs_yulia": true,
  "needs_yulia_reason": "Причина передачи или null",
  "product_interest": "diagnostics|session|constellation|business_constellation|strategic|support|b2b|education|unknown",
  "interests": ["темы, вызвавшие отклик"],
  "bot_response": "Текст ответа человеку"
}"""

QUALIFICATION_PROMPT_TEMPLATE = """Проведи квалификацию клиента по диалогу ниже.

Опирайся строго на «Критерии квалификации клиентов v1.0» и «Логику принятия
решений AI v1.0» из базы знаний:
- статус определяется совокупностью наблюдаемых признаков, а не мнением;
- запрещено учитывать возраст, пол, национальность, профессию, доход,
  политические взгляды, религию, семейное положение — оценивается
  исключительно запрос и готовность к работе;
- «Правило смещения»: признаки warm при высокой срочности и высокой
  готовности → hot;
- немедленная передача Юлии (needs_yulia=true) при любом основании из
  раздела «Основания передачи Юлии»;
- confidence — целое 0–100, рассчитывается из числа совпавших наблюдаемых
  признаков и их чёткости; в status_reason перечисли эти признаки
  (образец: «описал проблему, взаимодействовал трижды, интересуется
  диагностикой, спрашивал о формате работы»);
- summary — нейтральное, без интерпретаций и диагнозов;
- key_phrases — только дословные цитаты клиента;
- bot_response — следующее сообщение человеку: спокойно, уважительно,
  коротко, без давления, без запрещённых фраз; если needs_yulia=true —
  корректное сообщение о передаче.

Диалог (реплики клиента и бота по порядку):
\"\"\"{conversation}\"\"\"

Ответь строго JSON без пояснений, в формате:
{json_format}"""


def build_qualification_prompt(conversation: str | list[dict]) -> str:
    """Промпт квалификации по истории диалога.

    ``conversation`` — готовая строка или список реплик
    ``[{{"role": "client"|"bot", "text": "..."}}]`` из ``conversation_history``.
    """
    if not isinstance(conversation, str):
        lines = []
        for turn in conversation:
            who = "Клиент" if turn.get("role") in ("client", "user") else "Бот"
            lines.append(f"{who}: {turn.get('text', '')}")
        conversation = "\n".join(lines)
    return QUALIFICATION_PROMPT_TEMPLATE.format(
        conversation=sanitize_user_text(conversation), json_format=QUALIFICATION_JSON_FORMAT
    )
