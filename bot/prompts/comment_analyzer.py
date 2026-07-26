"""Промпт анализа комментария под постом → строгий JSON (Блок 4, функция 3).

Результат пишется в таблицу Comments и решает, уведомлять ли Юлию
о потенциальном клиенте (Блок 8). Автопубликации нет: suggested_reply —
только предложение, публикуется после явного утверждения Юлией.
"""

from __future__ import annotations

from bot.utils.validators import sanitize_user_text

COMMENT_JSON_FORMAT = """{
  "topic": "тема комментария",
  "emotion": "positive|neutral|negative|interested",
  "key_problem": "ключевая проблема или null",
  "interest_level": 4,
  "is_potential_client": true,
  "needs_reply": true,
  "request_detected": "описание запроса или null",
  "suggested_reply": "предложенный ответ",
  "should_invite_to_bot": true,
  "confidence": 88
}"""

COMMENT_PROMPT_TEMPLATE = """Проанализируй комментарий под постом канала Юлии.

Правила:
- emotion — по тону комментария; interested = виден личный отклик или запрос;
- interest_level — целое 1–5: насколько человек похож на потенциального
  клиента по содержанию комментария (5 — явный личный запрос);
- is_potential_client=true только при наблюдаемых признаках: узнаёт себя,
  описывает свою ситуацию, задаёт вопрос о методе или работе;
- needs_reply=true, если содержательный ответ уместен (вопрос, личный
  отклик); на реакции-междометия отвечать не нужно;
- suggested_reply — короткий уважительный ответ от AI-помощника Юлии
  (не от имени Юлии): без давления, без продажи, без запрещённых фраз;
  при уместности — мягкое приглашение написать в личные сообщения бота;
- should_invite_to_bot=true, если человека стоит пригласить в бот
  для разговора о его ситуации;
- key_problem и request_detected — null, если их не видно в тексте;
- confidence — целое 0–100.

Пост: «{post_topic}»
{post_context}Автор комментария: {author_name}
Комментарий:
\"\"\"{comment_text}\"\"\"

Ответь строго JSON без пояснений, в формате:
{json_format}"""


def build_comment_prompt(
    comment_text: str,
    author_name: str,
    post_topic: str = "тема неизвестна",
    post_text: str | None = None,
) -> str:
    """Промпт анализа комментария с контекстом поста."""
    post_context = (
        f"Текст поста (фрагмент): «{sanitize_user_text(post_text)[:500]}»\n" if post_text else ""
    )
    return COMMENT_PROMPT_TEMPLATE.format(
        comment_text=sanitize_user_text(comment_text),
        author_name=sanitize_user_text(author_name),
        post_topic=sanitize_user_text(post_topic),
        post_context=post_context,
        json_format=COMMENT_JSON_FORMAT,
    )
