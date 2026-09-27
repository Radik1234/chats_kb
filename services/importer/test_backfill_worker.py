#!/usr/bin/env python3
"""Unit tests for the backfill worker (no network, no Telegram, no OpenSearch).

Runnable directly (``python test_backfill_worker.py``) or via pytest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backfill_worker as bw
from telegram_source import message_to_export


# --------------------------------------------------------------------------- #
# message_to_export (Telethon -> Telegram Desktop export dict)
# --------------------------------------------------------------------------- #
def test_message_to_export_basic():
    msg = SimpleNamespace(
        id=42,
        date=datetime(2026, 1, 5, 12, 30, 0, tzinfo=timezone.utc),
        message="hello tempdb",
        entities=None,
        reply_to=None,
        action=None,
        sender_id=777,
        sender=SimpleNamespace(first_name="Ivan", last_name="Petrov", username="ivan"),
        fwd_from=None,
        photo=None,
    )
    doc = message_to_export(msg)
    assert doc["id"] == 42
    assert doc["type"] == "message"
    assert doc["date"] == "2026-01-05T12:30:00"
    assert doc["from"] == "Ivan Petrov"
    assert doc["from_id"] == "user777"
    assert doc["text"] == "hello tempdb"
    assert "reply_to_message_id" not in doc


def test_message_to_export_service_and_reply():
    msg = SimpleNamespace(
        id=43,
        date=datetime(2026, 1, 5, 13, 0, 0, tzinfo=timezone.utc),
        message="",
        action=SimpleNamespace(),  # service message
        reply_to=SimpleNamespace(reply_to_msg_id=42),
        sender_id=None,
        sender=None,
    )
    doc = message_to_export(msg)
    assert doc["type"] == "service"
    assert doc["reply_to_message_id"] == 42
    assert "from_id" not in doc


def test_message_to_export_empty_skipped():
    assert message_to_export(SimpleNamespace(id=None, date=None)) is None


# --------------------------------------------------------------------------- #
# discovery / selection / guard
# --------------------------------------------------------------------------- #
class FakeClient:
    def __init__(self, aliases, frontiers):
        self._aliases = aliases  # list of alias strings
        self._frontiers = frontiers  # {alias: {"max_message_id":.., "date_max":..}}

    def ping(self):
        return True

    def request(self, method, path, body=None, ignore_status=()):
        if path.startswith("/_cat/aliases"):
            rows = [{"alias": a, "index": f"{a}_2026-01"} for a in self._aliases]
            # include some noise aliases that must be filtered out
            rows += [{"alias": "kb_catalog", "index": "kb_catalog"},
                     {"alias": ".security", "index": ".security"}]
            return rows
        if method == "POST" and path.endswith("/_search"):
            alias = path.strip("/").split("/")[0]
            fr = self._frontiers.get(alias, {"max_message_id": None, "date_max": None})
            aggs = {}
            if fr["max_message_id"] is not None:
                aggs["max_id"] = {"value": float(fr["max_message_id"])}
            else:
                aggs["max_id"] = {"value": None}
            aggs["max_ts"] = {"value_as_string": fr["date_max"]}
            return {"aggregations": aggs}
        return {}


def test_discover_chats_filters_noise():
    client = FakeClient(["111_demo", "222_other"], {})
    chats = bw.discover_chats(client)
    aliases = [c["alias"] for c in chats]
    assert aliases == ["111_demo", "222_other"]
    assert chats[0]["chat_id"] == "111" and chats[0]["slug"] == "demo"


def test_select_chat_prefers_most_behind():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    chats = [
        {"alias": "a", "max_message_id": 5, "date_max": "2026-01-20T00:00:00", "last_checked_at": None},
        {"alias": "b", "max_message_id": 5, "date_max": "2025-06-01T00:00:00", "last_checked_at": None},
        {"alias": "c", "max_message_id": None, "date_max": None, "last_checked_at": None},
    ]
    chosen = bw.select_chat(chats, now, min_recheck_sec=3600)
    assert chosen["alias"] == "b"  # oldest date_max, and not the empty one


def test_select_chat_backoff_skips_recent():
    now = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    chats = [
        {"alias": "b", "max_message_id": 5, "date_max": "2025-06-01T00:00:00",
         "last_checked_at": "2026-02-01T11:59:00"},  # 60s ago, within backoff
        {"alias": "a", "max_message_id": 5, "date_max": "2026-01-20T00:00:00",
         "last_checked_at": None},
    ]
    chosen = bw.select_chat(chats, now, min_recheck_sec=3600)
    assert chosen["alias"] == "a"  # b is in backoff despite being more behind


def test_pipeline_busy_detects_zones():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        assert bw.pipeline_busy(root, guard_inbox=True) is False
        (root / "processing" / "c").mkdir()
        (root / "processing" / "c" / "x.json").write_text("{}")
        assert bw.pipeline_busy(root, guard_inbox=True) is True


# --------------------------------------------------------------------------- #
# run_once end-to-end (fakes for OpenSearch + Telegram)
# --------------------------------------------------------------------------- #
class FakeSource:
    def __init__(self, messages):
        self._messages = messages
        self.calls = []

    def fetch(self, chat_id, slug, min_id, until_date):
        self.calls.append((chat_id, slug, min_id, until_date))
        meta = {"id": chat_id, "name": "Demo", "type": "public_supergroup"}
        # simulate min_id filter
        msgs = [m for m in self._messages if m["id"] > min_id]
        return meta, msgs


def _config(root: Path) -> bw.BackfillConfig:
    os.environ["INGEST_ROOT"] = str(root)
    os.environ["BACKFILL_DAYS_PER_RUN"] = "7"
    os.environ["BACKFILL_MIN_RECHECK_SEC"] = "3600"
    os.environ["BACKFILL_GUARD_INBOX"] = "true"
    return bw.BackfillConfig()


def test_run_once_writes_inbox_and_state():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        client = FakeClient(
            ["111_demo"],
            {"111_demo": {"max_message_id": 100, "date_max": "2026-01-01T00:00:00"}},
        )
        source = FakeSource([{"id": 101, "type": "message", "date": "2026-01-02T10:00:00", "text": "new"}])
        config = _config(root)
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)

        result = bw.run_once(client, source, config, now=now)
        assert result["status"] == "ok"
        assert result["fetched"] == 1
        # inbox file exists with export shape
        files = list((root / "inbox" / "111_demo").glob("*.json"))
        assert len(files) == 1
        export = json.loads(files[0].read_text())
        assert export["id"] == "111" and export["messages"][0]["id"] == 101
        # source called with correct frontier
        assert source.calls[0][2] == 100
        # state updated
        state = json.loads(config.state_path.read_text())
        assert state["111_demo"]["last_max_id"] == 100
        assert state["111_demo"]["last_fetched"] == 1


def test_run_once_skips_when_busy():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        (root / "processing" / "c").mkdir(parents=True)
        (root / "processing" / "c" / "x.json").write_text("{}")
        client = FakeClient(["111_demo"], {"111_demo": {"max_message_id": 100, "date_max": "2026-01-01T00:00:00"}})
        source = FakeSource([{"id": 101, "type": "message", "date": "2026-01-02T10:00:00", "text": "x"}])
        config = _config(root)
        result = bw.run_once(client, source, config, now=datetime(2026, 1, 10, tzinfo=timezone.utc))
        assert result["status"] == "busy"
        assert source.calls == []  # never fetched


def test_run_once_no_messages_updates_state_only():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        client = FakeClient(["111_demo"], {"111_demo": {"max_message_id": 100, "date_max": "2026-01-01T00:00:00"}})
        source = FakeSource([])  # nothing new
        config = _config(root)
        result = bw.run_once(client, source, config, now=datetime(2026, 1, 10, tzinfo=timezone.utc))
        assert result["status"] == "ok" and result["fetched"] == 0
        assert list((root / "inbox").rglob("*.json")) == []
        state = json.loads(config.state_path.read_text())
        assert state["111_demo"]["last_fetched"] == 0


class RaisingSource:
    """Fake source that raises for specific chat ids (inaccessible chats)."""

    def __init__(self, fail_chat_ids, messages=None):
        self.fail = set(fail_chat_ids)
        self.messages = messages or []
        self.calls = []

    def fetch(self, chat_id, slug, min_id, until_date):
        self.calls.append(chat_id)
        if chat_id in self.fail:
            raise RuntimeError(f"ChannelPrivateError: {chat_id} not accessible")
        meta = {"id": chat_id, "name": "Good", "type": "public_supergroup"}
        return meta, [m for m in self.messages if m["id"] > min_id]


def test_select_chat_error_backoff_skips_future_retry():
    now = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    chats = [
        {"alias": "a", "max_message_id": 5, "date_max": "2025-06-01T00:00:00",
         "last_checked_at": "2026-02-01T11:00:00", "retry_after": "2026-02-01T18:00:00", "error_count": 2},
        {"alias": "b", "max_message_id": 5, "date_max": "2026-01-20T00:00:00", "last_checked_at": None},
    ]
    chosen = bw.select_chat(chats, now, min_recheck_sec=3600)
    assert chosen["alias"] == "b"  # a is in error backoff despite being most behind


def test_run_once_inaccessible_chat_backs_off_not_retried():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        client = FakeClient(["111_demo"], {"111_demo": {"max_message_id": 100, "date_max": "2026-01-01T00:00:00"}})
        source = RaisingSource(["111"])
        config = _config(root)
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)

        r1 = bw.run_once(client, source, config, now=now)
        assert r1["status"] == "error" and r1["error_count"] == 1
        state = json.loads(config.state_path.read_text())
        assert state["111_demo"]["error_count"] == 1
        assert "retry_after" in state["111_demo"]
        assert list((root / "inbox").rglob("*.json")) == []  # nothing written

        # Same cycle time: chat is in error backoff -> not retried, no new fetch.
        r2 = bw.run_once(client, source, config, now=now)
        assert r2["status"] == "idle"
        assert source.calls == ["111"]  # fetched exactly once, no infinite retry


def test_run_once_inaccessible_chat_does_not_starve_others():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        client = FakeClient(
            ["111_bad", "222_good"],
            {
                "111_bad": {"max_message_id": 100, "date_max": "2025-01-01T00:00:00"},   # most behind
                "222_good": {"max_message_id": 50, "date_max": "2026-01-01T00:00:00"},
            },
        )
        source = RaisingSource(["111"], messages=[{"id": 51, "type": "message", "date": "2026-01-02T00:00:00", "text": "x"}])
        config = _config(root)
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)

        r1 = bw.run_once(client, source, config, now=now)
        assert r1["status"] == "error" and r1["alias"] == "111_bad"  # most behind picked first, fails

        r2 = bw.run_once(client, source, config, now=now)
        assert r2["status"] == "ok" and r2["alias"] == "222_good"  # good chat still gets served
        assert list((root / "inbox" / "222_good").glob("*.json"))


def test_run_once_recovers_when_chat_becomes_available():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for z in bw.INGEST_ZONES:
            (root / z).mkdir(parents=True)
        client = FakeClient(["111_demo"], {"111_demo": {"max_message_id": 100, "date_max": "2026-01-01T00:00:00"}})
        config = _config(root)  # BACKFILL_ERROR_BACKOFF_SEC default 900
        config.min_recheck_sec = 0  # isolate the error backoff (retry_after) from idle backoff
        t0 = datetime(2026, 1, 10, tzinfo=timezone.utc)

        # 1) inaccessible -> parked with a retry_after
        r1 = bw.run_once(client, RaisingSource(["111"]), config, now=t0)
        assert r1["status"] == "error"
        state = json.loads(config.state_path.read_text())
        assert "retry_after" in state["111_demo"]

        # 2) still inside the backoff window -> skipped, not retried
        r_mid = bw.run_once(client, RaisingSource(["111"]), config, now=t0 + timedelta(seconds=60))
        assert r_mid["status"] == "idle"

        # 3) after retry_after, and the chat is reachable again -> resumes normally
        good = FakeSource([{"id": 101, "type": "message", "date": "2026-01-02T10:00:00", "text": "back"}])
        r2 = bw.run_once(client, good, config, now=t0 + timedelta(seconds=1000))
        assert r2["status"] == "ok" and r2["fetched"] == 1
        assert good.calls[0][2] == 100  # resumed from the exact message_id frontier
        state = json.loads(config.state_path.read_text())
        assert state["111_demo"]["error_count"] == 0
        assert "retry_after" not in state["111_demo"]
        assert list((root / "inbox" / "111_demo").glob("*.json"))


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
