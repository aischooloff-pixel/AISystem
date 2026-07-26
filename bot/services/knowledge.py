"""База знаний: загрузка ``.md`` из ``bot/knowledge/`` в контекст AI (Блок 2).

Расширение базы — добавлением файла в каталог, без изменения кода:
``load_knowledge()`` подхватывает все ``*.md``. Файлы читаются в алфавитном
порядке, каждый предваряется заголовком ``## Источник: {filename}``, чтобы
Юлия могла разобрать, из какого документа AI взял формулировку.

База кэшируется в памяти (ТЗ, Часть 7): OpenAI вызывается на каждое сообщение,
диск — только на старте и по ``/reload_knowledge``.
"""

from __future__ import annotations

import os
from pathlib import Path

from bot.utils.logger import get_app_logger

# Оценка токенов для русского текста: ~3 символа на токен (ТЗ, Блок 2).
CHARS_PER_TOKEN = 3
# Превышение — сигнал переходить на векторную базу (следующий спринт), а не ошибка.
TOKEN_WARN_LIMIT = 100_000

_DEFAULT_DIR = Path("bot/knowledge")

_cache: str | None = None
_cache_dir: Path | None = None


def _knowledge_dir(knowledge_dir: Path | str | None = None) -> Path:
    """Каталог базы знаний: аргумент → каталог первой загрузки → env → дефолт.

    Кэш-каталог в приоритете над env: pydantic-settings читает .env только
    в Config и НЕ экспортирует в os.environ — без этого get_knowledge() без
    аргумента молча подменял бы нестандартный KNOWLEDGE_DIR дефолтным.
    """
    if knowledge_dir is not None:
        return Path(knowledge_dir)
    if _cache_dir is not None:
        return _cache_dir
    return Path(os.environ.get("KNOWLEDGE_DIR", _DEFAULT_DIR))


def load_knowledge(knowledge_dir: Path | str | None = None) -> str:
    """Читает все ``.md`` базы знаний и конкатенирует их с заголовками источников.

    Обновляет кэш. Пишет в лог состав базы и оценку токенов;
    при превышении ``TOKEN_WARN_LIMIT`` — предупреждение.
    """
    global _cache, _cache_dir
    directory = _knowledge_dir(knowledge_dir)
    logger = get_app_logger()

    parts: list[str] = []
    for path in sorted(directory.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            # Нечитаемый файл не должен ронять бота: работаем на остальной базе.
            logger.exception("База знаний: не удалось прочитать %s", path)
            continue
        parts.append(f"## Источник: {path.name}\n\n{content}")

    text = "\n\n".join(parts)
    _cache = text
    _cache_dir = directory

    stats = _stats_for(text, directory)
    logger.info(
        "База знаний загружена: %d файлов, %d символов, ~%d токенов (%s)",
        stats["files"],
        stats["total_chars"],
        stats["estimated_tokens"],
        ", ".join(stats["file_names"]) or "каталог пуст",
    )
    if not parts:
        logger.warning("База знаний пуста: в %s нет .md файлов", directory)
    if stats["estimated_tokens"] > TOKEN_WARN_LIMIT:
        logger.warning(
            "База знаний превышает %d токенов (~%d): пора переходить на векторную базу "
            "(следующий спринт)",
            TOKEN_WARN_LIMIT,
            stats["estimated_tokens"],
        )
    return text


def get_knowledge(knowledge_dir: Path | str | None = None) -> str:
    """Возвращает закэшированную базу знаний; при первом вызове читает с диска."""
    directory = _knowledge_dir(knowledge_dir)
    if _cache is None or _cache_dir != directory:
        return load_knowledge(directory)
    return _cache


def reload_knowledge(knowledge_dir: Path | str | None = None) -> str:
    """Перечитывает базу знаний с диска (команда Юлии ``/reload_knowledge``)."""
    return load_knowledge(knowledge_dir)


def get_knowledge_stats(knowledge_dir: Path | str | None = None) -> dict:
    """Статистика базы: число файлов, их имена, размер и оценка токенов."""
    directory = _knowledge_dir(knowledge_dir)
    return _stats_for(get_knowledge(directory), directory)


def _stats_for(text: str, directory: Path) -> dict:
    file_names = sorted(path.name for path in directory.glob("*.md"))
    return {
        "files": len(file_names),
        "file_names": file_names,
        "total_chars": len(text),
        "estimated_tokens": len(text) // CHARS_PER_TOKEN,
    }
