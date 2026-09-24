#!/usr/bin/env python3
"""Background backfill worker: top up chats already present in OpenSearch.

Single-threaded scheduled worker that, on each cycle:
  1. skips if an import is already in flight (files in ``processing/`` — or, by
     default, anything pending in ``inbox/``);
  2. discovers chats from OpenSearch aliases ``{chat_id}_{slug}`` (scheme
     independent: works for both daily and monthly index layouts);
  3. picks the most behind chat (smallest ``date_max``), honouring a per-chat
     recheck backoff so idle chats are not polled every cycle;
  4. computes the exact frontier ``max(message_id)`` for that chat and asks the
     Telegram source for messages newer than it (``min_id``), bounded to a
     window of ``BACKFILL_DAYS_PER_RUN`` days;
  5. writes a Telegram-Desktop-export-compatible JSON into
     ``inbox/{chat_id}_{slug}/`` where the existing ``ingest`` picks it up.

The frontier is a message id (not a date): it is exact and monotonic, so the
tail of the last, partially-loaded day is never lost to ingest's per-day
idempotency.

The Telegram fetch itself lives behind a ``source`` object (see
``telegram_source.TelethonSource``) so the whole worker is testable without a
real Telegram account by injecting a fake source.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

# Reuse the stdlib-only OpenSearch client from the ingest service. In the
# container both files sit flat in /app; on a dev host we add services/import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))
from import_telegram import OpenSearchHttp, load_dotenv  # noqa: E402

LOGGER = logging.getLogger("backfill_worker")

INGEST_ZONES = ("inbox", "processing", "archive", "failed", "logs")
STATE_DIRNAME = "state"
STATE_FILENAME = "backfill_state.json"
DATE_FMT = "%Y-%m-%dT%H:%M:%S"


class TelegramSource(Protocol):
    def fetch(
        self,
        chat_id: str,
        slug: str,
        min_id: int,
        until_date: datetime,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return (chat_meta, messages) for messages with id > ``min_id`` whose
        date is <= ``until_date``. ``chat_meta`` has name/type/id in Telegram
        Desktop export shape; ``messages`` is a list of export message dicts."""
        ...


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class BackfillConfig:
    def __init__(self) -> None:
        self.ingest_root = Path(os.environ.get("INGEST_ROOT", "/data/telegram"))
        self.days_per_run = env_int("BACKFILL_DAYS_PER_RUN", 7)
        self.interval_sec = float(env_int("BACKFILL_INTERVAL_SEC", 300))
        self.min_recheck_sec = float(env_int("BACKFILL_MIN_RECHECK_SEC", 3600))
        # Guard on inbox as well so at most one backfill file is outstanding.
        self.guard_inbox = env_flag("BACKFILL_GUARD_INBOX", True)

    @property
    def state_path(self) -> Path:
        return self.ingest_root / STATE_DIRNAME / STATE_FILENAME


# --------------------------------------------------------------------------- #
# OpenSearch discovery / frontier
# --------------------------------------------------------------------------- #
def discover_chats(client: OpenSearchHttp) -> list[dict[str, str]]:
    """List chats as aliases ``{chat_id}_{slug}`` from OpenSearch."""
    raw = client.request("GET", "/_cat/aliases?format=json&h=alias,index", ignore_status=(404,))
    chats: dict[str, dict[str, str]] = {}
    if not isinstance(raw, list):
        return []
    for row in raw:
        if not isinstance(row, dict):
            continue
        alias = str(row.get("alias") or "")
        if not alias or "_" not in alias or alias.startswith("."):
            continue
        if alias in ("kb_catalog", "backfill_state"):
            continue
        chat_id, slug = alias.split("_", 1)
        if not chat_id or not slug:
            continue
        chats.setdefault(alias, {"alias": alias, "chat_id": chat_id, "slug": slug})
    return sorted(chats.values(), key=lambda c: c["alias"])


def chat_frontier(client: OpenSearchHttp, alias: str) -> dict[str, Any]:
    """Return {'max_message_id': int|None, 'date_max': str|None} for a chat."""
    body = {
        "size": 0,
        "track_total_hits": False,
        "aggs": {
            "max_id": {"max": {"field": "message_id"}},
            "max_ts": {"max": {"field": "timestamp"}},
        },
    }
    raw = client.request("POST", f"/{alias}/_search", body, ignore_status=(404,))
    if not isinstance(raw, dict):
        return {"max_message_id": None, "date_max": None}
    aggs = raw.get("aggregations", {}) if isinstance(raw.get("aggregations"), dict) else {}
    max_id_val = aggs.get("max_id", {}).get("value")
    max_id = int(max_id_val) if max_id_val is not None else None
    date_max = aggs.get("max_ts", {}).get("value_as_string")
    return {"max_message_id": max_id, "date_max": date_max}


# --------------------------------------------------------------------------- #
# State (per-chat last_checked_at) — a JSON file on the shared data volume
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Chat selection: most behind (smallest date_max) with recheck backoff
# --------------------------------------------------------------------------- #
def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").split(".")[0]).replace(tzinfo=None)
    except ValueError:
        return None


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def select_chat(
    chats: list[dict[str, Any]],
    now: datetime,
    min_recheck_sec: float,
) -> dict[str, Any] | None:
    """Pick the most behind chat that has a frontier and is not in backoff."""
    now = _naive_utc(now)
    eligible: list[dict[str, Any]] = []
    for chat in chats:
        if chat.get("max_message_id") is None:
            continue  # empty alias / nothing indexed yet
        last_checked = _parse_iso(chat.get("last_checked_at"))
        if last_checked is not None:
            if (now - last_checked).total_seconds() < min_recheck_sec:
                continue
        eligible.append(chat)
    if not eligible:
        return None
    # Smallest date_max first (most behind); missing date_max sorts first.
    return min(eligible, key=lambda c: (c.get("date_max") or ""))


# --------------------------------------------------------------------------- #
# Inbox output (Telegram Desktop export shape)
# --------------------------------------------------------------------------- #
def build_export(chat_meta: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": chat_meta.get("name") or "",
        "type": chat_meta.get("type") or "unknown",
        "id": chat_meta["id"],
        "messages": messages,
    }


def write_inbox(root: Path, chat_id: str, slug: str, export: dict[str, Any], stamp: str) -> Path:
    dest_dir = root / "inbox" / f"{chat_id}_{slug}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"backfill_{stamp}.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    return dest


# --------------------------------------------------------------------------- #
# Single-flight guard
# --------------------------------------------------------------------------- #
def _zone_has_json(root: Path, zone: str) -> bool:
    zone_dir = root / zone
    if not zone_dir.is_dir():
        return False
    for path in zone_dir.rglob("*.json"):
        if path.is_file() and not path.name.startswith("."):
            return True
    return False


def pipeline_busy(root: Path, guard_inbox: bool) -> bool:
    if _zone_has_json(root, "processing"):
        return True
    if guard_inbox and _zone_has_json(root, "inbox"):
        return True
    return False


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
def run_once(
    client: OpenSearchHttp,
    source: TelegramSource,
    config: BackfillConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _naive_utc(now or datetime.now(timezone.utc))
    root = config.ingest_root

    if pipeline_busy(root, config.guard_inbox):
        LOGGER.info("pipeline busy (processing/inbox not empty) — skipping cycle")
        return {"status": "busy"}

    state = load_state(config.state_path)
    chats = discover_chats(client)
    if not chats:
        LOGGER.info("no chats discovered in OpenSearch yet")
        return {"status": "no_chats"}

    for chat in chats:
        chat.update(chat_frontier(client, chat["alias"]))
        chat["last_checked_at"] = state.get(chat["alias"], {}).get("last_checked_at")

    chat = select_chat(chats, now, config.min_recheck_sec)
    if chat is None:
        LOGGER.info("no eligible chat (all in recheck backoff or empty)")
        return {"status": "idle"}

    alias = chat["alias"]
    min_id = int(chat["max_message_id"])
    date_max = _parse_iso(chat.get("date_max")) or now
    until_date = date_max + timedelta(days=config.days_per_run)
    LOGGER.info(
        "selected %s: min_id=%s date_max=%s window_until=%s",
        alias,
        min_id,
        chat.get("date_max"),
        until_date.strftime(DATE_FMT),
    )

    chat_meta, messages = source.fetch(chat["chat_id"], chat["slug"], min_id, until_date)

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    result: dict[str, Any] = {"status": "ok", "alias": alias, "fetched": len(messages)}
    if messages:
        chat_meta.setdefault("id", chat["chat_id"])
        export = build_export(chat_meta, messages)
        dest = write_inbox(root, chat["chat_id"], chat["slug"], export, stamp)
        result["written"] = str(dest)
        LOGGER.info("wrote %s messages -> %s", len(messages), dest)
    else:
        LOGGER.info("no new messages for %s within window", alias)

    entry = state.setdefault(alias, {})
    entry["last_checked_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    entry["last_max_id"] = min_id
    entry["last_date_max"] = chat.get("date_max")
    entry["last_fetched"] = len(messages)
    save_state(config.state_path, state)
    return result


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
def _make_source() -> TelegramSource:
    from telegram_source import TelethonSource

    return TelethonSource.from_env()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    os.environ.setdefault("OPENSEARCH_URL", "https://opensearch:9200")
    config = BackfillConfig()

    # Ensure the shared tree exists (ingest also does this).
    for zone in INGEST_ZONES:
        (config.ingest_root / zone).mkdir(parents=True, exist_ok=True)

    # Cross-process single-instance lock on the shared volume.
    lock_path = config.ingest_root / STATE_DIRNAME / "backfill.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        LOGGER.error("another backfill worker holds %s — exiting", lock_path)
        return 1

    client = OpenSearchHttp()
    source = _make_source()
    LOGGER.info(
        "backfill worker started root=%s days_per_run=%s interval=%.0fs recheck=%.0fs",
        config.ingest_root,
        config.days_per_run,
        config.interval_sec,
        config.min_recheck_sec,
    )
    while True:
        try:
            if client.ping():
                run_once(client, source, config)
            else:
                LOGGER.warning("OpenSearch not reachable, retrying")
        except Exception:
            LOGGER.exception("backfill cycle error")
        time.sleep(config.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
