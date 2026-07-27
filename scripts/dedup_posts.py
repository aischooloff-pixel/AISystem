"""Схлопывает дубли в Posts, оставшиеся от гонки до коммита 1cb8735.

Гонка исправлена (пер-постовый замок в ``AirtableClient``), но записи,
созданные до исправления, остались в базе и искажают аналитику по постам.

Скрипт группирует Posts по ``post_id``, оставляет самую полную запись,
пересчитывает ``comments_count`` и ``potential_clients_count`` по фактическим
строкам в Comments и удаляет остальные. Пересчёт важен: инкременты
разошлись по дублям, поэтому счётчики оставшейся записи тоже занижены.

    python -m scripts.dedup_posts            # показать план, ничего не менять
    python -m scripts.dedup_posts --apply    # выполнить
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import httpx

from scripts.setup_airtable import load_settings

API = "https://api.airtable.com/v0"


def _fetch_all(base: str, table: str, headers: dict) -> list[dict]:
    """Все записи таблицы с постраничным обходом."""
    records: list[dict] = []
    params: dict = {}
    while True:
        response = httpx.get(f"{API}/{base}/{table}", headers=headers, params=params, timeout=30)
        response.raise_for_status()
        page = response.json()
        records.extend(page.get("records", []))
        offset = page.get("offset")
        if not offset:
            return records
        params["offset"] = offset


def _filled(record: dict) -> int:
    """Сколько полей записи реально заполнено — критерий «самой полной»."""
    return sum(1 for value in record["fields"].values() if value not in (None, "", 0, []))


def main() -> None:
    parser = argparse.ArgumentParser(description="Схлопывание дублей в Posts")
    parser.add_argument("--apply", action="store_true", help="выполнить (без флага — только план)")
    args = parser.parse_args()

    settings = load_settings()
    headers = {"Authorization": f"Bearer {settings.airtable_api_key}"}
    base = settings.airtable_base_id

    posts = _fetch_all(base, "Posts", headers)
    comments = _fetch_all(base, "Comments", headers)

    by_post: dict[str, list[dict]] = defaultdict(list)
    for post in posts:
        post_id = post["fields"].get("post_id")
        if post_id:
            by_post[str(post_id)].append(post)

    comment_totals: dict[str, int] = defaultdict(int)
    potential_totals: dict[str, int] = defaultdict(int)
    for comment in comments:
        post_id = comment["fields"].get("post_id")
        if not post_id:
            continue
        comment_totals[str(post_id)] += 1
        if comment["fields"].get("is_potential_client"):
            potential_totals[str(post_id)] += 1

    to_delete: list[str] = []
    to_update: list[tuple[str, str, dict]] = []

    for post_id, group in sorted(by_post.items()):
        keep = max(group, key=_filled)
        duplicates = [p for p in group if p["id"] != keep["id"]]
        counters = {
            "comments_count": comment_totals.get(post_id, 0),
            "potential_clients_count": potential_totals.get(post_id, 0),
        }
        if duplicates:
            print(f"post_id={post_id}: записей {len(group)} → 1")
            print(f"  оставляем {keep['id']} (заполнено полей: {_filled(keep)})")
            # Уникальные данные в дублях — повод остановиться и разобрать руками
            for dup in duplicates:
                extra = {
                    k: v
                    for k, v in dup["fields"].items()
                    if k not in keep["fields"] and v not in (None, "", 0, [])
                }
                if extra:
                    print(f"  ⚠️  {dup['id']} содержит поля, которых нет у оставляемой: {extra}")
            to_delete.extend(d["id"] for d in duplicates)
        current = {k: keep["fields"].get(k) for k in counters}
        if current != counters:
            print(f"  счётчики {post_id}: {current} → {counters}")
            to_update.append((post_id, keep["id"], counters))

    if not to_delete and not to_update:
        print("Дублей нет, счётчики сходятся — делать нечего.")
        return

    print(f"\nИтого: удалить {len(to_delete)}, обновить счётчиков {len(to_update)}")
    if not args.apply:
        print("Это план. Для выполнения повторите с --apply.")
        return

    for _post_id, record_id, counters in to_update:
        response = httpx.patch(
            f"{API}/{base}/Posts/{record_id}",
            headers=headers,
            json={"fields": counters, "typecast": True},
            timeout=30,
        )
        print(f"обновлено {record_id}: HTTP {response.status_code}")

    # Airtable удаляет не больше 10 записей за запрос
    for chunk_start in range(0, len(to_delete), 10):
        chunk = to_delete[chunk_start : chunk_start + 10]
        response = httpx.delete(
            f"{API}/{base}/Posts",
            headers=headers,
            params=[("records[]", record_id) for record_id in chunk],
            timeout=30,
        )
        if response.status_code != 200:
            print(f"ОШИБКА удаления {chunk}: HTTP {response.status_code} {response.text[:200]}")
            sys.exit(1)
        print(f"удалено {len(chunk)}: HTTP {response.status_code}")

    print("Готово.")


if __name__ == "__main__":
    main()
