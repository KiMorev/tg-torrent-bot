"""Tests for P0 subscription bug fixes (roadmap item 1.2).

Bugs covered:
  A. Jackett-fast-path (_check_jackett_sub_via_rutracker_direct) advanced
     last_episode_end even when the DS download failed → next check saw
     same state and never retried.
  B. Same function ignored notify_policy=final_only → users got
     intermediate per-episode pushes despite asking for final-only mode.
  C. _check_jackett_subscriptions silently advanced state on
     final_only even when auto-download failed → failed updates
     were marked as «seen» and never retried.
  D. Plex confirm dialog for SERIES dropped policy fields → confirming
     a duplicate silently downgraded final_only → each_update.
  F. Retry/queue after a failed download hardcoded subscribe=False —
     restoring a «⬇️📺 Серии» retry just did a plain one-shot download
     with no subscription. Pending-success path now also restores the
     subscription.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from rutracker import RutrackerError
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
    )


def _jackett_rt_sub(
    *, last_end: int = 1, total: int = 10,
    notify_policy: str = bot.NOTIFY_EACH_UPDATE,
    download_policy: str = bot.DOWNLOAD_AUTO_EACH_UPDATE,
) -> dict:
    return {
        "type": "jackett",
        "chat_id": 100,
        "query": "Show",
        "title": f"Show S1E1-1 of {total}",
        "tracker": "rutracker",
        "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
        "last_episode_end": last_end,
        "total_episodes": total,
        "notify_policy": notify_policy,
        "download_policy": download_policy,
        "added_at": "2026-05-01 10:00",
    }


class JackettRtDirectStateAdvanceTests(unittest.TestCase):
    """Bug A: state must NOT advance when DS download fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)
        self._allowed_patch = patch.object(bot, "ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()
        self._tmp.cleanup()

    def _run(self, sub: dict, *, ds_raises: Exception | None = None,
             new_title: str = "Show S1E1-5 of 10",
             task_id: str = "task-99") -> dict:
        """Drive _check_jackett_sub_via_rutracker_direct with a stubbed
        rutracker + ds client. Returns the resulting sub dict."""
        key = "jackett:abc"
        subs = {key: sub}
        rt = MagicMock()
        rt.get_topic_title.return_value = new_title
        rt.download_torrent.return_value = b"fake-torrent-bytes"
        ds = MagicMock()
        if ds_raises:
            ds.create_torrent_file.side_effect = ds_raises
        else:
            ds.create_torrent_file.return_value = task_id
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "rutracker_client", rt),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_build_task_meta_from_title", return_value={}),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
        ):
            handled = asyncio.run(
                bot._check_jackett_sub_via_rutracker_direct(app, subs, key, sub)
            )
        return {"handled": handled, "sub": subs.get(key), "subs": subs,
                "send": app.bot.send_message}

    def test_state_advances_on_successful_download(self):
        from download_station import DownloadStationError
        result = self._run(_jackett_rt_sub(last_end=1, total=10))
        self.assertTrue(result["handled"])
        self.assertEqual(result["sub"]["last_episode_end"], 5)

    def test_state_does_not_advance_when_ds_create_fails(self):
        from download_station import DownloadStationError
        sub = _jackett_rt_sub(last_end=1, total=10)
        result = self._run(sub, ds_raises=DownloadStationError("DSM 119"))
        # State frozen at 1 so next check retries the same update.
        self.assertEqual(result["sub"]["last_episode_end"], 1)

    def test_state_does_not_advance_when_ds_returns_empty_task_id(self):
        sub = _jackett_rt_sub(last_end=1, total=10)
        result = self._run(sub, task_id="")
        self.assertEqual(result["sub"]["last_episode_end"], 1)
        text = result["send"].await_args.kwargs.get("text", "")
        self.assertIn("скачать не удалось", text)
        self.assertNotIn("задача добавлена", text)
        # But the notification with manual-link should still fire.
        result["send"].assert_awaited_once()
        text = result["send"].await_args.kwargs.get("text", "")
        self.assertIn("скачать не удалось", text.lower())

    def test_state_does_not_advance_when_rutracker_download_fails(self):
        from download_station import DownloadStationError
        sub = _jackett_rt_sub(last_end=1, total=10)
        key = "jackett:abc"
        subs = {key: sub}
        rt = MagicMock()
        rt.get_topic_title.return_value = "Show S1E1-5 of 10"
        rt.download_torrent.side_effect = RutrackerError("auth failed")
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "rutracker_client", rt),
            patch.object(bot, "ds_client", MagicMock()),
            patch.object(bot, "_remember_task_owner"),
        ):
            asyncio.run(
                bot._check_jackett_sub_via_rutracker_direct(app, subs, key, sub)
            )
        # last_episode_end stays at 1 — failed RT download triggers retry on next loop.
        self.assertEqual(subs[key]["last_episode_end"], 1)


class JackettRtDirectSeasonCompleteTests(unittest.TestCase):
    """Bug B: notify_policy=final_only must suppress intermediate pushes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)
        self._allowed_patch = patch.object(bot, "ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()
        self._tmp.cleanup()

    def _run(self, sub: dict, new_title: str = "Show S1E1-5 of 10"):
        key = "jackett:abc"
        subs = {key: sub}
        rt = MagicMock()
        rt.get_topic_title.return_value = new_title
        rt.download_torrent.return_value = b"x"
        ds = MagicMock()
        ds.create_torrent_file.return_value = "task-99"
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "rutracker_client", rt),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_build_task_meta_from_title", return_value={}),
        ):
            asyncio.run(
                bot._check_jackett_sub_via_rutracker_direct(app, subs, key, sub)
            )
        return subs, app.bot.send_message

    def test_final_only_suppresses_intermediate_push(self):
        sub = _jackett_rt_sub(
            last_end=1, total=10, notify_policy=bot.NOTIFY_FINAL_ONLY,
        )
        subs, send = self._run(sub, new_title="Show S1E1-5 of 10")
        # Episode 5/10 → silent advance, no push.
        send.assert_not_awaited()
        # But state HAS advanced (download succeeded).
        self.assertEqual(subs["jackett:abc"]["last_episode_end"], 5)

    def test_final_only_pushes_when_season_done(self):
        sub = _jackett_rt_sub(
            last_end=8, total=10, notify_policy=bot.NOTIFY_FINAL_ONLY,
        )
        subs, send = self._run(sub, new_title="Show S1E1-10 of 10")
        # Final episode → push fires.
        send.assert_awaited_once()
        text = send.await_args.kwargs.get("text", "")
        self.assertIn("сезон", text.lower())
        # Subscription removed on completion.
        self.assertNotIn("jackett:abc", subs)

    def test_each_update_still_pushes_on_intermediate(self):
        """Regression guard — each_update default must keep behaviour."""
        sub = _jackett_rt_sub(last_end=1, total=10)
        _subs, send = self._run(sub, new_title="Show S1E1-5 of 10")
        send.assert_awaited_once()


class JackettSubsSeasonCompleteFailedDownloadTests(unittest.TestCase):
    """Bug C: _check_jackett_subscriptions must NOT silently advance state
    when auto-download fails in final_only mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)
        self._allowed_patch = patch.object(bot, "ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()
        self._tmp.cleanup()

    def test_failed_download_in_final_only_falls_back_to_notify(self):
        """When auto-download returns None (no task_id), the silent-advance
        branch must NOT fire — instead the user gets a notify-with-error
        message so they can recover manually."""
        from jackett import JackettResult, JackettError
        sub_key = "jackett:bug-c"
        # Use a non-rutracker topic_url so _check_jackett_sub_via_rutracker_direct
        # short-circuits and we exercise the main Jackett-search path.
        sub = {
            "type": "jackett",
            "chat_id": 100,
            "query": "Some show",
            "title": "",
            "tracker": "kinozal",
            "topic_url": "https://kinozal.tv/topic/77",
            "notify_policy": bot.NOTIFY_FINAL_ONLY,
            "download_policy": bot.DOWNLOAD_AUTO_EACH_UPDATE,
            "last_check": "2026-05-01 10:00",
            "seen_titles": [],
        }
        self.store.save_topic_subscriptions({sub_key: sub})

        candidate = JackettResult(
            title="Show S1E1-5 of 10", size="10 GB", seeders=42,
            torrent_url="http://jk/dl?p=xxx", magnet_url=None,
            tracker="kinozal", topic_url="https://kinozal.tv/topic/77",
        )
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        jackett = MagicMock()
        jackett.search.return_value = [candidate]

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "select_jackett_subscription_candidate", return_value=candidate),
            patch.object(bot, "_check_jackett_sub_via_rutracker_direct",
                         AsyncMock(return_value=False)),
            patch.object(bot, "_jackett_subscription_auto_download",
                         AsyncMock(side_effect=JackettError("DS unavailable"))),
        ):
            asyncio.run(bot._check_jackett_subscriptions(app))

        # User got a manual-link message (NOT silent advance).
        app.bot.send_message.assert_awaited_once()
        text = app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("вручную", text.lower())

    def test_empty_task_id_counts_as_failed_download(self):
        """An empty task_id is not a successful DS auto-download."""
        from jackett import JackettResult
        sub_key = "jackett:empty-task"
        sub = {
            "type": "jackett",
            "chat_id": 100,
            "query": "Some show",
            "title": "",
            "tracker": "kinozal",
            "topic_url": "https://kinozal.tv/topic/77",
            "notify_policy": bot.NOTIFY_FINAL_ONLY,
            "download_policy": bot.DOWNLOAD_AUTO_EACH_UPDATE,
            "last_check": "2026-05-01 10:00",
            "seen_titles": [],
        }
        self.store.save_topic_subscriptions({sub_key: sub})

        candidate = JackettResult(
            title="Show S1E1-5 of 10", size="10 GB", seeders=42,
            torrent_url="http://jk/dl?p=xxx", magnet_url=None,
            tracker="kinozal", topic_url="https://kinozal.tv/topic/77",
        )
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        jackett = MagicMock()
        jackett.search.return_value = [candidate]

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "select_jackett_subscription_candidate", return_value=candidate),
            patch.object(bot, "_check_jackett_sub_via_rutracker_direct",
                         AsyncMock(return_value=False)),
            patch.object(bot, "_jackett_subscription_auto_download",
                         AsyncMock(return_value="")),
        ):
            asyncio.run(bot._check_jackett_subscriptions(app))

        app.bot.send_message.assert_awaited_once()
        text = app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("вручную", text.lower())
        self.assertNotIn("задача добавлена", text.lower())

    def test_subscription_torrent_empty_task_id_does_not_retry_as_magnet(self):
        """If DS accepted the .torrent but gave no id, do not create a duplicate magnet."""
        from jackett import JackettResult
        candidate = JackettResult(
            title="Show S1E1-5 of 10", size="10 GB", seeders=42,
            torrent_url="http://jk/dl?p=xxx",
            magnet_url="magnet:?xt=urn:btih:deadbeef",
            tracker="kinozal",
            topic_url="https://kinozal.tv/topic/77",
        )
        jackett = MagicMock()
        jackett.download_torrent.return_value = b"d8:announce4:test"
        ds = MagicMock()
        ds.create_torrent_file.return_value = ""
        ds.create_magnet = MagicMock()

        with (
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "TMP_DIR", Path(self._tmp.name)),
            self.assertRaises(bot.MissingTaskIdError),
        ):
            asyncio.run(bot._jackett_subscription_auto_download(candidate, chat_id=100))

        ds.create_magnet.assert_not_called()

    def test_successful_download_in_final_only_still_silent_advances(self):
        """Regression guard — happy path of final_only still silently
        advances when download succeeds and season isn't done yet."""
        from jackett import JackettResult
        sub_key = "jackett:bug-c-happy"
        sub = {
            "type": "jackett",
            "chat_id": 100,
            "query": "Show",
            "title": "",
            "tracker": "kinozal",
            "topic_url": "https://kinozal.tv/topic/77",
            "notify_policy": bot.NOTIFY_FINAL_ONLY,
            "download_policy": bot.DOWNLOAD_AUTO_EACH_UPDATE,
            "last_check": "2026-05-01 10:00",
            "seen_titles": [],
        }
        self.store.save_topic_subscriptions({sub_key: sub})

        candidate = JackettResult(
            title="Show S1E1-5 of 10", size="10 GB", seeders=10,
            torrent_url="http://jk/x", magnet_url=None,
            tracker="kinozal", topic_url="https://kinozal.tv/topic/77",
        )
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        jackett = MagicMock()
        jackett.search.return_value = [candidate]

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "select_jackett_subscription_candidate", return_value=candidate),
            patch.object(bot, "_check_jackett_sub_via_rutracker_direct",
                         AsyncMock(return_value=False)),
            patch.object(bot, "_jackett_subscription_auto_download",
                         AsyncMock(return_value="task-1")),
        ):
            asyncio.run(bot._check_jackett_subscriptions(app))

        # No push to user — silent advance.
        app.bot.send_message.assert_not_awaited()

    def test_complete_normal_jackett_subscription_is_removed_after_download(self):
        """Normal Jackett-search path must remove the subscription on season completion."""
        from jackett import JackettResult
        sub_key = "jackett:complete"
        sub = {
            "type": "jackett",
            "chat_id": 100,
            "query": "Show",
            "title": "Show S1E1-9 of 10",
            "tracker": "kinozal",
            "topic_url": "https://kinozal.tv/topic/77",
            "notify_policy": "each_update",
            "download_policy": "auto_each_update",
            "last_episode_end": 9,
            "total_episodes": 10,
            "last_check": "2026-05-01 10:00",
            "seen_titles": [],
        }
        self.store.save_topic_subscriptions({sub_key: sub})

        candidate = JackettResult(
            title="Show S1E1-10 of 10", size="10 GB", seeders=10,
            torrent_url="http://jk/x", magnet_url=None,
            tracker="kinozal", topic_url="https://kinozal.tv/topic/77",
        )
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        jackett = MagicMock()
        jackett.search.return_value = [candidate]

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "select_jackett_subscription_candidate", return_value=candidate),
            patch.object(bot, "_check_jackett_sub_via_rutracker_direct",
                         AsyncMock(return_value=False)),
            patch.object(bot, "_jackett_subscription_auto_download",
                         AsyncMock(return_value="task-final")),
        ):
            asyncio.run(bot._check_jackett_subscriptions(app))

        self.assertNotIn(sub_key, self.store.load_topic_subscriptions())
        text = app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("сезон заверш", text.lower())
        self.assertIn("Подписка снята", text)

    def test_complete_normal_jackett_notify_only_subscription_is_removed(self):
        """Final notification-only Jackett updates should not keep a dead subscription."""
        from jackett import JackettResult
        from subscription_policy import DOWNLOAD_NOTIFY_ONLY, NOTIFY_FINAL_ONLY

        sub_key = "jackett:notify-final"
        sub = {
            "type": "jackett",
            "chat_id": 100,
            "query": "Show",
            "title": "Show S1E1-9 of 10",
            "tracker": "kinozal",
            "topic_url": "https://kinozal.tv/topic/77",
            "notify_policy": NOTIFY_FINAL_ONLY,
            "download_policy": DOWNLOAD_NOTIFY_ONLY,
            "last_episode_end": 9,
            "total_episodes": 10,
            "last_check": "2026-05-01 10:00",
            "seen_titles": [],
        }
        self.store.save_topic_subscriptions({sub_key: sub})

        candidate = JackettResult(
            title="Show S1E1-10 of 10", size="10 GB", seeders=10,
            torrent_url="http://jk/x", magnet_url=None,
            tracker="kinozal", topic_url="https://kinozal.tv/topic/77",
        )
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        jackett = MagicMock()
        jackett.search.return_value = [candidate]

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "select_jackett_subscription_candidate", return_value=candidate),
            patch.object(bot, "_check_jackett_sub_via_rutracker_direct",
                         AsyncMock(return_value=False)),
            patch.object(bot, "_jackett_subscription_auto_download",
                         AsyncMock()) as dl,
        ):
            asyncio.run(bot._check_jackett_subscriptions(app))

        dl.assert_not_awaited()
        self.assertNotIn(sub_key, self.store.load_topic_subscriptions())
        text = app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("Авто-загрузка отключена", text)
        self.assertIn("Подписка снята", text)


    def test_send_failure_after_jackett_download_retries_notification_only(self):
        from jackett import JackettResult
        sub_key = "jackett:pending-send"
        sub = {
            "type": "jackett",
            "chat_id": 100,
            "query": "Show",
            "title": "Show S1E1-1 of 10",
            "tracker": "kinozal",
            "topic_url": "https://kinozal.tv/topic/77",
            "notify_policy": bot.NOTIFY_EACH_UPDATE,
            "download_policy": bot.DOWNLOAD_AUTO_EACH_UPDATE,
            "last_episode_end": 1,
            "total_episodes": 10,
            "last_check": "2026-05-01 10:00",
            "seen_titles": [],
        }
        self.store.save_topic_subscriptions({sub_key: sub})

        candidate = JackettResult(
            title="Show S1E1-5 of 10", size="10 GB", seeders=10,
            torrent_url="http://jk/x", magnet_url=None,
            tracker="kinozal", topic_url="https://kinozal.tv/topic/77",
        )
        app = MagicMock()
        app.bot.send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None])
        jackett = MagicMock()
        jackett.search.return_value = [candidate]
        download = AsyncMock(return_value="task-1")

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "select_jackett_subscription_candidate", return_value=candidate),
            patch.object(bot, "_check_jackett_sub_via_rutracker_direct",
                         AsyncMock(return_value=False)),
            patch.object(bot, "_jackett_subscription_auto_download", download),
        ):
            asyncio.run(bot._check_jackett_subscriptions(app))
            stored = self.store.load_topic_subscriptions()[sub_key]
            self.assertIn("pending_notification", stored)
            self.assertEqual(stored["last_episode_end"], 1)

            asyncio.run(bot._check_jackett_subscriptions(app))

        stored = self.store.load_topic_subscriptions()[sub_key]
        self.assertNotIn("pending_notification", stored)
        self.assertEqual(stored["last_episode_end"], 5)
        self.assertEqual(download.await_count, 1)
        self.assertEqual(jackett.search.call_count, 1)


class PendingDownloadSubscribePreserveTests(unittest.TestCase):
    """Bug F: queueing a failed «⬇️📺 Серии» / «⬇️🎯 Сезон» must restore the
    subscription when the pending download eventually succeeds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)
        self._allowed_patch = patch.object(bot, "ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()
        self._tmp.cleanup()

    def _seed_subscribe_entry(
        self, *, source: str,
        notify_policy: str = bot.NOTIFY_EACH_UPDATE,
        download_policy: str = bot.DOWNLOAD_AUTO_EACH_UPDATE,
        title: str = "Some Show 2 of 10") -> str:
        from datetime import datetime
        entry_id = "p1"
        self.store.save_pending_downloads({
            entry_id: {
                "chat_id": 100,
                "added_at": datetime.now(bot.DISPLAY_TIMEZONE).isoformat(),
                "title": title,
                "topic_url": "https://rutracker.org/forum/viewtopic.php?t=99",
                "torrent_url": "http://jk/dl?x",
                "magnet_url": None,
                "tracker": "rutracker",
                "source": source,
                "subscribe": True,
                "notify_policy": notify_policy,
                "download_policy": download_policy,
                "attempts": 0,
                "last_attempt_at": None,
                "last_error": "",
            }
        })
        return entry_id

    def _run_loop_with_success(self):
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download",
                         AsyncMock(return_value=("task-x", "torrent-файл"))),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
        ):
            asyncio.run(bot._run_pending_downloads_once(mock_app))
        return mock_app

    def test_jackett_subscribe_restored_on_pending_success(self):
        self._seed_subscribe_entry(
            source="jackett", notify_policy=bot.NOTIFY_FINAL_ONLY,
        )
        app = self._run_loop_with_success()
        # Subscription was created with the correct policy.
        subs = self.store.load_topic_subscriptions()
        jackett_subs = [s for s in subs.values() if s.get("type") == "jackett"]
        self.assertEqual(len(jackett_subs), 1)
        self.assertEqual(jackett_subs[0]["notify_policy"], bot.NOTIFY_FINAL_ONLY)
        self.assertEqual(jackett_subs[0]["download_policy"], bot.DOWNLOAD_AUTO_EACH_UPDATE)
        # User notified that subscription was restored.
        text = app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("Подписка восстановлена", text)

    def test_rutracker_subscribe_restored_on_pending_success(self):
        self._seed_subscribe_entry(
            source="rutracker",
            title="Some Show S1E1-2 of 10",
        )
        app = self._run_loop_with_success()
        subs = self.store.load_topic_subscriptions()
        # Rutracker subs are keyed by topic_id (99 here).
        self.assertIn("99", subs)
        self.assertEqual(subs["99"]["notify_policy"], bot.NOTIFY_EACH_UPDATE)
        self.assertEqual(subs["99"]["download_policy"], bot.DOWNLOAD_AUTO_EACH_UPDATE)
        self.assertEqual(subs["99"]["last_episode_end"], 2)
        self.assertEqual(subs["99"]["total_episodes"], 10)

    def test_no_subscribe_intent_no_subscription_created(self):
        """Regression guard — entries without subscribe=True must not create subs."""
        from datetime import datetime
        self.store.save_pending_downloads({
            "p2": {
                "chat_id": 100, "added_at": datetime.now(bot.DISPLAY_TIMEZONE).isoformat(),
                "title": "One-shot Movie", "topic_url": "", "torrent_url": "http://x",
                "magnet_url": None, "tracker": "", "source": "jackett",
                "subscribe": False,
                "attempts": 0, "last_attempt_at": None, "last_error": "",
            }
        })
        self._run_loop_with_success()
        subs = self.store.load_topic_subscriptions()
        self.assertEqual(subs, {})

    def test_pending_success_notification_retry_does_not_download_again(self):
        from datetime import datetime
        self.store.save_pending_downloads({
            "p3": {
                "chat_id": 100,
                "added_at": datetime.now(bot.DISPLAY_TIMEZONE).isoformat(),
                "title": "One-shot Movie",
                "topic_url": "",
                "torrent_url": "http://x",
                "magnet_url": None,
                "tracker": "",
                "source": "jackett",
                "subscribe": False,
                "attempts": 0,
                "last_attempt_at": None,
                "last_error": "",
            }
        })
        app = MagicMock()
        app.bot.send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None])
        attempt = AsyncMock(return_value=("task-x", "torrent-файл"))

        with (
            patch.object(bot, "state_store", self.store),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_TTL_HOURS", 24.0),
            patch.object(bot, "_attempt_pending_download", attempt),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_record_download_added_history"),
        ):
            asyncio.run(bot._run_pending_downloads_once(app))
            pending_after_first = self.store.load_pending_downloads()
            self.assertEqual(pending_after_first["p3"]["notification_pending"], "success")

            asyncio.run(bot._run_pending_downloads_once(app))

        self.assertEqual(attempt.await_count, 1)
        self.assertEqual(app.bot.send_message.await_count, 2)
        self.assertEqual(self.store.load_pending_downloads(), {})

    def test_pending_entry_carries_policy_from_search(self):
        """Bug F upstream: _pending_download_entry_from_result must preserve
        policy fields when building the queue entry."""
        entry = bot._pending_download_entry_from_result(
            {"title": "X", "torrent_url": "u", "tracker_name": "t", "source": "jackett"},
            chat_id=100, subscribe=True,
            notify_policy=bot.NOTIFY_FINAL_ONLY,
            download_policy=bot.DOWNLOAD_ONLY_WHEN_COMPLETE,
            error="boom",
        )
        self.assertEqual(entry["notify_policy"], bot.NOTIFY_FINAL_ONLY)
        self.assertEqual(entry["download_policy"], bot.DOWNLOAD_ONLY_WHEN_COMPLETE)
        self.assertTrue(entry["subscribe"])

    def test_pending_entry_preserves_canonical_meta_for_task_meta(self):
        entry = bot._pending_download_entry_from_result(
            {
                "title": "Noisy.Release.2026.1080p.WEB-DL",
                "movie_title": "Canonical Movie",
                "year": 2026,
                "quality": "1080p",
                "topic_id": "12345",
                "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                "torrent_url": "u",
                "tracker_name": "rutracker",
                "source": "jackett",
            },
            chat_id=100, subscribe=False, error="boom",
        )

        restored = bot._pending_entry_to_search_result(entry)
        self.assertEqual(restored["movie_title"], "Canonical Movie")
        self.assertEqual(restored["year"], 2026)
        self.assertEqual(restored["quality"], "1080p")
        self.assertEqual(restored["topic_id"], "12345")

        meta = bot._build_task_meta_from_result(restored, source="pending")
        self.assertEqual(meta["title"], "Canonical Movie")
        self.assertEqual(meta["year"], 2026)
        self.assertEqual(meta["quality"], "1080")
        self.assertEqual(meta["source"], "pending")


if __name__ == "__main__":
    unittest.main()
