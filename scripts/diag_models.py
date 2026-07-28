"""Сравнение моделей на задаче определения сценария.

Одноразовый скрипт аудита. Запуск: python -m scripts.diag_models
"""

from __future__ import annotations

import asyncio
import time

from bot.config import load_config
from bot.services.ai import AIService
from bot.services.knowledge import load_knowledge
from scripts.diag_audit import MESSAGES

CANDIDATES = ("gpt-4o-mini", "gpt-4.1-mini", "gpt-5-nano", "gpt-5-mini")


async def run(model: str, api_key: str, threshold: int) -> tuple[int, float, list[str]]:
    ai = AIService(api_key=api_key, model=model, confidence_threshold=threshold, timeout=60)
    started = time.monotonic()
    ok = 0
    misses: list[str] = []
    for text, expected in MESSAGES:
        result = await ai.detect_scenario(text)
        actual = result["scenario"] if result else "СБОЙ"
        if actual == expected:
            ok += 1
        else:
            misses.append(f"{text!r}: ждали {expected}, получили {actual}")
    elapsed = time.monotonic() - started
    await ai.close()
    return ok, elapsed, misses


async def main() -> None:
    config = load_config()
    load_knowledge(config.knowledge_dir)
    print(f"\nЗадача: определение сценария, {len(MESSAGES)} сообщений\n")
    print(f"{'Модель':<16} {'Точность':<12} {'Время':<10} Промахи")
    print("-" * 72)
    for model in CANDIDATES:
        try:
            ok, elapsed, misses = await run(model, config.openai_api_key, config.ai_confidence_threshold)
        except Exception as error:  # noqa: BLE001 — диагностика, нужен любой сбой
            print(f"{model:<16} НЕДОСТУПНА  — {type(error).__name__}: {error}")
            continue
        rate = f"{ok}/{len(MESSAGES)}"
        per = elapsed / len(MESSAGES)
        print(f"{model:<16} {rate:<12} {per:>5.2f} с/шт  {len(misses)}")
        for miss in misses:
            print(f"{'':<16} └─ {miss}")


if __name__ == "__main__":
    asyncio.run(main())
