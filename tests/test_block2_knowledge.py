"""Тесты Блока 2: база знаний.

Проверяются критерии готовности из ТЗ: все 8 файлов на месте, ключевое
содержание (цены, стоп-фразы, 18 возражений) перенесено, get_knowledge()
указывает источники, новый .md подхватывается без изменения кода,
оценка токенов логируется.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from bot.services import knowledge
from bot.services.knowledge import (
    CHARS_PER_TOKEN,
    TOKEN_WARN_LIMIT,
    get_knowledge,
    get_knowledge_stats,
    load_knowledge,
    reload_knowledge,
)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "bot" / "knowledge"

# Все 8 файлов из структуры проекта (ТЗ, Часть 4)
EXPECTED_FILES = [
    # Порядок алфавитный — в нём же get_knowledge() склеивает файлы.
    # Восемь исходных (ТЗ, Блок 2), шесть из пакета заказчика 2026-07-27
    # (каждый прямо адресован AI-помощнику) и внутренний регламент запретов,
    # который Юлия решением от 2026-07-27 сохранила отдельно от FAQ.
    "ai_safety.md",
    "brand.md",
    "brand_architecture.md",
    "cases.md",
    "communication.md",
    "content.md",
    "faq.md",
    "glossary.md",
    "objections.md",
    "products.md",
    "qualification.md",
    "routes.md",
    "stop_topics.md",
    "templates.md",
    "tone_of_voice.md",
]


@pytest.fixture(autouse=True)
def _reset_cache():
    """Каждый тест начинает с холодного кэша."""
    knowledge._cache = None
    knowledge._cache_dir = None
    yield
    knowledge._cache = None
    knowledge._cache_dir = None


def test_all_eight_files_exist_and_not_empty() -> None:
    """Критерий ТЗ: все 8 .md созданы; плюс документы пакета 2026-07-27."""
    for name in EXPECTED_FILES:
        path = KNOWLEDGE_DIR / name
        assert path.is_file(), f"нет файла базы знаний: {name}"
        assert path.read_text(encoding="utf-8").strip(), f"файл пуст: {name}"


def test_knowledge_contains_all_sources_in_alphabetical_order() -> None:
    """Критерий: get_knowledge() возвращает текст с указанием источников;
    файлы читаются в алфавитном порядке."""
    text = get_knowledge(KNOWLEDGE_DIR)
    positions = []
    for name in EXPECTED_FILES:
        marker = f"## Источник: {name}"
        assert marker in text, f"нет заголовка источника для {name}"
        positions.append(text.index(marker))
    assert positions == sorted(positions), "источники идут не в алфавитном порядке"


def test_prices_transferred_verbatim() -> None:
    """Критерий: все цены на месте (проверка дословности переноса)."""
    products = (KNOWLEDGE_DIR / "products.md").read_text(encoding="utf-8")
    for price, product in [
        ("15 000", "диагностика «Точка сбоя»"),
        ("20 000", "системная сессия / расстановка"),
        ("25 000", "бизнес-расстановка"),
        ("150 000", "пакет 8 встреч / проект «Под ключ»"),
        ("350 000", "сопровождение 20 встреч"),
        ("80 000", "совместный спринт"),
    ]:
        assert price in products, f"в products.md нет цены {price} ({product})"


def test_all_18_objections_present() -> None:
    """Критерий: все 18 возражений на месте (в PDF №13 есть, пропуска нет —
    правило ТЗ «ориентируйся на факт», решение зафиксировано в logs.txt)."""
    objections = (KNOWLEDGE_DIR / "objections.md").read_text(encoding="utf-8")
    for n in range(1, 19):
        assert f"Возражение №{n}" in objections, f"нет возражения №{n}"
    # Контрольные формулировки
    assert "«Это дорого.»" in objections
    assert "«Мне нужно всё обдумать.»" in objections  # №13, которого «нет» по ТЗ
    assert "«Почему сначала диагностика?»" in objections  # №18


def test_stop_phrases_present() -> None:
    """Критерий: все стоп-фразы на месте — их будет ловить валидатор (Блок 4)."""
    objections = (KNOWLEDGE_DIR / "objections.md").read_text(encoding="utf-8")
    for phrase in [
        "Если не купите сейчас — ничего не изменится.",
        "У вас точно родовая программа.",
        "Вам срочно нужна диагностика.",
        "Я знаю причину вашей проблемы.",
        "Это единственный способ решить ситуацию.",
        "Без моей помощи вы не справитесь.",
        "После работы всё обязательно изменится.",
        "Вы сами виноваты.",
    ]:
        assert phrase in objections, f"нет запрещённой фразы: {phrase}"
    # Перечень запретов живёт в отдельном регламенте: решением Юлии
    # от 2026-07-27 это внутренние правила системы, а не раздел FAQ
    safety = (KNOWLEDGE_DIR / "ai_safety.md").read_text(encoding="utf-8")
    for phrase in [
        "«Я знаю причину вашей проблемы»",
        "«После диагностики всё изменится»",
        "«Вам обязательно нужна эта услуга»",
        "«Я гарантирую результат»",
        "«Это точно родовая проблема»",
        "«Вам поможет только этот метод»",
    ]:
        assert phrase in safety, f"в ai_safety.md нет запрещённого ответа: {phrase}"


def test_every_forbidden_answer_is_actually_blocked() -> None:
    """Регламент и его исполнение не должны разойтись.

    Запрет, который есть в документе, но не ловится валидатором, — это
    обещание безопасности без самой безопасности: модель произнесёт фразу,
    и она уйдёт клиенту. Список берём из самого регламента, а не из кода,
    поэтому новый пункт в документе без поддержки в валидаторе уронит тест.
    """
    from bot.utils import validators

    safety = (KNOWLEDGE_DIR / "ai_safety.md").read_text(encoding="utf-8")
    forbidden = re.findall(r"^- «(.+?)»$", safety, re.M)
    assert len(forbidden) >= 6, f"перечень запретов не разобрался: {forbidden}"
    for phrase in forbidden:
        assert validators.find_stop_phrase(phrase), f"валидатор пропускает запрет: {phrase!r}"


def test_key_content_spot_checks() -> None:
    """Выборочная проверка обязательных фрагментов из ТЗ (Блок 2)."""
    brand = (KNOWLEDGE_DIR / "brand.md").read_text(encoding="utf-8")
    assert "не ставит медицинские диагнозы" in brand
    assert "выдавать себя за Юлию" in brand

    qualification = (KNOWLEDGE_DIR / "qualification.md").read_text(encoding="utf-8")
    assert "Порог для автоматического решения: 85%" in qualification
    assert "Немедленная передача Юлии" in qualification

    stop_topics = (KNOWLEDGE_DIR / "stop_topics.md").read_text(encoding="utf-8")
    assert "лечение заболеваний" in stop_topics

    communication = (KNOWLEDGE_DIR / "communication.md").read_text(encoding="utf-8")
    assert "Юлия не продаёт услуги" in communication

    templates = (KNOWLEDGE_DIR / "templates.md").read_text(encoding="utf-8")
    assert "Я AI-помощник Юлии Гейкиной" in templates

    products = (KNOWLEDGE_DIR / "products.md").read_text(encoding="utf-8")
    # Обе допустимые формулировки предложения диагностики — дословно
    assert "первым этапом обычно становится системная диагностика «Точка сбоя»" in products
    assert "первым шагом обычно становится системная диагностика." in products


def test_new_md_picked_up_without_code_change(tmp_path: Path) -> None:
    """Критерий: новый .md подхватывается без изменения кода."""
    (tmp_path / "a.md").write_text("Первый файл", encoding="utf-8")
    text = get_knowledge(tmp_path)
    assert "## Источник: a.md" in text
    assert "Первый файл" in text

    # Кэш: повторный вызов не видит новый файл до перезагрузки
    (tmp_path / "b.md").write_text("Второй файл", encoding="utf-8")
    assert "b.md" not in get_knowledge(tmp_path)

    reloaded = reload_knowledge(tmp_path)
    assert "## Источник: b.md" in reloaded
    assert reloaded.index("a.md") < reloaded.index("b.md")


def test_stats_counts_files_and_tokens(tmp_path: Path) -> None:
    """Статистика: файлов, размер, оценка токенов (len / 3 для русского)."""
    (tmp_path / "a.md").write_text("абв" * 30, encoding="utf-8")
    stats = get_knowledge_stats(tmp_path)
    assert stats["files"] == 1
    assert stats["file_names"] == ["a.md"]
    assert stats["total_chars"] == len(get_knowledge(tmp_path))
    assert stats["estimated_tokens"] == stats["total_chars"] // CHARS_PER_TOKEN


def test_token_estimate_logged_on_load(app_caplog: pytest.LogCaptureFixture) -> None:
    """Критерий: оценка токенов выводится в лог при загрузке."""
    with app_caplog.at_level(logging.INFO, logger="app"):
        load_knowledge(KNOWLEDGE_DIR)
    messages = " ".join(record.getMessage() for record in app_caplog.records)
    assert "токенов" in messages


def test_oversize_warning(tmp_path: Path, app_caplog: pytest.LogCaptureFixture) -> None:
    """Превышение 100 000 токенов — предупреждение в лог (сигнал перехода
    на векторную базу), а не ошибка."""
    (tmp_path / "big.md").write_text("х" * (TOKEN_WARN_LIMIT * CHARS_PER_TOKEN + 3), "utf-8")
    with app_caplog.at_level(logging.WARNING, logger="app"):
        text = load_knowledge(tmp_path)
    assert text  # база загружена, бот работает
    assert any("векторную базу" in r.getMessage() for r in app_caplog.records)


def test_real_knowledge_under_token_limit() -> None:
    """Текущая база укладывается в лимит: предупреждения на старте нет."""
    stats = get_knowledge_stats(KNOWLEDGE_DIR)
    assert stats["files"] == len(EXPECTED_FILES)
    assert stats["estimated_tokens"] < TOKEN_WARN_LIMIT
