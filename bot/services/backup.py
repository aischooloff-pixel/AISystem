"""Экспорт всех таблиц Airtable в CSV (Блок 10).

Запуск (используется backup.sh, можно вручную):

    python -m bot.services.backup --output backups/data

Данные клиентов — самое ценное в системе; при недоступности любой из
таблиц скрипт завершается с кодом 1, чтобы backup.sh (set -e) не создал
архив, который выглядит полным, но им не является.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from bot.config import load_config
from bot.services import airtable
from bot.services.airtable import init_airtable
from bot.utils.logger import get_app_logger, setup_logging

logger = get_app_logger()

TABLES = ("Contacts", "Touches", "Comments", "Posts", "Tasks")


async def export_all(output_dir: Path) -> list[str]:
    """Экспортирует все таблицы; возвращает список недоступных."""
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config()
    tables = (
        config.airtable_contacts_table,
        config.airtable_touches_table,
        config.airtable_comments_table,
        config.airtable_posts_table,
        config.airtable_tasks_table,
    )
    failed: list[str] = []
    for table in tables:
        csv_text = await airtable.export_table_to_csv(table)
        if csv_text is None:
            failed.append(table)
            continue
        path = output_dir / f"{table}.csv"
        path.write_text(csv_text, encoding="utf-8")
        logger.info("Экспортирована таблица %s → %s (%d байт)", table, path, len(csv_text))
    return failed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт таблиц Airtable в CSV")
    parser.add_argument("--output", required=True, help="каталог для CSV-файлов")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.log_dir, config.log_level)
    init_airtable(config)
    try:
        failed = await export_all(Path(args.output))
    finally:
        await airtable.get_client().close()
    if failed:
        print(f"ОШИБКА: не выгружены таблицы: {', '.join(failed)}", file=sys.stderr)
        raise SystemExit(1)
    print(f"✅ Все таблицы выгружены в {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
