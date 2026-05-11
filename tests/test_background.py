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


if __name__ == "__main__":
    unittest.main()
