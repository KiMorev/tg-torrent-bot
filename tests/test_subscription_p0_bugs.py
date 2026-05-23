"""Tests for P0 subscription bug fixes (roadmap item 1.2).

Bugs covered:
  A. Jackett-fast-path (_check_jackett_sub_via_rutracker_direct) advanced
     last_episode_end even when the DS download failed → next check saw
     same state and never retried.
  B. Same function ignored notify_mode=season_complete → users got
     intermediate per-episode pushes despite asking for silent mode.
  C. _check_jackett_subscriptions silently advanced state on
     season_complete even when auto-download failed → failed updates
     were marked as «seen» and never retried.
  D. Plex confirm dialog for SERIES dropped notify_mode → confirming
     a duplicate silently downgraded season_complete → per_episode.
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
    *, last_end: int = 1, total: int = 10, notify_mode: str = "per_episode",
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
        "notify_mode": notify_mode,
        "added_at": "2026-05-01 10:00",
    }


class JackettRtDirectStateAdvanceTests(unittest.TestCase):
    """Bug A: state must NOT advance when DS download fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, sub: dict, *, ds_raises: Exception | None = None,
             new_title: str = "Show S1E1-5 of 10") -> dict:
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
    """Bug B: notify_mode=season_complete must suppress intermediate pushes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self):
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

    def test_season_complete_suppresses_intermediate_push(self):
        sub = _jackett_rt_sub(last_end=1, total=10, notify_mode="season_complete")
        subs, send = self._run(sub, new_title="Show S1E1-5 of 10")
        # Episode 5/10 → silent advance, no push.
        send.assert_not_awaited()
        # But state HAS advanced (download succeeded).
        self.assertEqual(subs["jackett:abc"]["last_episode_end"], 5)

    def test_season_complete_pushes_when_season_done(self):
        sub = _jackett_rt_sub(last_end=8, total=10, notify_mode="season_complete")
        subs, send = self._run(sub, new_title="Show S1E1-10 of 10")
        # Final episode → push fires.
        send.assert_awaited_once()
        text = send.await_args.kwargs.get("text", "")
        self.assertIn("сезон", text.lower())
        # Subscription removed on completion.
        self.assertNotIn("jackett:abc", subs)

    def test_per_episode_still_pushes_on_intermediate(self):
        """Regression guard — per_episode default must keep behaviour."""
        sub = _jackett_rt_sub(last_end=1, total=10, notify_mode="per_episode")
        _subs, send = self._run(sub, new_title="Show S1E1-5 of 10")
        send.assert_awaited_once()


class JackettSubsSeasonCompleteFailedDownloadTests(unittest.TestCase):
    """Bug C: _check_jackett_subscriptions must NOT silently advance state
    when auto-download fails in season_complete mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_download_in_season_complete_falls_back_to_notify(self):
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
            "notify_mode": "season_complete",
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

    def test_successful_download_in_season_complete_still_silent_advances(self):
        """Regression guard — happy path of season_complete still silently
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
            "notify_mode": "season_complete",
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


class PendingDownloadSubscribePreserveTests(unittest.TestCase):
    """Bug F: queueing a failed «⬇️📺 Серии» / «⬇️🎯 Сезон» must restore the
    subscription when the pending download eventually succeeds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_subscribe_entry(self, *, source: str, notify_mode: str,
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
                "notify_mode": notify_mode,
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
        self._seed_subscribe_entry(source="jackett", notify_mode="season_complete")
        app = self._run_loop_with_success()
        # Subscription was created with the correct notify_mode.
        subs = self.store.load_topic_subscriptions()
        jackett_subs = [s for s in subs.values() if s.get("type") == "jackett"]
        self.assertEqual(len(jackett_subs), 1)
        self.assertEqual(jackett_subs[0]["notify_mode"], "season_complete")
        # User notified that subscription was restored.
        text = app.bot.send_message.await_args.kwargs.get("text", "")
        self.assertIn("Подписка восстановлена", text)

    def test_rutracker_subscribe_restored_on_pending_success(self):
        self._seed_subscribe_entry(
            source="rutracker", notify_mode="per_episode",
            title="Some Show S1E1-2 of 10",
        )
        app = self._run_loop_with_success()
        subs = self.store.load_topic_subscriptions()
        # Rutracker subs are keyed by topic_id (99 here).
        self.assertIn("99", subs)
        self.assertEqual(subs["99"]["notify_mode"], "per_episode")
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
                "subscribe": False, "notify_mode": "per_episode",
                "attempts": 0, "last_attempt_at": None, "last_error": "",
            }
        })
        self._run_loop_with_success()
        subs = self.store.load_topic_subscriptions()
        self.assertEqual(subs, {})

    def test_pending_entry_carries_notify_mode_from_search(self):
        """Bug F upstream: _pending_download_entry_from_result must preserve
        notify_mode when building the queue entry."""
        entry = bot._pending_download_entry_from_result(
            {"title": "X", "torrent_url": "u", "tracker_name": "t", "source": "jackett"},
            chat_id=100, subscribe=True, notify_mode="season_complete", error="boom",
        )
        self.assertEqual(entry["notify_mode"], "season_complete")
        self.assertTrue(entry["subscribe"])


if __name__ == "__main__":
    unittest.main()
