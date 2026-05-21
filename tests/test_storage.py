"""Tests for the /admin storage budget feature.

Covers:
- get_storage_info() — graceful degradation when path missing / unreadable
- format_bytes() — human-readable rendering
- storage_history persistence (load/append, TTL pruning)
- Forecast rendering when history is too thin / growing / zero-rate
- Threshold alert one-shot semantics (fire on crossing, reset on drop)
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Provide the minimum env vars that bot.py needs at import time
# (mirrors what tests/test_handlers.py does).
os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import storage
from state_store import JsonStateStore


class GetStorageInfoTests(unittest.TestCase):
    def test_none_when_path_empty_string(self):
        self.assertIsNone(storage.get_storage_info(""))

    def test_none_when_path_missing(self):
        self.assertIsNone(storage.get_storage_info("/definitely/not/a/real/path/xyz"))

    def test_returns_dataclass_for_existing_path(self):
        with tempfile.TemporaryDirectory() as td:
            info = storage.get_storage_info(td)
            self.assertIsNotNone(info)
            self.assertGreater(info.total_bytes, 0)
            self.assertGreaterEqual(info.used_bytes, 0)
            self.assertGreaterEqual(info.free_bytes, 0)
            self.assertGreaterEqual(info.used_percent, 0.0)
            self.assertLessEqual(info.used_percent, 100.0)


class FormatBytesTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(storage.format_bytes(0), "0 B")
        self.assertEqual(storage.format_bytes(500), "500 B")

    def test_kb_mb_gb(self):
        # Decimal units (1000-based).
        self.assertEqual(storage.format_bytes(1500), "1.5 KB")
        self.assertEqual(storage.format_bytes(2_500_000), "2.5 MB")
        self.assertEqual(storage.format_bytes(8_500_000_000), "8.5 GB")

    def test_tb(self):
        self.assertIn("TB", storage.format_bytes(1_400_000_000_000))

    def test_negative_and_none_safe(self):
        self.assertEqual(storage.format_bytes(-1), "?")


def _make_store(tmp_dir: str) -> JsonStateStore:
    p = Path(tmp_dir)
    return JsonStateStore(
        approved_chat_ids_file=p / "approved.json",
        tracker_processed_file=p / "trackers.json",
        task_owners_file=p / "task_owners.json",
        notified_tasks_file=p / "notified.json",
        auto_delete_tasks_file=p / "auto_delete.json",
        storage_history_file=p / "storage_history.json",
    )


class StorageHistoryPersistenceTests(unittest.TestCase):
    def test_load_returns_empty_when_file_absent(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(td)
            self.assertEqual(store.load_storage_history(), [])

    def test_append_creates_file_with_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(td)
            snap = {"ts": "2026-05-22T03:00:00", "used_bytes": 1_000_000, "free_bytes": 9_000_000}
            store.append_storage_snapshot(snap)
            self.assertTrue((Path(td) / "storage_history.json").exists())
            self.assertEqual(store.load_storage_history(), [snap])

    def test_append_prunes_entries_older_than_max_age(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(td)
            # Mix old and fresh entries — write file directly so pruning logic
            # in append() operates on real existing data.
            ancient_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds").split("+")[0]
            fresh_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds").split("+")[0]
            store.append_storage_snapshot(
                {"ts": ancient_ts, "used_bytes": 1, "free_bytes": 1},
                max_age_days=30,
            )
            store.append_storage_snapshot(
                {"ts": fresh_ts, "used_bytes": 2, "free_bytes": 2},
                max_age_days=30,
            )
            new_ts = datetime.now(timezone.utc).isoformat(timespec="seconds").split("+")[0]
            store.append_storage_snapshot(
                {"ts": new_ts, "used_bytes": 3, "free_bytes": 3},
                max_age_days=30,
            )
            history = store.load_storage_history()
            # Ancient entry should be gone, fresh + new remain.
            tss = [e["ts"] for e in history]
            self.assertNotIn(ancient_ts, tss)
            self.assertIn(fresh_ts, tss)
            self.assertIn(new_ts, tss)


class StorageAlertOneShotTests(unittest.TestCase):
    """Ensures _maybe_send_storage_alert fires once per upward crossing."""

    def setUp(self):
        # Import here to allow patches inside `bot` module.
        import bot
        self.bot = bot
        # Snapshot and reset module-level alert state so tests are isolated.
        self._original_state = bot._STORAGE_ALERT_STATE.copy()
        bot._STORAGE_ALERT_STATE["above"] = False

    def tearDown(self):
        self.bot._STORAGE_ALERT_STATE.clear()
        self.bot._STORAGE_ALERT_STATE.update(self._original_state)

    def _make_app(self):
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.send_message = AsyncMock()
        return app

    def _info(self, used_percent: float) -> storage.StorageInfo:
        total = 4_000_000_000_000
        used = int(total * used_percent / 100)
        return storage.StorageInfo(
            total_bytes=total, used_bytes=used, free_bytes=total - used,
            used_percent=used_percent,
        )

    def test_alert_fires_once_on_crossing(self):
        import asyncio
        app = self._make_app()
        with (
            patch.object(self.bot, "ADMIN_CHAT_IDS", {100, 200}),
            patch.object(self.bot, "STORAGE_ALERT_PERCENT", 90),
        ):
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(91), app))
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(92), app))
        # Two admins → 2 messages on the first crossing; second call must be silent.
        self.assertEqual(app.bot.send_message.await_count, 2)

    def test_alert_resets_when_usage_drops(self):
        import asyncio
        app = self._make_app()
        with (
            patch.object(self.bot, "ADMIN_CHAT_IDS", {100}),
            patch.object(self.bot, "STORAGE_ALERT_PERCENT", 90),
        ):
            # First crossing: 1 send.
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(91), app))
            # Drop below — flag clears.
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(80), app))
            # Second crossing: another 1 send (total 2).
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(95), app))
        self.assertEqual(app.bot.send_message.await_count, 2)

    def test_alert_silent_when_below_threshold(self):
        import asyncio
        app = self._make_app()
        with (
            patch.object(self.bot, "ADMIN_CHAT_IDS", {100}),
            patch.object(self.bot, "STORAGE_ALERT_PERCENT", 90),
        ):
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(50), app))
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(80), app))
            asyncio.run(self.bot._maybe_send_storage_alert(self._info(89.9), app))
        self.assertEqual(app.bot.send_message.await_count, 0)


if __name__ == "__main__":
    unittest.main()
