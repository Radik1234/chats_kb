#!/usr/bin/env python3
"""Migrate legacy daily OpenSearch indices to monthly ones.

Old scheme: one index per day    {chat_id}_{slug}_{YYYY-MM-DD}
New scheme: one index per month  {chat_id}_{slug}_{YYYY-MM}

For every daily index the script:
  1. ensures the target month index exists (messages mapping),
  2. reindexes the daily index into the month index (``_id`` is preserved,
     so this is safe to re-run),
  3. points the chat alias at the month index and drops the daily index from it,
  4. deletes the now-empty daily index (freeing one primary shard each),
  5. rebuilds the ``kb_catalog`` registry entry for the month.

The calendar day of every message stays in the ``message_date`` field, so
"per day" queries become a filter on ``message_date`` instead of an index name.

Usage:
    python scripts/migrate_daily_to_monthly.py            # migrate everything
    python scripts/migrate_daily_to_monthly.py --dry-run  # show the plan only
    python scripts/migrate_daily_to_monthly.py --chat 1393071168_mssqlplus1c
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "import"))
from import_telegram import (  # noqa: E402
    CATALOG_INDEX,
    CATALOG_MAPPING,
    MESSAGES_MAPPING,
    OpenSearchHttp,
    add_to_alias,
    ensure_cluster_settings,
    ensure_index,
    existing_days_in_index,
    index_month_stats,
    load_dotenv,
    write_catalog,
)

LOGGER = logging.getLogger("migrate_daily_to_monthly")

DAILY_RE = re.compile(r"^(?P<prefix>.+)_(?P<date>\d{4}-\d{2}-\d{2})$")


def list_daily_indices(client: OpenSearchHttp) -> list[str]:
    raw = client.request("GET", "/_cat/indices?h=index&format=json", ignore_status=(404,))
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("index") or "")
                if DAILY_RE.match(name):
                    names.append(name)
    return sorted(names)


def group_daily_by_month(names: list[str], chat_filter: str | None) -> dict[str, list[str]]:
    """Map ``{prefix}_{YYYY-MM}`` month index -> list of daily source indices."""
    by_month: dict[str, list[str]] = defaultdict(list)
    for name in names:
        match = DAILY_RE.match(name)
        if not match:
            continue
        prefix = match.group("prefix")  # {chat_id}_{slug}
        if chat_filter and prefix != chat_filter:
            continue
        month = match.group("date")[:7]
        month_index = f"{prefix}_{month}"
        by_month[month_index].append(name)
    return dict(by_month)


def reindex(client: OpenSearchHttp, source: str, dest: str) -> int:
    body = {
        "source": {"index": source},
        "dest": {"index": dest, "op_type": "index"},
        "conflicts": "proceed",
    }
    result = client.request("POST", "/_reindex?refresh=true&wait_for_completion=true", body)
    if isinstance(result, dict):
        failures = result.get("failures") or []
        if failures:
            raise RuntimeError(f"reindex {source} -> {dest} failed: {failures[:3]}")
        return int(result.get("created", 0)) + int(result.get("updated", 0))
    return 0


def sample_chat_meta(client: OpenSearchHttp, month_index: str) -> dict[str, str]:
    raw = client.request("POST", f"/{month_index}/_search", {"size": 1}, ignore_status=(404,))
    hits = raw.get("hits", {}).get("hits", []) if isinstance(raw, dict) else []
    src = hits[0].get("_source", {}) if hits else {}
    return {
        "chat_id": str(src.get("chat_id") or ""),
        "chat_name": str(src.get("chat_name") or ""),
        "chat_type": str(src.get("chat_type") or "unknown"),
        "slug": str(src.get("slug") or ""),
    }


def remove_from_alias(client: OpenSearchHttp, alias: str, index_name: str) -> None:
    client.request(
        "POST",
        "/_aliases",
        {"actions": [{"remove": {"index": index_name, "alias": alias}}]},
        ignore_status=(404,),
    )


def delete_daily_catalog(client: OpenSearchHttp, daily_indices: list[str]) -> None:
    if not daily_indices:
        return
    client.request(
        "POST",
        f"/{CATALOG_INDEX}/_delete_by_query?refresh=true",
        {"query": {"terms": {"index_name": daily_indices}}},
        ignore_status=(404,),
    )


def migrate_month(
    client: OpenSearchHttp,
    month_index: str,
    daily_indices: list[str],
    dry_run: bool,
) -> tuple[int, int]:
    month = month_index.rsplit("_", 1)[-1]
    alias = month_index.rsplit("_", 1)[0]  # {chat_id}_{slug}
    LOGGER.info(
        "month %s <- %s daily index(es) [alias %s]", month_index, len(daily_indices), alias
    )
    if dry_run:
        for src in daily_indices:
            LOGGER.info("  dry-run reindex %s -> %s", src, month_index)
        return 0, len(daily_indices)

    ensure_index(client, month_index, MESSAGES_MAPPING, recreate=False)
    moved = 0
    for src in sorted(daily_indices):
        moved += reindex(client, src, month_index)
    client.request("POST", f"/{month_index}/_refresh")

    add_to_alias(client, alias, month_index)
    for src in daily_indices:
        remove_from_alias(client, alias, src)

    meta = sample_chat_meta(client, month_index)
    days = sorted(existing_days_in_index(client, month_index))
    count, date_min, date_max = index_month_stats(client, month_index)
    write_catalog(
        client,
        index_name=month_index,
        alias=alias,
        chat_id=meta["chat_id"],
        chat_name=meta["chat_name"],
        chat_type=meta["chat_type"],
        slug=meta["slug"],
        month=month,
        days=days,
        message_count=count,
        skipped_count=0,
        source_file="migrate_daily_to_monthly",
        date_min=date_min,
        date_max=date_max,
    )

    deleted = 0
    for src in daily_indices:
        client.request("DELETE", f"/{src}", ignore_status=(404,))
        deleted += 1
    delete_daily_catalog(client, daily_indices)
    LOGGER.info(
        "  done %s: %s docs, %s day(s), deleted %s daily index(es)",
        month_index,
        count,
        len(days),
        deleted,
    )
    return moved, deleted


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate daily OpenSearch indices to monthly")
    parser.add_argument("--chat", help="Only migrate this {chat_id}_{slug} prefix")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan, change nothing")
    parser.add_argument(
        "--skip-shard-limit",
        action="store_true",
        help="Do not lower cluster.max_shards_per_node after migration",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    os.environ.setdefault("OPENSEARCH_URL", "https://127.0.0.1:9200")
    args = parse_args(argv)

    client = OpenSearchHttp()
    if not client.ping():
        LOGGER.error("OpenSearch ping failed")
        return 1

    daily = list_daily_indices(client)
    plan = group_daily_by_month(daily, args.chat)
    if not plan:
        LOGGER.info("no daily indices to migrate")
        return 0

    total_daily = sum(len(v) for v in plan.values())
    LOGGER.info(
        "found %s daily index(es) -> %s month index(es)%s",
        total_daily,
        len(plan),
        " (dry-run)" if args.dry_run else "",
    )

    if not args.dry_run:
        ensure_index(client, CATALOG_INDEX, CATALOG_MAPPING, recreate=False)

    moved_total = 0
    deleted_total = 0
    for month_index in sorted(plan):
        moved, deleted = migrate_month(client, month_index, plan[month_index], args.dry_run)
        moved_total += moved
        deleted_total += deleted

    if not args.dry_run and not args.skip_shard_limit:
        ensure_cluster_settings(client)
        LOGGER.info(
            "set cluster.max_shards_per_node=%s",
            os.environ.get("OPENSEARCH_MAX_SHARDS_PER_NODE", "1000"),
        )

    LOGGER.info(
        "migration %s: %s month index(es), %s docs reindexed, %s daily index(es) removed",
        "planned" if args.dry_run else "done",
        len(plan),
        moved_total,
        deleted_total,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
