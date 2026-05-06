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
    _run_auto_delete_finished_once,
    _run_task_notifications_once,
)
from state_store import JsonStateStore


def _make_store(tmp_dir: str) -> JsonStateStore:
    d = Path(tmp_dir)
    return JsonStateStore(
        approved_chat_ids_file=d / "approved.json",
        tracker_processed_file=d / "tracker.json",
        task_owners_file=d / "owners.json",
        notified_tasks_file=d / "notified.json",
        auto_delete_tasks_file=d / "auto_delete.json",
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

    def tearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
