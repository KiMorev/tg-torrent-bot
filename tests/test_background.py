import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "999")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from bot import (
    _is_auto_delete_candidate,
    _is_tracker_task_candidate,
    _run_background_monitor_cycle,
    _run_background_step,
    _run_auto_delete_finished_once,
    _run_task_notifications_once,
)
from rutracker import RutrackerTopicUnavailable
from state_store import JsonStateStore


def _make_store(tmp_dir: str) -> JsonStateStore:
    d = Path(tmp_dir)
    return JsonStateStore(
        approved_chat_ids_file=d / "approved.json",
        tracker_processed_file=d / "tracker.json",
        task_owners_file=d / "owners.json",
        notified_tasks_file=d / "notified.json",
        auto_delete_tasks_file=d / "auto_delete.json",
        topic_subscriptions_file=d / "subscriptions.json",
        pending_downloads_file=d / "pending_downloads.json",
        series_bulk_jobs_file=d / "series_bulk_jobs.json",
    )


class TrackerCandidateTests(unittest.TestCase):
    def _task(self, *, task_id="t1", status="downloading", task_type="bt") -> dict:
        return {"id": task_id, "type": task_type, "status": status}

    def test_bt_downloading_is_candidate(self) -> None:
        self.assertTrue(_is_tracker_task_candidate(self._task(), set()))

    def test_already_processed_is_skipped(self) -> None:
        self.assertFalse(_is_tracker_task_candidate(self._task(), {"t1"}))

    def test_non_bt_type_is_skipped(self) -> None:
        self.assertFalse(_is_tracker_task_candidate(self._task(task_type="http"), set()))

    def test_finished_status_is_skipped(self) -> None:
        self.assertFalse(_is_tracker_task_candidate(self._task(status="finished"), set()))


class AutoDeleteCandidateTests(unittest.TestCase):
    def test_finished_is_candidate(self) -> None:
        self.assertTrue(_is_auto_delete_candidate({"id": "t1", "status": "finished"}))

    def test_downloading_is_not_candidate(self) -> None:
        self.assertFalse(_is_auto_delete_candidate({"id": "t1", "status": "downloading"}))

    def test_missing_id_is_not_candidate(self) -> None:
        self.assertFalse(_is_auto_delete_candidate({"status": "finished"}))


class NotificationDeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)
        bot.TASK_CARD_MESSAGES.clear()

    def tearDown(self) -> None:
        bot.TASK_CARD_MESSAGES.clear()
        self._tmp.cleanup()

    def test_notification_not_sent_twice_for_same_status(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "TestFile", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(mock_app.bot.send_message.call_count, 1)

    def test_notification_deletes_registered_task_card_for_notified_chat(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "TestFile", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_app.bot.delete_message = AsyncMock()
        bot.TASK_CARD_MESSAGES["tid1"] = {(999, 77), (100, 88)}

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        mock_app.bot.send_message.assert_called_once()
        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=999, message_id=77)
        self.assertEqual(bot.TASK_CARD_MESSAGES["tid1"], {(100, 88)})

    def test_notification_retries_only_failed_recipients(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "TestFile", "size": 0}
        attempts: list[int] = []
        failed_once = False

        async def send_message(*, chat_id, **kwargs):
            nonlocal failed_once
            attempts.append(chat_id)
            if chat_id == 999 and not failed_once:
                failed_once = True
                raise RuntimeError("telegram down")

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=send_message)
        mock_app.bot.delete_message = AsyncMock()

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "NOTIFY_CHAT_IDS_RAW", "100,999"),
        ):
            with self.assertLogs("tg_torrent_drop", level="WARNING"):
                asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(attempts, [100, 999, 999])
        notified = self._store.load_notified_tasks()["tid1"]
        self.assertEqual(notified["status"], "done")
        self.assertEqual(notified["sent"], ["100", "999"])
        self.assertEqual(notified["failures"], {})

    def test_notification_stops_retrying_after_failure_limit(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "TestFile", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
            patch.object(bot, "MAX_TASK_NOTIFICATION_FAILURES", 2),
        ):
            with self.assertLogs("tg_torrent_drop", level="WARNING"):
                asyncio.run(_run_task_notifications_once(mock_app))
            with self.assertLogs("tg_torrent_drop", level="WARNING"):
                asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(mock_app.bot.send_message.await_count, 2)
        notified = self._store.load_notified_tasks()["tid1"]
        self.assertEqual(notified["failures"], {"999": 2})

    def test_seeding_status_starts_plex_polling_once(self) -> None:
        task = {"id": "tid1", "status": "seeding", "type": "bt", "title": "TestFile", "size": 0}
        created_coroutines = []

        def fake_create_task(coro):
            created_coroutines.append(coro)
            coro.close()
            return MagicMock()

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"seeding"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "_plex_poll_is_done", return_value=False),
            patch.object(bot, "_get_task_meta", return_value={"kind": "movie", "title": "TestFile"}),
            patch.object(bot, "_plex_poll_after_finish", AsyncMock()) as poll,
            patch.object(bot.asyncio, "create_task", side_effect=fake_create_task),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        poll.assert_called_once()
        self.assertEqual(len(created_coroutines), 1)


class NotificationSkipLoggingTests(unittest.TestCase):
    """Regression: every skip branch in _run_task_notifications_once must
    produce a log line so a missing push notification can be diagnosed from
    docker logs without code instrumentation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)
        bot.TASK_CARD_MESSAGES.clear()

    def tearDown(self) -> None:
        bot.TASK_CARD_MESSAGES.clear()
        self._tmp.cleanup()

    def test_skip_legacy_done_logs_reason(self) -> None:
        # Pre-seed notified_tasks with the legacy plain-string format.
        self._store.save_notified_tasks({"tid1": "done"})
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            with self.assertLogs("tg_torrent_drop", level="INFO") as captured:
                asyncio.run(_run_task_notifications_once(mock_app))
        joined = "\n".join(captured.output)
        self.assertIn("legacy_done", joined)
        self.assertIn("tid1", joined)
        mock_app.bot.send_message.assert_not_awaited()

    def test_skip_no_recipients_logs_reason(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", False),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
        ):
            with self.assertLogs("tg_torrent_drop", level="INFO") as captured:
                asyncio.run(_run_task_notifications_once(mock_app))
        joined = "\n".join(captured.output)
        self.assertIn("no recipients", joined)
        self.assertIn("tid1", joined)
        # Diagnostic context should include why recipients were empty
        self.assertIn("external_enabled=False", joined)
        mock_app.bot.send_message.assert_not_awaited()

    def test_skip_failures_cap_logs_reason(self) -> None:
        # Pre-seed notified_tasks with the failure cap already hit.
        self._store.save_notified_tasks({
            "tid1": {"status": "done", "sent": [], "failures": {"999": 3}},
        })
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
            patch.object(bot, "MAX_TASK_NOTIFICATION_FAILURES", 3),
        ):
            with self.assertLogs("tg_torrent_drop", level="INFO") as captured:
                asyncio.run(_run_task_notifications_once(mock_app))
        joined = "\n".join(captured.output)
        self.assertIn("failures cap", joined)
        self.assertIn("tid1", joined)
        self.assertIn("3/3", joined)
        mock_app.bot.send_message.assert_not_awaited()


class NotificationSelfHealingTests(unittest.TestCase):
    """When task_owners.json lost the record but TASK_CARD_MESSAGES still
    holds the chat that's actively viewing the task, we recover the recipient
    so the push gets delivered anyway. Filtering through ALLOWED_CHAT_IDS
    keeps unauthorised chat_ids out."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)
        bot.TASK_CARD_MESSAGES.clear()

    def tearDown(self) -> None:
        bot.TASK_CARD_MESSAGES.clear()
        self._tmp.cleanup()

    def test_recipients_recovered_from_task_card_registry(self) -> None:
        # No owner in task_owners.json, no external/explicit — primary path
        # would return empty. But the user has an active task-card.
        bot.TASK_CARD_MESSAGES["tid1"] = {(999, 42)}
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_app.bot.delete_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", False),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            with self.assertLogs("tg_torrent_drop", level="INFO") as captured:
                asyncio.run(_run_task_notifications_once(mock_app))

        # Push delivered to the recovered chat_id
        mock_app.bot.send_message.assert_awaited_once()
        self.assertEqual(mock_app.bot.send_message.await_args.kwargs.get("chat_id"), 999)
        # Recovery is logged for observability
        joined = "\n".join(captured.output)
        self.assertIn("recovered from task-card registry", joined)
        self.assertIn("tid1", joined)

    def test_recovered_recipients_filtered_by_allowed_chat_ids(self) -> None:
        # Card registered with a chat_id NOT in ALLOWED_CHAT_IDS — safety filter
        # must drop it. No push should go out.
        bot.TASK_CARD_MESSAGES["tid1"] = {(666, 42)}  # 666 is not allowed
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", False),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),  # only 999 allowed
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
        mock_app.bot.send_message.assert_not_awaited()

    def test_sticky_owner_via_register_task_card_message(self) -> None:
        # Calling _register_task_card_message should also write to task_owners.
        with patch.object(bot, "state_store", self._store):
            bot._register_task_card_message(chat_id=999, message_id=42, task_id="tid1")
        owners = self._store.load_task_owners()
        self.assertEqual(owners.get("tid1"), 999)
        # Also stored in TASK_CARD_MESSAGES.
        self.assertIn(("tid1"), bot.TASK_CARD_MESSAGES)
        self.assertIn((999, 42), bot.TASK_CARD_MESSAGES["tid1"])


class TaskNotificationDeliveryTests(unittest.TestCase):
    """Regression: transient Telegram errors must NOT count against the
    per-chat failure threshold. Only permanent errors (bot blocked, chat
    not found, programming bugs) increment the counter, so a temporary
    network blip / rate-limit / 5xx doesn't permanently drop a user from
    the recipient list."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)
        bot.TASK_CARD_MESSAGES.clear()

    def tearDown(self) -> None:
        bot.TASK_CARD_MESSAGES.clear()
        self._tmp.cleanup()

    def _make_task(self):
        return {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}

    def _run_with_error(self, exc: Exception, *, cycles: int = 3) -> None:
        """Run the notification cycle ``cycles`` times, each time send_message raises ``exc``."""
        task = self._make_task()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=exc)
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            for _ in range(cycles):
                asyncio.run(_run_task_notifications_once(mock_app))
        return mock_app

    def test_retry_after_does_not_increment_failures(self) -> None:
        from telegram.error import RetryAfter
        # retry_after=0 to keep the test fast — our handler still calls asyncio.sleep(min(0, 30)).
        self._run_with_error(RetryAfter(retry_after=0), cycles=5)
        notified = self._store.load_notified_tasks().get("tid1")
        # Either no entry at all, or entry with empty/zero failures — both are acceptable.
        if notified is not None and isinstance(notified, dict):
            self.assertEqual(notified.get("failures") or {}, {})

    def test_timed_out_does_not_increment_failures(self) -> None:
        from telegram.error import TimedOut
        self._run_with_error(TimedOut("read timed out"), cycles=5)
        notified = self._store.load_notified_tasks().get("tid1")
        if notified is not None and isinstance(notified, dict):
            self.assertEqual(notified.get("failures") or {}, {})

    def test_network_error_does_not_increment_failures(self) -> None:
        from telegram.error import NetworkError
        self._run_with_error(NetworkError("connection reset"), cycles=5)
        notified = self._store.load_notified_tasks().get("tid1")
        if notified is not None and isinstance(notified, dict):
            self.assertEqual(notified.get("failures") or {}, {})

    def test_forbidden_increments_failures(self) -> None:
        from telegram.error import Forbidden
        self._run_with_error(Forbidden("bot was blocked by the user"), cycles=3)
        notified = self._store.load_notified_tasks()["tid1"]
        # Capped at MAX_TASK_NOTIFICATION_FAILURES (default 3) — 3 cycles, 3 failures.
        self.assertEqual(notified["failures"], {"999": 3})

    def test_bad_request_chat_not_found_increments_failures(self) -> None:
        from telegram.error import BadRequest
        self._run_with_error(BadRequest("chat not found"), cycles=2)
        notified = self._store.load_notified_tasks()["tid1"]
        self.assertEqual(notified["failures"], {"999": 2})

    def test_unknown_exception_treated_as_permanent(self) -> None:
        # ValueError isn't a Telegram-specific class — should be treated as permanent
        # so we don't busy-retry on our own programming bugs.
        self._run_with_error(ValueError("kaboom"), cycles=2)
        notified = self._store.load_notified_tasks()["tid1"]
        self.assertEqual(notified["failures"], {"999": 2})

    def test_permanent_then_recovery_sends_notification(self) -> None:
        """Failures < limit and then a successful send → notification delivered, counter cleared."""
        from telegram.error import Forbidden
        task = self._make_task()
        mock_app = MagicMock()
        # First two cycles fail with Forbidden (permanent), third succeeds.
        mock_app.bot.send_message = AsyncMock(side_effect=[
            Forbidden("blocked"), Forbidden("blocked"), None,
        ])
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(mock_app.bot.send_message.await_count, 3)
        notified = self._store.load_notified_tasks()["tid1"]
        # After success, failures should be cleared and the user marked as sent.
        self.assertEqual(notified.get("failures") or {}, {})
        self.assertIn("999", notified.get("sent", []))

    def test_transient_failure_does_not_persist_state(self) -> None:
        """If all sends fail with transient errors, no state changes are persisted —
        next cycle starts fresh and can retry without the user 'used up' attempts."""
        from telegram.error import TimedOut
        self._run_with_error(TimedOut("read timed out"), cycles=3)
        notified = self._store.load_notified_tasks()
        # Either the file has no entry for tid1, or it has one but with no failures recorded.
        entry = notified.get("tid1")
        if entry is None:
            return
        if isinstance(entry, dict):
            self.assertEqual(entry.get("failures") or {}, {})
            self.assertEqual(entry.get("sent") or [], [])

    def test_state_saved_per_task_not_just_at_end(self) -> None:
        """Two tasks in the same cycle — state must be written after each, so a
        crash between them doesn't lose the first task's notification."""
        from telegram.error import Forbidden
        task1 = {"id": "tid1", "status": "finished", "type": "bt", "title": "A", "size": 0}
        task2 = {"id": "tid2", "status": "finished", "type": "bt", "title": "B", "size": 0}
        mock_app = MagicMock()
        # Task 1 fails permanently (so state should be written for it), task 2 succeeds.
        mock_app.bot.send_message = AsyncMock(side_effect=[Forbidden("blocked"), None])
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task1, task2]

        save_calls = []
        original_save = self._store.save_notified_tasks

        def spy_save(payload):
            save_calls.append(dict(payload))
            original_save(payload)

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(self._store, "save_notified_tasks", side_effect=spy_save),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        # Both tasks resulted in task_changed → save was called twice (per-task)
        # plus the final no-op save at the end of the cycle = at least 2 calls.
        self.assertGreaterEqual(len(save_calls), 2,
            f"expected ≥2 saves (one per changed task), got {len(save_calls)}")


class PendingDownloadsLoopTests(unittest.TestCase):
    """Pending download queue: success → entry removed + push, failure → attempts++,
    TTL expiry → entry dropped + 'gave up' push, disabled → no-op."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_entry(self, *, attempts: int = 0, hours_ago: float = 0.0) -> str:
        from datetime import datetime, timedelta
        added_at = (datetime.now(bot.DISPLAY_TIMEZONE) - timedelta(hours=hours_ago)).isoformat()
        entry_id = "test-entry-1"
        self._store.save_pending_downloads({
            entry_id: {
                "chat_id": 100,
                "added_at": added_at,
                "title": "Test Movie",
                "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                "torrent_url": "http://jackett:9117/dl/rutracker/?path=Q",
                "magnet_url": None,
                "tracker": "rutracker",
                "source": "jackett",
                "subscribe": False,
                "attempts": attempts,
                "last_attempt_at": None,
                "last_error": "",
            }
        })
        return entry_id

    def test_disabled_loop_is_noop(self) -> None:
        self._seed_entry()
        mock_app = MagicMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", False),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))
        # Entry untouched.
        loaded = self._store.load_pending_downloads()
        self.assertEqual(len(loaded), 1)

    def test_success_removes_entry_and_notifies(self) -> None:
        entry_id = self._seed_entry()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("task1", "torrent-файл"))),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))
        loaded = self._store.load_pending_downloads()
        self.assertNotIn(entry_id, loaded)
        mock_app.bot.send_message.assert_awaited_once()
        sent_text = mock_app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("стартовала", sent_text)
        self.assertIn("Test Movie", sent_text)

    def test_success_without_task_id_warns_and_does_not_track(self) -> None:
        entry_id = self._seed_entry()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        remember_owner = MagicMock()
        remember_meta = MagicMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("", "magnet"))),
            patch.object(bot, "_remember_task_owner", remember_owner),
            patch.object(bot, "_remember_task_meta", remember_meta),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))

        loaded = self._store.load_pending_downloads()
        self.assertNotIn(entry_id, loaded)
        mock_app.bot.send_message.assert_awaited_once()
        sent_text = mock_app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("ID пока не появился", sent_text)
        self.assertNotIn("Слежу за прогрессом", sent_text)
        remember_owner.assert_not_called()
        remember_meta.assert_not_called()

    def test_series_bulk_success_updates_job_and_removes_pending_marker(self) -> None:
        from datetime import datetime
        self._store.save_pending_downloads({
            "bulk-pending-1": {
                "chat_id": 100,
                "added_at": datetime.now(bot.DISPLAY_TIMEZONE).isoformat(),
                "title": "Клиника / Scrubs / Сезон: 1",
                "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                "torrent_url": "http://jackett/dl/1",
                "magnet_url": None,
                "tracker": "rutracker",
                "source": "jackett",
                "subscribe": False,
                "attempts": 0,
                "last_attempt_at": None,
                "last_error": "❌ Jackett HTTP 404",
                "series_bulk": {"job_id": "bulk_test", "season": 1},
            }
        })
        self._store.save_series_bulk_jobs({
            "bulk_test": {
                "id": "bulk_test",
                "status": "batch_completed_with_pending",
                "seasons": {
                    "1": {
                        "season": 1,
                        "runtime_status": "pending_retry",
                        "pending_entry_id": "bulk-pending-1",
                    }
                },
            }
        })
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("task1", "torrent-файл"))),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))

        self.assertEqual(self._store.load_pending_downloads(), {})
        job = self._store.load_series_bulk_jobs()["bulk_test"]
        season = job["seasons"]["1"]
        self.assertEqual(job["status"], "batch_completed")
        self.assertEqual(season["runtime_status"], "downloaded")
        self.assertEqual(season["task_id"], "task1")
        self.assertEqual(season["method"], "torrent-файл")
        self.assertNotIn("pending_entry_id", season)
        self.assertNotIn("error", season)

    def test_series_bulk_ttl_expiry_marks_job_pending_failed(self) -> None:
        from datetime import datetime, timedelta
        self._store.save_pending_downloads({
            "bulk-pending-1": {
                "chat_id": 100,
                "added_at": (datetime.now(bot.DISPLAY_TIMEZONE) - timedelta(hours=25)).isoformat(),
                "title": "Клиника / Scrubs / Сезон: 1",
                "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                "torrent_url": "http://jackett/dl/1",
                "magnet_url": None,
                "tracker": "rutracker",
                "source": "jackett",
                "subscribe": False,
                "attempts": 3,
                "last_attempt_at": None,
                "last_error": "❌ Jackett HTTP 404",
                "series_bulk": {"job_id": "bulk_test", "season": 1},
            }
        })
        self._store.save_series_bulk_jobs({
            "bulk_test": {
                "id": "bulk_test",
                "status": "batch_completed_with_pending",
                "seasons": {
                    "1": {
                        "season": 1,
                        "runtime_status": "pending_retry",
                        "pending_entry_id": "bulk-pending-1",
                    }
                },
            }
        })
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", AsyncMock(side_effect=AssertionError("should not be called"))),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))

        self.assertEqual(self._store.load_pending_downloads(), {})
        job = self._store.load_series_bulk_jobs()["bulk_test"]
        season = job["seasons"]["1"]
        self.assertEqual(job["status"], "batch_failed")
        self.assertEqual(season["runtime_status"], "pending_failed")
        self.assertIn("Jackett HTTP 404", season["error"])
        self.assertNotIn("pending_entry_id", season)

    def test_attempt_pending_download_uses_magnet_from_jackett_redirect(self) -> None:
        from jackett import JackettMagnetRedirect
        entry = {
            "title": "Public Movie 1080p",
            "topic_url": "https://example.org/topic/1",
            "torrent_url": "http://jackett/dl/public/?path=old",
            "magnet_url": None,
            "tracker": "public",
            "source": "jackett",
        }
        mock_jackett = MagicMock()
        mock_jackett.download_torrent = MagicMock(
            side_effect=JackettMagnetRedirect("magnet:?xt=urn:btih:pending")
        )
        mock_ds = MagicMock()
        mock_ds.create_magnet.return_value = "task-pending"

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            task_id, method = asyncio.run(bot._attempt_pending_download(entry))

        self.assertEqual(task_id, "task-pending")
        self.assertEqual(method, "magnet")
        mock_ds.create_magnet.assert_called_once_with("magnet:?xt=urn:btih:pending")

    def test_failure_increments_attempts_and_persists(self) -> None:
        entry_id = self._seed_entry(attempts=1)
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        from jackett import JackettError
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", AsyncMock(side_effect=JackettError("HTTP 404"))),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))
        loaded = self._store.load_pending_downloads()
        # Still queued, attempts bumped to 2.
        self.assertIn(entry_id, loaded)
        self.assertEqual(loaded[entry_id]["attempts"], 2)
        # Error is recorded as the compact form, not the raw exception text.
        self.assertIn("404", loaded[entry_id]["last_error"])
        self.assertIsNotNone(loaded[entry_id]["last_attempt_at"])
        # No success notification.
        mock_app.bot.send_message.assert_not_awaited()

    def test_ttl_expired_drops_entry_and_pushes_failure(self) -> None:
        entry_id = self._seed_entry(attempts=5, hours_ago=25.0)
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", AsyncMock(side_effect=AssertionError("should not be called"))),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))
        loaded = self._store.load_pending_downloads()
        self.assertNotIn(entry_id, loaded)
        mock_app.bot.send_message.assert_awaited_once()
        sent_text = mock_app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("Не удалось скачать", sent_text)

    def test_empty_queue_is_fast_path(self) -> None:
        mock_app = MagicMock()
        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "_attempt_pending_download", AsyncMock(side_effect=AssertionError("should not be called"))),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))
        # No-op.


class MessageFormatBugClassificationTests(unittest.TestCase):
    """Regression: Telegram's BadRequest with text like 'inline keyboard button
    url is invalid' is OUR bug (malformed message), not the chat's fault.
    Counting it as permanent against chat_id would blackhole healthy chats.

    Real example: in May 2026 Telegram stopped accepting `plex://` URLs in
    inline-button URLs; our notification code shipped that URL and ALL push
    notifications for finished tasks died after 3 retries per task — until
    the URL was fixed AND the per-chat failure counter was reset.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)
        bot.TASK_CARD_MESSAGES.clear()

    def tearDown(self) -> None:
        bot.TASK_CARD_MESSAGES.clear()
        self._tmp.cleanup()

    def test_button_url_invalid_does_not_increment_failures(self) -> None:
        """BadRequest about button URL must NOT add to failures[chat_id]."""
        from telegram.error import BadRequest
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=BadRequest(
            "Inline keyboard button url 'plex://' is invalid: unsupported url protocol"
        ))
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            # Multiple cycles — failures must STILL be empty.
            for _ in range(5):
                asyncio.run(_run_task_notifications_once(mock_app))

        notified = self._store.load_notified_tasks().get("tid1")
        # Either no entry persisted (transient = no state change), or entry
        # exists but with no failures recorded.
        if notified is not None and isinstance(notified, dict):
            self.assertEqual(notified.get("failures") or {}, {})

    def test_button_url_invalid_logs_error_level(self) -> None:
        """For our format-bug class we log at ERROR to make the broken code visible."""
        from telegram.error import BadRequest
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=BadRequest(
            "Inline keyboard button url 'plex://preplay/?...' is invalid"
        ))
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            with self.assertLogs("tg_torrent_drop", level="ERROR") as captured:
                asyncio.run(_run_task_notifications_once(mock_app))

        joined = "\n".join(captured.output)
        self.assertIn("message_format_bug", joined)
        self.assertIn("tid1", joined)

    def test_real_chat_not_found_still_counts_as_permanent(self) -> None:
        """Regression guard: actual chat errors must STILL count against the cap
        (we discriminate on text, not on exception type)."""
        from telegram.error import BadRequest
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
        notified = self._store.load_notified_tasks()["tid1"]
        self.assertEqual(notified["failures"], {"999": 1})


class AutoDeleteDelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_task_not_deleted_before_threshold(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "AUTO_DELETE_FINISHED_AFTER_HOURS", 24.0),
            patch.object(bot, "AUTO_DELETE_FINISHED_STATUSES", {"finished"}),
        ):
            asyncio.run(_run_auto_delete_finished_once())

        mock_ds.delete_tasks.assert_not_called()

    def test_task_deleted_after_threshold(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "T", "size": 0}
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        mock_ds.delete_tasks.return_value = None

        past_timestamp = time.time() - (25 * 3600)
        self._store.save_auto_delete_tasks({"tid1": past_timestamp})

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "AUTO_DELETE_FINISHED_AFTER_HOURS", 24.0),
            patch.object(bot, "AUTO_DELETE_FINISHED_STATUSES", {"finished"}),
        ):
            asyncio.run(_run_auto_delete_finished_once())

        mock_ds.delete_tasks.assert_called_once_with(["tid1"])


class BackgroundMonitorResilienceTests(unittest.TestCase):
    def test_cycle_continues_after_unexpected_step_error(self) -> None:
        calls: list[str] = []

        async def fail_tracker() -> None:
            calls.append("trackers")
            raise RuntimeError("boom")

        async def notifications(app) -> None:
            calls.append("notifications")

        async def auto_delete() -> None:
            calls.append("auto_delete")

        async def prune() -> None:
            calls.append("prune")

        with (
            patch.object(bot, "_run_tracker_background_once", fail_tracker),
            patch.object(bot, "_run_task_notifications_once", notifications),
            patch.object(bot, "_run_auto_delete_finished_once", auto_delete),
            patch.object(bot, "_run_prune_stale_state_once", prune),
        ):
            with self.assertLogs("tg_torrent_drop", level="ERROR") as logs:
                asyncio.run(_run_background_monitor_cycle(MagicMock()))

        self.assertEqual(calls, ["trackers", "notifications", "auto_delete", "prune"])
        self.assertIn("Background step failed: public tracker scan", logs.output[0])

    def test_background_step_does_not_swallow_cancellation(self) -> None:
        async def cancel() -> None:
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(_run_background_step("cancel", cancel))


class SubscriptionCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unavailable_rutracker_topic_pauses_subscription_and_notifies(self) -> None:
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 1 из 10",
                "last_episode_end": 1,
                "total_episodes": 10,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.side_effect = RutrackerTopicUnavailable("topic removed")
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        updated = self._store.load_topic_subscriptions()["123"]
        self.assertIn("unavailable_at", updated)
        self.assertEqual(updated["unavailable_reason"], "topic removed")
        mock_app.bot.send_message.assert_awaited_once()

    def test_unavailable_rutracker_topic_is_not_rechecked_after_pause(self) -> None:
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 1 из 10",
                "last_episode_end": 1,
                "total_episodes": 10,
                "unavailable_at": "2026-05-11 22:00",
            }
        })
        mock_rt = MagicMock()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        mock_rt.get_topic_title.assert_not_called()
        mock_app.bot.send_message.assert_not_called()

    def test_jackett_subscription_seen_titles_update_only_after_notification(self) -> None:
        self._store.save_topic_subscriptions({
            "jackett:abc": {
                "type": "jackett",
                "chat_id": 999,
                "query": "series",
                "seen_titles": ["old"],
            }
        })
        result = MagicMock()
        result.title = "new"
        result.size = "1 GB"
        result.seeders = 5
        result.tracker = "idx"
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [result]
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "jackett_client", mock_jackett),
        ):
            with self.assertLogs("tg_torrent_drop", level="WARNING"):
                asyncio.run(bot._check_jackett_subscriptions(mock_app))

        updated = self._store.load_topic_subscriptions()["jackett:abc"]
        self.assertEqual(updated["seen_titles"], ["old"])

    def test_rutracker_subscription_retries_pending_notification_without_duplicate_download(self) -> None:
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 1 из 10",
                "last_episode_end": 1,
                "total_episodes": 10,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 2 из 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "dbid_1"
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None])

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(2, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            with self.assertLogs("tg_torrent_drop", level="WARNING"):
                asyncio.run(bot._check_subscriptions(mock_app))
            asyncio.run(bot._check_subscriptions(mock_app))

        updated = self._store.load_topic_subscriptions()["123"]
        self.assertNotIn("pending_notification", updated)
        self.assertEqual(updated["last_episode_end"], 2)
        self.assertEqual(updated["title"], "Series / 2 из 10")
        mock_ds.create_torrent_file.assert_called_once()
        mock_rt.download_torrent.assert_called_once_with("123")
        self.assertEqual(mock_app.bot.send_message.await_count, 2)

    def test_rutracker_notify_only_does_not_claim_download(self) -> None:
        from subscription_policy import DOWNLOAD_NOTIFY_ONLY, NOTIFY_EACH_UPDATE

        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 1 из 10",
                "last_episode_end": 1,
                "total_episodes": 10,
                "notify_policy": NOTIFY_EACH_UPDATE,
                "download_policy": DOWNLOAD_NOTIFY_ONLY,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 2 из 10"
        mock_ds = MagicMock()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(2, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        text = mock_app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("Авто-загрузка отключена", text)
        self.assertNotIn("Download Station", text)
        mock_rt.download_torrent.assert_not_called()
        mock_ds.create_torrent_file.assert_not_called()
        updated = self._store.load_topic_subscriptions()["123"]
        self.assertEqual(updated["last_episode_end"], 2)

    def test_complete_rutracker_notify_only_removes_after_manual_notification(self) -> None:
        from subscription_policy import DOWNLOAD_NOTIFY_ONLY, NOTIFY_EACH_UPDATE

        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 9 из 10",
                "last_episode_end": 9,
                "total_episodes": 10,
                "notify_policy": NOTIFY_EACH_UPDATE,
                "download_policy": DOWNLOAD_NOTIFY_ONLY,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 10 из 10"
        mock_ds = MagicMock()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(10, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        text = mock_app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("сезон завершён", text)
        self.assertIn("Авто-загрузка отключена", text)
        self.assertNotIn("Download Station", text)
        self.assertNotIn("123", self._store.load_topic_subscriptions())
        mock_rt.download_torrent.assert_not_called()
        mock_ds.create_torrent_file.assert_not_called()

    def test_silent_rutracker_complete_only_updates_progress_without_download(self) -> None:
        from subscription_policy import DOWNLOAD_ONLY_WHEN_COMPLETE, NOTIFY_SILENT

        new_title = "Series S1E1-5 of 12"
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series S1E1-3 of 10",
                "last_episode_end": 3,
                "total_episodes": 10,
                "notify_policy": NOTIFY_SILENT,
                "download_policy": DOWNLOAD_ONLY_WHEN_COMPLETE,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = new_title
        mock_ds = MagicMock()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(5, 12)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        updated = self._store.load_topic_subscriptions()["123"]
        self.assertEqual(updated["last_episode_end"], 5)
        self.assertEqual(updated["total_episodes"], 12)
        self.assertEqual(updated["title"], new_title)
        self.assertNotIn("pending_notification", updated)
        mock_rt.download_torrent.assert_not_called()
        mock_ds.create_torrent_file.assert_not_called()
        mock_app.bot.send_message.assert_not_awaited()

    def test_silent_rutracker_subscription_removed_on_completion(self) -> None:
        from subscription_policy import DOWNLOAD_AUTO_EACH_UPDATE, NOTIFY_SILENT

        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series S1E1-9 of 10",
                "last_episode_end": 9,
                "total_episodes": 10,
                "notify_policy": NOTIFY_SILENT,
                "download_policy": DOWNLOAD_AUTO_EACH_UPDATE,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series S1E1-10 of 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "dbid_1"
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(10, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        self.assertNotIn("123", self._store.load_topic_subscriptions())
        mock_rt.download_torrent.assert_called_once_with("123")
        mock_ds.create_torrent_file.assert_called_once()
        mock_app.bot.send_message.assert_not_awaited()

    def test_rutracker_subscription_empty_task_id_keeps_state_for_retry(self) -> None:
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 1 из 10",
                "last_episode_end": 1,
                "total_episodes": 10,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 2 из 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = ""
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(2, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
            patch.object(bot, "_remember_task_owner") as remember_owner,
            self.assertLogs("tg_torrent_drop", level="WARNING"),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        updated = self._store.load_topic_subscriptions()["123"]
        self.assertEqual(updated["last_episode_end"], 1)
        self.assertNotIn("pending_notification", updated)
        mock_app.bot.send_message.assert_not_awaited()
        remember_owner.assert_not_called()

    def test_final_only_policy_silent_when_not_finished(self) -> None:
        """notify_policy=final_only: bot still downloads the new episodes
        (so Plex gets the files), but suppresses the push and silently
        advances last_episode_end. No notification fires until new_end ≥ total."""
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 3 из 10",
                "last_episode_end": 3,
                "total_episodes": 10,
                "notify_policy": bot.NOTIFY_FINAL_ONLY,
                "download_policy": bot.DOWNLOAD_AUTO_EACH_UPDATE,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 5 из 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "dbid_1"
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(5, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        updated = self._store.load_topic_subscriptions()["123"]
        # State advanced silently
        self.assertEqual(updated["last_episode_end"], 5)
        # No pending notification (we explicitly suppressed)
        self.assertNotIn("pending_notification", updated)
        # File was downloaded — Plex gets episodes regardless of mode
        mock_ds.create_torrent_file.assert_called_once()
        # No push sent to user
        mock_app.bot.send_message.assert_not_awaited()

    def test_final_only_policy_pushes_on_completion(self) -> None:
        """When new_end ≥ total_episodes the push fires even in final_only
        mode — the user finally gets one consolidated «сезон готов» notification."""
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 9 из 10",
                "last_episode_end": 9,
                "total_episodes": 10,
                "notify_policy": bot.NOTIFY_FINAL_ONLY,
                "download_policy": bot.DOWNLOAD_AUTO_EACH_UPDATE,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 10 из 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "dbid_1"
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(10, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        mock_app.bot.send_message.assert_awaited()

    def test_subscription_without_policy_defaults_to_each_update(self) -> None:
        """Missing policy fields default to each-update notifications."""
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 3 из 10",
                "last_episode_end": 3,
                "total_episodes": 10,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 5 из 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "dbid_1"
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(5, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            asyncio.run(bot._check_subscriptions(mock_app))

        # Push fired even though new_end (5) < total (10) — that's each_update behavior.
        mock_app.bot.send_message.assert_awaited()

    def test_complete_rutracker_subscription_removed_only_after_notification_delivered(self) -> None:
        self._store.save_topic_subscriptions({
            "123": {
                "chat_id": 999,
                "title": "Series / 9 из 10",
                "last_episode_end": 9,
                "total_episodes": 10,
            }
        })
        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = "Series / 10 из 10"
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "dbid_1"
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None])

        with (
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_parse_episode_info", return_value=(10, 10)),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            with self.assertLogs("tg_torrent_drop", level="WARNING"):
                asyncio.run(bot._check_subscriptions(mock_app))
            self.assertIn("123", self._store.load_topic_subscriptions())

            asyncio.run(bot._check_subscriptions(mock_app))

        self.assertNotIn("123", self._store.load_topic_subscriptions())
        mock_ds.create_torrent_file.assert_called_once()
        mock_rt.download_torrent.assert_called_once_with("123")
        self.assertEqual(mock_app.bot.send_message.await_count, 2)


class DuplicateDetectionTests(unittest.TestCase):
    """_run_task_notifications_once detects torrent_duplicate and calls _handle_duplicate_task."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _dup_task(self, *, task_id: str = "dup1", title: str = "Movie.mkv") -> dict:
        return {
            "id": task_id,
            "title": title,
            "status": "error",
            "type": "bt",
            "size": 0,
            "additional": {
                "detail": {"error_detail": "torrent_duplicate"},
            },
        }

    def test_duplicate_task_triggers_handler(self) -> None:
        task = self._dup_task()
        mock_app = MagicMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        mock_handler = AsyncMock()

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"error"}),
            patch.object(bot, "_handle_duplicate_task", mock_handler),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        mock_handler.assert_awaited_once_with(mock_app, task, [task])

    def test_duplicate_handler_not_called_twice(self) -> None:
        task = self._dup_task()
        mock_app = MagicMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        mock_handler = AsyncMock()

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"error"}),
            patch.object(bot, "_handle_duplicate_task", mock_handler),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(mock_handler.await_count, 1)

    def test_regular_error_task_is_not_intercepted(self) -> None:
        task = {
            "id": "err1",
            "title": "Movie.mkv",
            "status": "error",
            "type": "bt",
            "size": 0,
            "additional": {
                "detail": {"error_detail": "disk_full"},
            },
        }
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        mock_handler = AsyncMock()

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"error"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
            patch.object(bot, "_handle_duplicate_task", mock_handler),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        # handler not called — normal notification flow used instead
        mock_handler.assert_not_awaited()
        mock_app.bot.send_message.assert_awaited_once()

    def test_duplicate_state_saved_with_special_key(self) -> None:
        task = self._dup_task()
        mock_app = MagicMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"error"}),
            patch.object(bot, "_handle_duplicate_task", AsyncMock()),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        notified = self._store.load_notified_tasks()
        self.assertIn("dup1", notified)
        self.assertEqual(notified["dup1"]["status"], "error:torrent_duplicate")


class SubscriberNotificationTests(unittest.TestCase):
    """Users who subscribed via sub_notify are notified when the task finishes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = _make_store(self._tmp.name)
        bot.TASK_CARD_MESSAGES.clear()

    def tearDown(self) -> None:
        bot.TASK_CARD_MESSAGES.clear()
        self._tmp.cleanup()

    def test_subscriber_receives_notification_on_finish(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "Movie", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_app.bot.delete_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        # Subscriber 888 signed up; owner 999 is the regular recipient.
        self._store.save_notified_tasks({
            "tid1": {"status": "", "sent": [], "failures": {}, "subscribers": ["888"]},
        })

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        sent_to = {call.kwargs["chat_id"] for call in mock_app.bot.send_message.call_args_list}
        self.assertIn(888, sent_to)
        self.assertIn(999, sent_to)

    def test_subscriber_not_double_notified(self) -> None:
        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "Movie", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_app.bot.delete_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        # Subscriber who is already a regular recipient too.
        self._store.save_notified_tasks({
            "tid1": {"status": "", "sent": [], "failures": {}, "subscribers": ["999"]},
        })

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(mock_app.bot.send_message.await_count, 1)

    def test_failed_subscriber_remains_until_retry_success(self) -> None:
        from telegram.error import TimedOut

        task = {"id": "tid1", "status": "finished", "type": "bt", "title": "Movie", "size": 0}
        attempts: list[int] = []
        subscriber_failed = False

        async def send_message(*, chat_id, **kwargs):
            nonlocal subscriber_failed
            attempts.append(chat_id)
            if chat_id == 888 and not subscriber_failed:
                subscriber_failed = True
                raise TimedOut("read timed out")

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=send_message)
        mock_app.bot.delete_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        self._store.save_notified_tasks({
            "tid1": {
                "status": "",
                "sent": [],
                "failures": {},
                "subscribers": ["888"],
                "plex_done": True,
            },
        })

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
            patch.object(bot, "PLEX_ENABLED", False),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))
            after_first = self._store.load_notified_tasks()["tid1"]
            self.assertEqual(after_first["sent"], ["999"])
            self.assertEqual(after_first["subscribers"], ["888"])
            self.assertTrue(after_first["plex_done"])

            asyncio.run(_run_task_notifications_once(mock_app))

        self.assertEqual(attempts, [888, 999, 888])
        after_retry = self._store.load_notified_tasks()["tid1"]
        self.assertEqual(after_retry["sent"], ["888", "999"])
        self.assertNotIn("subscribers", after_retry)
        self.assertTrue(after_retry["plex_done"])

    def test_subscribers_not_notified_for_non_final_status(self) -> None:
        task = {"id": "tid1", "status": "downloading", "type": "bt", "title": "Movie", "size": 0}
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]

        self._store.save_notified_tasks({
            "tid1": {"status": "", "sent": [], "failures": {}, "subscribers": ["888"]},
        })

        with (
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "state_store", self._store),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"downloading", "finished"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "ALLOWED_CHAT_IDS", {999}),
        ):
            asyncio.run(_run_task_notifications_once(mock_app))

        sent_to = {call.kwargs["chat_id"] for call in mock_app.bot.send_message.call_args_list}
        # subscriber 888 should NOT be notified for downloading status
        self.assertNotIn(888, sent_to)


if __name__ == "__main__":
    unittest.main()
