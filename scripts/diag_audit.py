"""Диагностика аудита: путь «погода на завтра» и живые данные CRM.

Одноразовый скрипт. Запуск: python -m scripts.diag_audit
"""

from __future__ import annotations

import asyncio
import json
import sys

from bot.config import load_config
from bot.services.ai import init_ai
from bot.services.airtable import init_airtable, get_client
from bot.services.knowledge import load_knowledge

MESSAGES = [
    ("Погода на завтра", "non_target"),
    ("Привет", "C_info"),
    ("Хочу записаться", "A_ready"),
    ("Сколько стоит?", "A_ready"),
    ("У меня повторяется ситуация", "B_problem"),
    ("Не могу выйти из состояния", "B_problem"),
    ("Что такое ITC?", "C_info"),
    ("Чем вы занимаетесь?", "C_info"),
    ("Снимите порчу", "non_target"),
    ("Мне нужен юрист", "non_target"),
    ("Бизнес не растёт, что делать?", "B_problem"),
    ("Как работает расстановка?", "C_info"),
    ("Интересует диагностика", "A_ready"),
    ("Можно с живым человеком?", "handoff"),
    ("Не хочу разговаривать с ботом", "handoff"),
    ("Думаю о суициде, не могу больше", "handoff"),
    ("Я по рекомендации", "B_problem"),
    ("Вопрос по психиатрии", "non_target"),
    ("Помогите разобраться", "B_problem"),
    ("ааааааааа", "non_target"),
    ("Сколько стоит сопровождение?", "A_ready"),
    # Эмодзи без текста — переспросить, а не закрыть диалог как нецелевой
    ("👍", "C_info"),
]


async def scenarios() -> None:
    ai = init_ai(load_config())
    print("\n=== ТАБЛИЦА ОПРЕДЕЛЕНИЯ СЦЕНАРИЕВ ===\n")
    print(f"{'#':<3} {'Сообщение':<34} {'Ожидаемый':<12} {'Фактический':<12} OK")
    print("-" * 78)
    ok_count = 0
    for index, (text, expected) in enumerate(MESSAGES, 1):
        result = await ai.detect_scenario(text)
        actual = result["scenario"] if result else "СБОЙ"
        conf = result.get("confidence") if result else "—"
        match = "OK" if actual == expected else "FAIL"
        if match == "OK":
            ok_count += 1
        print(f"{index:<3} {text[:33]:<34} {expected:<12} {actual:<12} {match} ({conf}%)")
    print("-" * 78)
    print(f"Совпало: {ok_count}/{len(MESSAGES)}")
    await ai.close()


async def crm_stats() -> None:
    config = load_config()
    init_airtable(config)
    client = get_client()
    print("\n=== ЖИВЫЕ ДАННЫЕ CRM ===\n")
    records = await client._list_all(config.airtable_contacts_table)
    if records is None:
        print("Airtable недоступен")
        return
    statuses: dict[str, int] = {}
    scenarios_seen: dict[str, int] = {}
    qualified = 0
    history_lengths = []
    for record in records:
        fields = record.get("fields", {})
        statuses[fields.get("status") or "—"] = statuses.get(fields.get("status") or "—", 0) + 1
        key = fields.get("scenario") or "—"
        scenarios_seen[key] = scenarios_seen.get(key, 0) + 1
        if fields.get("qualification_completed"):
            qualified += 1
        raw = fields.get("conversation_history") or "[]"
        try:
            history = json.loads(raw)
            bot_turns = sum(1 for t in history if t.get("role") == "bot")
            if history:
                history_lengths.append((fields.get("name") or "?", len(history), bot_turns))
        except (json.JSONDecodeError, TypeError):
            pass

    print(f"Всего контактов: {len(records)}")
    print(f"Квалификация завершена: {qualified}")
    print("\nСтатусы:")
    for status, count in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {status:<14} {count}")
    print("\nСценарии:")
    for scenario, count in sorted(scenarios_seen.items(), key=lambda x: -x[1]):
        print(f"  {scenario:<14} {count}")
    print("\nДлина переписки (топ-10 по числу реплик):")
    for name, total, bot_turns in sorted(history_lengths, key=lambda x: -x[1])[:10]:
        print(f"  {name[:22]:<24} реплик={total:<4} из них бот={bot_turns}")
    await client.close()


async def main() -> None:
    load_knowledge(load_config().knowledge_dir)
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "crm"):
        await crm_stats()
    if what in ("all", "scenarios"):
        await scenarios()


if __name__ == "__main__":
    asyncio.run(main())
