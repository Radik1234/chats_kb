#!/usr/bin/env python3
"""Telegram (MTProto user session) source for the backfill worker.

Isolated so the worker can be tested without a real account. The pure mapping
``message_to_export`` (Telethon message -> Telegram Desktop export dict) is a
module-level function and is unit-tested; the network parts (client lifecycle,
entity resolution, history iteration) are only exercised with real credentials.

Backfill primitive ``messages.getHistory`` is user-only, so this needs a user
session (api_id/api_hash + StringSession), not a bot token.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger("telegram_source")

DATE_FMT = "%Y-%m-%dT%H:%M:%S"

# Telegram Desktop export "type" per Telethon entity class name.
_ENTITY_TYPE = {
    "Channel": "public_supergroup",
    "Chat": "private_group",
    "User": "personal_chat",
}


def _fmt_date(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    # Export uses naive local-ish timestamps; normalise to naive UTC seconds.
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime(DATE_FMT)


def _flatten_entities_text(text: str, entities: list[Any] | None) -> Any:
    """Keep it simple: ingest's ``flatten_text`` accepts a plain string, and
    ``has_links`` is also derived from the text itself, so a string is enough.
    Media-only messages have empty text."""
    return text or ""


def message_to_export(msg: Any) -> dict[str, Any] | None:
    """Map a Telethon message to a Telegram Desktop export message dict.

    Returns None if the object has no id/date (e.g. MessageEmpty)."""
    msg_id = getattr(msg, "id", None)
    date = _fmt_date(getattr(msg, "date", None))
    if msg_id is None or date is None:
        return None

    is_service = getattr(msg, "action", None) is not None
    doc: dict[str, Any] = {
        "id": int(msg_id),
        "type": "service" if is_service else "message",
        "date": date,
    }

    sender_id = getattr(msg, "sender_id", None)
    if sender_id is not None:
        # Export encodes sender as e.g. "user123456"; ingest only stores it as a
        # keyword, so a stable string is enough.
        doc["from_id"] = f"user{sender_id}"
    sender = getattr(msg, "sender", None)
    name = None
    if sender is not None:
        first = getattr(sender, "first_name", None) or ""
        last = getattr(sender, "last_name", None) or ""
        name = (first + " " + last).strip() or getattr(sender, "title", None) or getattr(sender, "username", None)
    if name:
        doc["from"] = name

    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    doc["text"] = _flatten_entities_text(text, getattr(msg, "entities", None))

    reply_to = getattr(msg, "reply_to", None)
    reply_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to is not None else None
    if reply_id is not None:
        doc["reply_to_message_id"] = int(reply_id)

    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        doc["forwarded_from"] = getattr(fwd, "from_name", None) or "forwarded"

    if getattr(msg, "photo", None) is not None:
        doc["photo"] = "(not included)"

    return doc


class TelethonSource:
    """Fetch new messages via a Telethon user session."""

    def __init__(self, api_id: int, api_hash: str, session: str, flood_sleep_threshold: int = 60) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.flood_sleep_threshold = flood_sleep_threshold

    @classmethod
    def from_env(cls) -> "TelethonSource":
        api_id = os.environ.get("TG_API_ID")
        api_hash = os.environ.get("TG_API_HASH")
        session = os.environ.get("TG_SESSION")
        if not (api_id and api_hash and session):
            raise RuntimeError("TG_API_ID, TG_API_HASH and TG_SESSION must be set")
        return cls(int(api_id), api_hash, session)

    def _client(self):
        # Lazy import so the worker module has no hard telethon dependency
        # (keeps it importable for tests without the package installed).
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(self.session),
            self.api_id,
            self.api_hash,
            flood_sleep_threshold=self.flood_sleep_threshold,
        )
        return client

    def _resolve_entity(self, client: Any, chat_id: str):
        target = int(chat_id)
        for dialog in client.iter_dialogs():
            entity = dialog.entity
            if getattr(entity, "id", None) == target:
                return entity
        # Fall back to a direct lookup (works if the peer is already cached).
        return client.get_entity(target)

    def fetch(
        self,
        chat_id: str,
        slug: str,
        min_id: int,
        until_date: datetime,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        messages: list[dict[str, Any]] = []
        with self._client() as client:
            entity = self._resolve_entity(client, chat_id)
            chat_meta = {
                "id": chat_id,
                "name": getattr(entity, "title", None) or getattr(entity, "username", None) or slug,
                "type": _ENTITY_TYPE.get(type(entity).__name__, "unknown"),
            }
            for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
                mdate = getattr(msg, "date", None)
                if isinstance(mdate, datetime):
                    naive = mdate.astimezone(timezone.utc).replace(tzinfo=None) if mdate.tzinfo else mdate
                    if naive > until_date:
                        break  # reached the end of this run's day-window
                doc = message_to_export(msg)
                if doc is not None:
                    messages.append(doc)
        LOGGER.info("fetched %s messages for %s (min_id=%s)", len(messages), chat_id, min_id)
        return chat_meta, messages
