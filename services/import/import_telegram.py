#!/usr/bin/env python3
"""Import Telegram Desktop JSON into monthly OpenSearch indices.

Index name: {chat_id}_{slug}_{YYYY-MM}  (calendar month of the message)
Example:    1393071168_mssqlplus1c_2026-09

The calendar day of every message is preserved in the ``message_date`` field, so
"per day" queries are a filter on ``message_date`` instead of an index per day.
Import stays idempotent per day: days already present in a month index are not
re-indexed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
import base64
import ssl

LOGGER = logging.getLogger("import_telegram")

CATALOG_INDEX = "kb_catalog"
DEFAULT_CHUNK_INTERVAL_SEC = 10
DEFAULT_MAX_SHARDS_PER_NODE = 1000

KNOWN_BOTS = {
    "gif",
    "banofbot",
    "botfather",
    "stickers",
    "gamee",
    "gifbot",
    "vid",
    "like",
    "bing",
    "previews",
}

LINK_RE = re.compile(r"https?://|t\.me/", re.IGNORECASE)
LOG_RE = re.compile(
    r"(stack trace|caused by:|\tat |\bEXCP\b|\bDBMSSQL\b|\bSDBL\b|"
    r"Msg\s+\d+|SQLSTATE|\bHRESULT\b|TraceId| tecolog)",
    re.IGNORECASE,
)
ERROR_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9]+(?:Exception|Error|Fault)\b"
    r"|HRESULT:\s*0x[0-9A-Fa-f]+"
    r"|Msg\s+\d+"
    r"|SQLSTATE\s+[A-Z0-9]+",
)
SLUG_RE = re.compile(r"[^a-z0-9]+")
INDEX_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INDEX_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
INDEX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,200}$")

MESSAGES_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "refresh_interval": "5s",
        "analysis": {
            "filter": {
                "russian_stop": {"type": "stop", "stopwords": "_russian_"},
                "russian_stemmer": {"type": "stemmer", "language": "russian"},
                "english_stop": {"type": "stop", "stopwords": "_english_"},
                "english_stemmer": {"type": "stemmer", "language": "english"},
            },
            "analyzer": {
                "ru_en": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": [
                        "lowercase",
                        "russian_stop",
                        "russian_stemmer",
                        "english_stop",
                        "english_stemmer",
                    ],
                }
            },
        },
    },
    "mappings": {
        "properties": {
            "chat_id": {"type": "keyword"},
            "chat_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "chat_type": {"type": "keyword"},
            "slug": {"type": "keyword"},
            "message_date": {"type": "date", "format": "yyyy-MM-dd"},
            "message_id": {"type": "long"},
            "user_id": {"type": "keyword"},
            "user_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "timestamp": {"type": "date"},
            "reply_to_message_id": {"type": "long"},
            "text": {"type": "text", "analyzer": "ru_en"},
            "has_links": {"type": "boolean"},
            "has_logs": {"type": "boolean"},
            "has_photo": {"type": "boolean"},
            "is_bot": {"type": "boolean"},
            "error_codes": {"type": "keyword"},
            "media_type": {"type": "keyword"},
            "file_name": {"type": "keyword"},
            "forwarded_from": {"type": "keyword"},
            "via_bot": {"type": "keyword"},
        }
    },
}

CATALOG_MAPPING: dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "index_name": {"type": "keyword"},
            "alias": {"type": "keyword"},
            "chat_id": {"type": "keyword"},
            "chat_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "chat_type": {"type": "keyword"},
            "slug": {"type": "keyword"},
            "month": {"type": "keyword"},
            "days": {"type": "keyword"},
            "message_count": {"type": "integer"},
            "skipped_count": {"type": "integer"},
            "date_min": {"type": "date"},
            "date_max": {"type": "date"},
            "source_file": {"type": "keyword"},
            "imported_at": {"type": "date"},
        }
    },
}


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(flatten_text(item.get("text")))
        return "".join(parts)
    return str(value)


def slugify(value: str, fallback: str = "chat") -> str:
    slug = SLUG_RE.sub("", value.lower().replace("+", "plus").replace("&", "and"))
    slug = slug.strip("_") or fallback
    return slug[:80]


def derive_slug(path: Path, chat_id: str, chat_name: str, explicit: str | None) -> str:
    if explicit:
        return slugify(explicit)
    parent = path.parent.name
    prefix = f"{chat_id}_"
    if parent.startswith(prefix) and parent[len(prefix) :]:
        return slugify(parent[len(prefix) :])
    skip = {".", "chats_history", "inbox", "processing", "archive", "failed", "telegram"}
    if parent and parent not in skip:
        return slugify(parent)
    return slugify(chat_name, fallback=f"chat{chat_id}")


def build_index_name(chat_id: str, slug: str, month: str) -> str:
    if not INDEX_MONTH_RE.match(month):
        raise ValueError(f"invalid month for index name: {month}")
    name = f"{chat_id}_{slug}_{month}".lower()
    if not INDEX_NAME_RE.match(name):
        raise ValueError(f"invalid OpenSearch index name: {name}")
    return name


def message_day(message: dict[str, Any]) -> str | None:
    raw = str(message.get("date") or "")
    day = raw[:10]
    if INDEX_DATE_RE.match(day):
        return day
    return None


def day_to_month(day: str) -> str:
    return day[:7]


def extract_error_codes(text: str) -> list[str]:
    seen: list[str] = []
    for match in ERROR_RE.findall(text):
        if match not in seen:
            seen.append(match)
    return seen[:20]


def is_bot_message(message: dict[str, Any]) -> bool:
    via = str(message.get("via_bot") or "").lstrip("@").lower()
    if via and (via in KNOWN_BOTS or via.endswith("bot")):
        return True
    name = str(message.get("from") or "").lower()
    if name.endswith("bot") or name.endswith("бот"):
        return True
    return False


def entity_has_link(entities: Any) -> bool:
    if not isinstance(entities, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") in {"link", "url", "text_link", "email", "mention"}
        for item in entities
    )


def normalize_message(
    message: dict[str, Any],
    *,
    chat_id: str,
    chat_name: str,
    chat_type: str,
    slug: str,
    skip_service: bool,
    skip_empty: bool,
) -> dict[str, Any] | None:
    if skip_service and message.get("type") != "message":
        return None
    if message.get("type") not in {"message", "service"}:
        return None
    text = flatten_text(message.get("text")).strip()
    if skip_empty and not text:
        return None
    day = message_day(message)
    if not day:
        return None
    timestamp = message.get("date") or None
    doc: dict[str, Any] = {
        "chat_id": str(chat_id),
        "chat_name": chat_name,
        "chat_type": chat_type,
        "slug": slug,
        "message_date": day,
        "message_id": int(message["id"]),
        "user_id": str(message.get("from_id") or message.get("actor_id") or ""),
        "user_name": message.get("from") or message.get("actor") or "",
        "timestamp": timestamp,
        "text": text,
        "has_links": bool(LINK_RE.search(text) or entity_has_link(message.get("text_entities"))),
        "has_logs": bool(LOG_RE.search(text)),
        "has_photo": "photo" in message,
        "is_bot": is_bot_message(message),
        "error_codes": extract_error_codes(text),
        "media_type": message.get("media_type") or None,
        "file_name": message.get("file_name") or None,
        "forwarded_from": message.get("forwarded_from") or None,
        "via_bot": message.get("via_bot") or None,
    }
    reply_to = message.get("reply_to_message_id")
    if reply_to is not None:
        doc["reply_to_message_id"] = int(reply_to)
    return doc


def group_documents_by_month(
    payload: dict[str, Any],
    source_path: Path,
    slug_arg: str | None,
) -> tuple[str, str, str, str, dict[str, list[dict[str, Any]]], int]:
    chat_id = str(payload.get("id") or "")
    if not chat_id:
        raise ValueError("export JSON has no chat id")
    chat_name = str(payload.get("name") or f"chat-{chat_id}")
    chat_type = str(payload.get("type") or "unknown")
    slug = derive_slug(source_path, chat_id, chat_name, slug_arg)
    skip_service = env_flag("INGEST_SKIP_SERVICE", True)
    skip_empty = env_flag("INGEST_SKIP_EMPTY", True)
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped = 0
    for raw in payload.get("messages") or []:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        doc = normalize_message(
            raw,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            slug=slug,
            skip_service=skip_service,
            skip_empty=skip_empty,
        )
        if doc is None:
            skipped += 1
            continue
        by_month[day_to_month(str(doc["message_date"]))].append(doc)
    return chat_id, chat_name, chat_type, slug, dict(by_month), skipped


class OpenSearchHttp:
    def __init__(self) -> None:
        self.base = os.environ.get("OPENSEARCH_URL", "https://127.0.0.1:9200").rstrip("/")
        username = os.environ.get("OPENSEARCH_USERNAME", "admin")
        password = os.environ.get("OPENSEARCH_PASSWORD") or os.environ.get(
            "OPENSEARCH_INITIAL_ADMIN_PASSWORD", ""
        )
        if not password:
            raise RuntimeError("OPENSEARCH_PASSWORD is not set")
        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.auth = f"Basic {basic}"
        ctx = ssl._create_unverified_context()
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ctx))

    def request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        content_type: str = "application/json",
        ignore_status: tuple[int, ...] = (),
    ) -> Any:
        data = None
        headers = {"Authorization": self.auth}
        if body is not None:
            payload = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
            data = payload
            headers["Content-Type"] = content_type
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=120) as resp:
                raw = resp.read()
                if method == "HEAD":
                    return {"status": resp.status}
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"raw": raw.decode("utf-8", errors="replace")}
        except HTTPError as exc:
            if exc.code in ignore_status:
                raw = exc.read()
                if not raw:
                    return {"status": exc.code}
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"status": exc.code}
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc

    def ping(self) -> bool:
        try:
            self.request("GET", "/")
            return True
        except RuntimeError:
            return False

    def index_exists(self, name: str) -> bool:
        result = self.request("GET", f"/{name}", ignore_status=(404,))
        if isinstance(result, dict) and (result.get("status") == 404 or "error" in result):
            return False
        return True


def ensure_index(client: OpenSearchHttp, name: str, mapping: dict[str, Any], recreate: bool) -> None:
    exists = client.index_exists(name)
    if exists and recreate:
        LOGGER.info("deleting index %s", name)
        client.request("DELETE", f"/{name}")
        exists = False
    if not exists:
        LOGGER.info("creating index %s", name)
        client.request("PUT", f"/{name}", mapping)


def add_to_alias(client: OpenSearchHttp, alias: str, index_name: str) -> None:
    current = client.request("GET", f"/_alias/{alias}", ignore_status=(404,))
    if isinstance(current, dict) and index_name in current and "error" not in current:
        return
    client.request("POST", "/_aliases", {"actions": [{"add": {"index": index_name, "alias": alias}}]})


def existing_days_in_index(client: OpenSearchHttp, index_name: str) -> set[str]:
    """Return the set of ``message_date`` days already present in a month index."""
    if not client.index_exists(index_name):
        return set()
    body = {
        "size": 0,
        "aggs": {"days": {"terms": {"field": "message_date", "size": 40}}},
    }
    raw = client.request("POST", f"/{index_name}/_search", body, ignore_status=(404,))
    days: set[str] = set()
    if not isinstance(raw, dict):
        return days
    buckets = (
        raw.get("aggregations", {}).get("days", {}).get("buckets", [])
        if isinstance(raw.get("aggregations"), dict)
        else []
    )
    for bucket in buckets:
        key = bucket.get("key_as_string") or bucket.get("key")
        if key is None:
            continue
        day = str(key)[:10]
        if INDEX_DATE_RE.match(day):
            days.add(day)
    return days


def index_month_stats(client: OpenSearchHttp, index_name: str) -> tuple[int, str | None, str | None]:
    """Return (doc_count, date_min, date_max) for a month index."""
    body = {
        "size": 0,
        "track_total_hits": True,
        "aggs": {
            "tmin": {"min": {"field": "timestamp"}},
            "tmax": {"max": {"field": "timestamp"}},
        },
    }
    raw = client.request("POST", f"/{index_name}/_search", body, ignore_status=(404,))
    if not isinstance(raw, dict):
        return 0, None, None
    total = raw.get("hits", {}).get("total", {})
    count = int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)
    aggs = raw.get("aggregations", {}) if isinstance(raw.get("aggregations"), dict) else {}
    date_min = aggs.get("tmin", {}).get("value_as_string")
    date_max = aggs.get("tmax", {}).get("value_as_string")
    return count, date_min, date_max


def ensure_cluster_settings(client: OpenSearchHttp) -> None:
    max_shards = env_int("OPENSEARCH_MAX_SHARDS_PER_NODE", DEFAULT_MAX_SHARDS_PER_NODE)
    client.request(
        "PUT",
        "/_cluster/settings",
        {"persistent": {"cluster.max_shards_per_node": max_shards}},
    )


def bulk_index(client: OpenSearchHttp, index_name: str, docs: Iterable[dict[str, Any]]) -> int:
    indexed = 0
    batch: list[dict[str, Any]] = []
    for doc in docs:
        batch.append(doc)
        if len(batch) >= 500:
            indexed += _flush_bulk(client, index_name, batch)
            batch = []
    if batch:
        indexed += _flush_bulk(client, index_name, batch)
    return indexed


def _flush_bulk(client: OpenSearchHttp, index_name: str, batch: list[dict[str, Any]]) -> int:
    lines: list[str] = []
    for doc in batch:
        meta = {"index": {"_index": index_name, "_id": f"{doc['chat_id']}_{doc['message_id']}"}}
        lines.append(json.dumps(meta, ensure_ascii=False))
        lines.append(json.dumps(doc, ensure_ascii=False))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    result = client.request("POST", "/_bulk", payload, content_type="application/x-ndjson")
    if result.get("errors"):
        failed = [item for item in result.get("items", []) if "error" in item.get("index", {})]
        LOGGER.error("bulk errors: %s", failed[:5])
        raise RuntimeError(f"bulk indexing failed for {len(failed)} documents")
    return len(batch)


def write_catalog(
    client: OpenSearchHttp,
    *,
    index_name: str,
    alias: str,
    chat_id: str,
    chat_name: str,
    chat_type: str,
    slug: str,
    month: str,
    days: list[str],
    message_count: int,
    skipped_count: int,
    source_file: str,
    date_min: str | None,
    date_max: str | None,
) -> None:
    body = {
        "index_name": index_name,
        "alias": alias,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "chat_type": chat_type,
        "slug": slug,
        "month": month,
        "days": days,
        "message_count": message_count,
        "skipped_count": skipped_count,
        "date_min": date_min,
        "date_max": date_max,
        "source_file": source_file,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    client.request("PUT", f"/{CATALOG_INDEX}/_doc/{index_name}?refresh=true", body)


def load_dotenv() -> None:
    start = Path(__file__).resolve().parent
    env_path = None
    for folder in [start, *start.parents]:
        candidate = folder / ".env"
        if candidate.is_file():
            env_path = candidate
            break
    if env_path is None:
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def import_payload(
    payload: dict[str, Any],
    source_path: Path,
    *,
    client: OpenSearchHttp | None = None,
    slug: str | None = None,
    chunk_interval_sec: float | None = None,
    recreate: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    chat_id, chat_name, chat_type, resolved_slug, by_month, skipped = group_documents_by_month(
        payload, source_path, slug
    )
    alias = f"{chat_id}_{resolved_slug}".lower()
    months = sorted(by_month)
    all_days = sorted({str(doc["message_date"]) for docs in by_month.values() for doc in docs})
    interval = (
        DEFAULT_CHUNK_INTERVAL_SEC
        if chunk_interval_sec is None
        else chunk_interval_sec
    )
    LOGGER.info(
        "chat=%s id=%s slug=%s months=%s days=%s messages=%s skipped=%s alias=%s",
        chat_name,
        chat_id,
        resolved_slug,
        len(months),
        len(all_days),
        sum(len(v) for v in by_month.values()),
        skipped,
        alias,
    )
    if dry_run:
        for month in months:
            file_days = sorted({str(doc["message_date"]) for doc in by_month[month]})
            LOGGER.info(
                "dry-run month %s -> %s (%s msgs, %s days)",
                month,
                build_index_name(chat_id, resolved_slug, month),
                len(by_month[month]),
                len(file_days),
            )
        return {
            "chat_id": chat_id,
            "slug": resolved_slug,
            "alias": alias,
            "months_in_file": months,
            "days_in_file": all_days,
            "loaded_months": [],
            "skipped_months": months,
            "loaded_days": [],
            "skipped_days": all_days,
            "skipped_messages": skipped,
        }

    if client is None:
        client = OpenSearchHttp()
    if not client.ping():
        raise RuntimeError("OpenSearch ping failed")
    ensure_cluster_settings(client)
    ensure_index(client, CATALOG_INDEX, CATALOG_MAPPING, recreate=False)

    loaded_months: list[str] = []
    skipped_months: list[str] = []
    loaded_days: list[str] = []
    skipped_days: list[str] = []
    for position, month in enumerate(months):
        docs = by_month[month]
        index_name = build_index_name(chat_id, resolved_slug, month)
        file_days = sorted({str(doc["message_date"]) for doc in docs})
        existing_days = set() if recreate else existing_days_in_index(client, index_name)
        docs_to_load = [doc for doc in docs if str(doc["message_date"]) not in existing_days]
        new_days = sorted({str(doc["message_date"]) for doc in docs_to_load})
        already_days = [day for day in file_days if day in existing_days]
        skipped_days.extend(already_days)

        if not docs_to_load and not recreate:
            LOGGER.info(
                "skip month %s: all %s day(s) already present in %s",
                month,
                len(file_days),
                index_name,
            )
            skipped_months.append(month)
            continue

        ensure_index(client, index_name, MESSAGES_MAPPING, recreate=recreate)
        indexed = bulk_index(client, index_name, docs_to_load)
        client.request("POST", f"/{index_name}/_refresh")
        add_to_alias(client, alias, index_name)
        count, date_min, date_max = index_month_stats(client, index_name)
        month_days = sorted(set(file_days) | existing_days) if not recreate else file_days
        write_catalog(
            client,
            index_name=index_name,
            alias=alias,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            slug=resolved_slug,
            month=month,
            days=month_days,
            message_count=count,
            skipped_count=skipped,
            source_file=str(source_path),
            date_min=date_min,
            date_max=date_max,
        )
        loaded_months.append(month)
        loaded_days.extend(new_days)
        LOGGER.info(
            "indexed %s new message(s) into %s (new days=%s, total docs=%s)",
            indexed,
            index_name,
            len(new_days),
            count,
        )
        if position < len(months) - 1 and interval > 0:
            LOGGER.info("sleep %.1fs before next month", interval)
            time.sleep(interval)

    return {
        "chat_id": chat_id,
        "slug": resolved_slug,
        "alias": alias,
        "months_in_file": months,
        "days_in_file": all_days,
        "loaded_months": loaded_months,
        "skipped_months": skipped_months,
        "loaded_days": loaded_days,
        "skipped_days": sorted(set(skipped_days)),
        "skipped_messages": skipped,
    }


def import_file(
    source_path: Path,
    *,
    client: OpenSearchHttp | None = None,
    slug: str | None = None,
    chunk_interval_sec: float | None = None,
    recreate: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    LOGGER.info("reading %s", source_path)
    with source_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("export JSON must be an object")
    return import_payload(
        payload,
        source_path,
        client=client,
        slug=slug,
        chunk_interval_sec=chunk_interval_sec,
        recreate=recreate,
        dry_run=dry_run,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Telegram JSON into daily OpenSearch indices")
    parser.add_argument("--file", required=True, help="Path to Telegram Desktop result.json")
    parser.add_argument("--slug", help="Override chat slug used in the index name")
    parser.add_argument(
        "--chunk-interval-sec",
        type=float,
        default=None,
        help="Pause between day chunks (default: INGEST_CHUNK_INTERVAL_SEC or 10)",
    )
    parser.add_argument("--recreate", action="store_true", help="Recreate day indices that will be loaded")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, do not write to OpenSearch")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    os.environ.setdefault("OPENSEARCH_URL", "https://127.0.0.1:9200")
    args = parse_args(argv)
    source = Path(args.file)
    if not source.is_file():
        LOGGER.error("file not found: %s", source)
        return 2
    interval = args.chunk_interval_sec
    if interval is None:
        interval = float(env_int("INGEST_CHUNK_INTERVAL_SEC", DEFAULT_CHUNK_INTERVAL_SEC))
    try:
        stats = import_file(
            source,
            slug=args.slug,
            chunk_interval_sec=interval,
            recreate=args.recreate,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1
    LOGGER.info(
        "done loaded_months=%s loaded_days=%s skipped_months=%s skipped_days=%s",
        len(stats["loaded_months"]),
        len(stats["loaded_days"]),
        len(stats["skipped_months"]),
        len(stats["skipped_days"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
