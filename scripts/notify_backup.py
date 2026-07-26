"""Уведомление Юлии о результате бэкапа (Блок 10). Вызывается из backup.sh:

python -m scripts.notify_backup --ok /path/backup_X.tar.gz   # успех
python -m scripts.notify_backup --fail "текст ошибки"        # ошибка
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from aiogram import Bot

from bot.config import load_config
from bot.utils.helpers import MSK


async def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ok", metavar="ARCHIVE", help="путь к созданному архиву")
    group.add_argument("--fail", metavar="ERROR", help="текст ошибки бэкапа")
    args = parser.parse_args()

    config = load_config()
    if args.ok:
        size_mb = Path(args.ok).stat().st_size / 1024 / 1024
        stamp = datetime.now(MSK).strftime("%d.%m")
        text = f"Резервная копия создана: {stamp}, размер {size_mb:.1f} МБ"
    else:
        text = f"⚠️ ОШИБКА резервного копирования: {args.fail}"

    bot = Bot(token=config.telegram_bot_token)
    try:
        await bot.send_message(config.telegram_admin_id, text)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
