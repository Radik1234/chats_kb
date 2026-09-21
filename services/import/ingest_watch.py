#!/usr/bin/env python3
"""Watch data/telegram/inbox and ingest Telegram JSON into OpenSearch."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from import_telegram import (
    DEFAULT_CHUNK_INTERVAL_SEC,
    OpenSearchHttp,
    env_int,
    import_file,
    load_dotenv,
)

LOGGER = logging.getLogger("ingest_watch")

ZONES = ("inbox", "processing", "archive", "failed", "logs")


def ingest_root() -> Path:
    return Path(os.environ.get("INGEST_ROOT", "/data/telegram"))


def ensure_tree(root: Path) -> None:
    for zone in ZONES:
        (root / zone).mkdir(parents=True, exist_ok=True)


def archive_name(original: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}__{original}"


def chat_folder(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root / "inbox")
    except ValueError:
        relative = path.relative_to(root / "processing")
    parts = relative.parts
    if len(parts) < 2:
        raise ValueError(f"JSON must be inside inbox/{{chat_id}}_{{slug}}/: {path}")
    return parts[0]


def list_inbox_json(root: Path) -> list[Path]:
    inbox = root / "inbox"
    files: list[Path] = []
    if not inbox.is_dir():
        return files
    for path in inbox.rglob("*.json"):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            chat_folder(path, root)
        except ValueError:
            LOGGER.warning("skip json not in chat folder: %s", path)
            continue
        files.append(path)
    return sorted(files)


def is_stable(path: Path, stable_sec: float) -> bool:
    try:
        first = (path.stat().st_size, path.stat().st_mtime)
        time.sleep(stable_sec)
        if not path.is_file():
            return False
        second = (path.stat().st_size, path.stat().st_mtime)
        return first == second and first[0] > 0
    except OSError:
        return False


def move_to_zone(path: Path, root: Path, zone: str, folder: str, rename: bool) -> Path:
    dest_dir = root / zone / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive_name(path.name) if rename else path.name
    dest = dest_dir / name
    if dest.exists():
        dest = dest_dir / archive_name(path.name)
    shutil.move(str(path), str(dest))
    return dest


def process_file(path: Path, root: Path, client: OpenSearchHttp, interval: float) -> None:
    folder = chat_folder(path, root)
    processing = move_to_zone(path, root, "processing", folder, rename=False)
    LOGGER.info("processing %s", processing)
    try:
        stats = import_file(processing, client=client, chunk_interval_sec=interval)
        archived = move_to_zone(processing, root, "archive", folder, rename=True)
        LOGGER.info(
            "archived %s loaded_months=%s loaded_days=%s skipped_months=%s skipped_days=%s",
            archived,
            len(stats["loaded_months"]),
            len(stats["loaded_days"]),
            len(stats["skipped_months"]),
            len(stats["skipped_days"]),
        )
    except Exception:
        LOGGER.exception("ingest failed for %s", processing)
        if processing.exists():
            failed = move_to_zone(processing, root, "failed", folder, rename=True)
            LOGGER.error("moved to %s", failed)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    os.environ.setdefault("OPENSEARCH_URL", "https://opensearch:9200")
    root = ingest_root()
    ensure_tree(root)
    poll_sec = float(env_int("INGEST_POLL_INTERVAL_SEC", 30))
    stable_sec = float(env_int("INGEST_STABLE_SEC", 5))
    interval = float(env_int("INGEST_CHUNK_INTERVAL_SEC", DEFAULT_CHUNK_INTERVAL_SEC))
    LOGGER.info(
        "watching %s poll=%.0fs stable=%.0fs chunk_interval=%.0fs",
        root,
        poll_sec,
        stable_sec,
        interval,
    )
    client = OpenSearchHttp()
    while True:
        try:
            if not client.ping():
                LOGGER.warning("OpenSearch is not reachable, retrying")
                time.sleep(poll_sec)
                continue
            for path in list_inbox_json(root):
                if not is_stable(path, stable_sec):
                    LOGGER.info("waiting for stable file %s", path)
                    continue
                process_file(path, root, client, interval)
        except Exception:
            LOGGER.exception("watch loop error")
        time.sleep(poll_sec)


if __name__ == "__main__":
    sys.exit(main())
