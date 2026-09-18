#!/usr/bin/env python3
"""Import Telegram Desktop JSON export into OpenSearch.

Index name: {chat_id}_{slug}_{YYYY-MM-DD}
Example:    1393071168_mssqlplus1c_2025-07-18
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
import base64
import ssl

LOGGER = logging.getLogger("import_telegram")

CATALOG_INDEX = "kb_catalog"

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
            "snapshot_date": {"type": "date", "format": "yyyy-MM-dd"},
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
            "snapshot_date": {"type": "date", "format": "yyyy-MM-dd"},
            "message_count": {"type": "integer"},
            "skipped_count": {"type": "integer"},
            "date_min": {"type": "date"},
            "date_max": {"type": "date"},
            "source_file": {"type": "keyword"},
            "imported_at": {"type": "date"},
        }
    },
}


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


def parse_snapshot_date(value: str) -> str:
    if not INDEX_DATE_RE.match(value):
        raise argparse.ArgumentTypeError("snapshot date must be YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def derive_slug(path: Path, chat_id: str, chat_name: str, explicit: str | None) -> str:
    if explicit:
        return slugify(explicit)
    parent = path.parent.name
    prefix = f"{chat_id}_"
    if parent.startswith(prefix) and parent[len(prefix) :]:
        return slugify(parent[len(prefix) :])
    if parent and parent not in {".", "chats_history"}:
        return slugify(parent)
    return slugify(chat_name, fallback=f"chat{chat_id}")


def build_index_name(chat_id: str, slug: str, snapshot_date: str) -> str:
    name = f"{chat_id}_{slug}_{snapshot_date}".lower()
    if not INDEX_NAME_RE.match(name):
        raise ValueError(f"invalid OpenSearch index name: {name}")
    return name


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
    snapshot_date: str,
) -> dict[str, Any] | None:
    if message.get("type") != "message":
        return None
    text = flatten_text(message.get("text")).strip()
    if not text:
        return None
    timestamp = message.get("date") or None
    doc: dict[str, Any] = {
        "chat_id": str(chat_id),
        "chat_name": chat_name,
        "chat_type": chat_type,
        "slug": slug,
        "snapshot_date": snapshot_date,
        "message_id": int(message["id"]),
        "user_id": str(message.get("from_id") or ""),
        "user_name": message.get("from") or "",
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


def iter_documents(
    payload: dict[str, Any],
    source_path: Path,
    snapshot_date: str,
    slug_arg: str | None,
) -> tuple[str, str, str, str, list[dict[str, Any]], int]:
    chat_id = str(payload.get("id") or "")
    if not chat_id:
        raise ValueError("export JSON has no chat id")
    chat_name = str(payload.get("name") or f"chat-{chat_id}")
    chat_type = str(payload.get("type") or "unknown")
    slug = derive_slug(source_path, chat_id, chat_name, slug_arg)
    index_name = build_index_name(chat_id, slug, snapshot_date)
    docs: list[dict[str, Any]] = []
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
            snapshot_date=snapshot_date,
        )
        if doc is None:
            skipped += 1
            continue
        docs.append(doc)
    return index_name, chat_id, chat_name, slug, docs, skipped


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
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            if exc.code in ignore_status:
                raw = exc.read()
                return json.loads(raw.decode("utf-8")) if raw else {"status": exc.code}
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


def point_alias(client: OpenSearchHttp, alias: str, index_name: str) -> None:
    current = client.request("GET", f"/_alias/{alias}", ignore_status=(404,))
    actions: list[dict[str, Any]] = []
    if isinstance(current, dict) and "error" not in current:
        for old_index in current:
            if old_index == "status":
                continue
            actions.append({"remove": {"index": old_index, "alias": alias}})
    actions.append({"add": {"index": index_name, "alias": alias}})
    client.request("POST", "/_aliases", {"actions": actions})


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
    snapshot_date: str,
    message_count: int,
    skipped_count: int,
    source_file: str,
    docs: list[dict[str, Any]],
) -> None:
    timestamps = [doc["timestamp"] for doc in docs if doc.get("timestamp")]
    body = {
        "index_name": index_name,
        "alias": alias,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "chat_type": chat_type,
        "slug": slug,
        "snapshot_date": snapshot_date,
        "message_count": message_count,
        "skipped_count": skipped_count,
        "date_min": min(timestamps) if timestamps else None,
        "date_max": max(timestamps) if timestamps else None,
        "source_file": source_file,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    client.request(
        "PUT",
        f"/{CATALOG_INDEX}/_doc/{index_name}?refresh=true",
        body,
    )


def load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Telegram JSON into OpenSearch")
    parser.add_argument("--file", required=True, help="Path to Telegram Desktop result.json")
    parser.add_argument(
        "--snapshot-date",
        type=parse_snapshot_date,
        default=date.today().isoformat(),
        help="Snapshot date YYYY-MM-DD used in the index name (default: today)",
    )
    parser.add_argument("--slug", help="Override chat slug used in the index name")
    parser.add_argument("--index-name", help="Override the full index name")
    parser.add_argument("--recreate", action="store_true", help="Delete the index before import")
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

    LOGGER.info("reading %s", source)
    with source.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    index_name, chat_id, chat_name, slug, docs, skipped = iter_documents(
        payload, source, args.snapshot_date, args.slug
    )
    if args.index_name:
        index_name = args.index_name.lower()
        if not INDEX_NAME_RE.match(index_name):
            LOGGER.error("invalid --index-name: %s", index_name)
            return 2
    alias = f"{chat_id}_{slug}".lower()

    LOGGER.info(
        "chat=%s id=%s slug=%s snapshot=%s index=%s alias=%s messages=%s skipped=%s",
        chat_name,
        chat_id,
        slug,
        args.snapshot_date,
        index_name,
        alias,
        len(docs),
        skipped,
    )
    if args.dry_run:
        return 0
    if not docs:
        LOGGER.error("no messages to index")
        return 1

    client = OpenSearchHttp()
    if not client.ping():
        LOGGER.error("OpenSearch ping failed")
        return 1

    ensure_index(client, CATALOG_INDEX, CATALOG_MAPPING, recreate=False)
    ensure_index(client, index_name, MESSAGES_MAPPING, recreate=args.recreate)
    indexed = bulk_index(client, index_name, docs)
    client.request("POST", f"/{index_name}/_refresh")
    point_alias(client, alias, index_name)
    write_catalog(
        client,
        index_name=index_name,
        alias=alias,
        chat_id=chat_id,
        chat_name=chat_name,
        chat_type=str(payload.get("type") or "unknown"),
        slug=slug,
        snapshot_date=args.snapshot_date,
        message_count=indexed,
        skipped_count=skipped,
        source_file=str(source),
        docs=docs,
    )
    LOGGER.info("indexed %s messages into %s (alias %s)", indexed, index_name, alias)
    return 0


if __name__ == "__main__":
    sys.exit(main())
