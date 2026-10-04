"""Post-download episode names, Plex scan ordering and incomplete-season polling."""
import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "999")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from plex import PlexSeason, PlexShow, PlexTimeoutError


class PlexFilenameNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.storage = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.title = "Oldschool.S02.2026.WEB-DL.1080p.ExKinoRay"
        self.folder = self.storage / self.title
        self.folder.mkdir()
        self.meta = {"kind": "series", "series_query": "Олдскул", "season_num": 2}
        self.show = PlexShow("Олдскул", 2025, "show", seasons={2: PlexSeason("season2", 2, 0)})
        self.app = MagicMock()
        self.app.bot.send_message = AsyncMock()
        self.client = MagicMock()
        self.client.refresh_show_section.return_value = True
        self.refresh = AsyncMock()
        self.history = MagicMock()
        self.done = MagicMock()
        self.remember = MagicMock()
        self.mark_sent = bot._mark_plex_poll_sent
        for name, value in {
            "ALLOWED_CHAT_IDS": {100},
            "STORAGE_MOUNT_PATH": str(self.storage),
            "plex_client": self.client,
            "_plex_shows_library": {("олдскул", 2025): self.show},
            "_plex_show_find": MagicMock(return_value=self.show),
            "_refresh_plex_library": self.refresh,
            "_plex_wait_for_poll_interval": AsyncMock(),
            "_plex_consecutive_failures": 0,
            "_PLEX_POLLING_TASKS": {},
            "_load_notified_tasks": MagicMock(return_value={}),
            "_mark_plex_poll_sent": MagicMock(),
            "_mark_plex_poll_done": self.done,
            "_record_download_history": self.history,
            "_remember_task_meta": self.remember,
        }.items():
            self.stack.enter_context(patch.object(bot, name, value))

    def poll(self, *, title=None, attempts=3):
        asyncio.run(bot._plex_poll_after_finish(
            self.app, "task", title or self.title, [100], meta=self.meta,
            max_attempts=attempts, interval_seconds=0,
        ))

    def plex_path(self, path):
        return "/volume1/video/" + path.relative_to(self.storage).as_posix()

    def test_pack_is_renamed_before_scan_and_waits_for_correct_season_and_paths(self):
        sources = [self.folder / f"Oldschool.S02.E{ep:02d}.2026.mkv" for ep in [1, 2]]
        targets = [path.with_name(path.name.replace("S02.E", "S02E")) for path in sources]
        for source in sources:
            source.write_bytes(b"episode")

        def scan():
            self.assertTrue(all(path.exists() for path in targets))
            self.assertFalse(any(path.exists() for path in sources))
            return True

        async def refresh(*args, **kwargs):
            self.client.refresh_show_section.assert_called_once()
            attempt = self.refresh.await_count
            if attempt == 1:
                self.show.seasons = {1: PlexSeason("stale", 1, 2, file_paths=[self.plex_path(path) for path in sources])}
            else:
                paths = sources if attempt == 2 else targets
                self.show.seasons = {2: PlexSeason("season2", 2, 2, file_paths=[self.plex_path(path) for path in paths])}

        self.client.refresh_show_section.side_effect = scan
        self.refresh.side_effect = refresh
        self.poll()

        self.assertEqual(self.refresh.await_count, 3)
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn("Сезон 2", self.app.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.history.call_args_list[0].args[0], "files_normalized")
        self.assertEqual(self.history.call_args_list[0].kwargs["renamed_count"], 2)

    def test_catch_up_ignores_existing_season_until_new_episode_is_indexed(self):
        self.meta["source"] = "continue"
        old = self.folder / "Oldschool.S02E01.2026.mkv"
        new = self.folder / "Oldschool.S02.E02.2026.mkv"
        target = self.folder / "Oldschool.S02E02.2026.mkv"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        season = self.show.seasons[2]
        season.file_paths = [self.plex_path(old)]

        async def refresh(*args, **kwargs):
            if self.refresh.await_count == 2:
                season.file_paths.append(self.plex_path(target))

        self.refresh.side_effect = refresh
        self.poll()

        self.assertEqual(old.read_bytes(), b"old")
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(self.refresh.await_count, 2)
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn("добавлен в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_single_episode_catch_up_keeps_working_with_original_ds_title_on_retry(self):
        self.meta["source"] = "continue"
        source = self.storage / "Oldschool.S02.E13.2026.mkv"
        target = self.storage / "Oldschool.S02E13.2026.mkv"
        source.write_bytes(b"episode 13")
        self.show.seasons[2].file_paths = [self.plex_path(target)]

        self.poll(title=source.name, attempts=1)
        self.poll(title=source.name, attempts=1)

        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"episode 13")
        self.assertEqual(self.app.bot.send_message.await_count, 2)
        self.assertTrue(all("Сезон 2" in call.kwargs["text"] for call in self.app.bot.send_message.await_args_list))
        self.assertEqual(sum(call.args[0] == "files_normalized" for call in self.history.call_args_list), 1)

    def test_collision_defers_polling_without_overwriting_or_marking_done(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        target = self.folder / "Oldschool.S02E01.mkv"
        source.write_bytes(b"downloaded")
        target.write_bytes(b"existing")

        self.poll()

        self.assertEqual(source.read_bytes(), b"downloaded")
        self.assertEqual(target.read_bytes(), b"existing")
        self.client.refresh_show_section.assert_not_called()
        self.refresh.assert_not_awaited()
        self.done.assert_not_called()
        self.assertIn("Plex-проверка отложена", self.app.bot.send_message.await_args.kwargs["text"])
        buttons = [button for row in self.app.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == "task:norm_skip:task" for button in buttons))
        self.assertTrue(any(button.callback_data == "task:close:" for button in buttons))

    def test_write_failure_defers_polling(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        with patch.object(Path, "rename", side_effect=PermissionError("read only")):
            self.poll()
        self.client.refresh_show_section.assert_not_called()
        self.refresh.assert_not_awaited()
        self.done.assert_not_called()

    def test_normalization_error_is_not_repeated_while_waiting_for_manual_fix(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        target = self.folder / "Oldschool.S02E01.mkv"
        source.write_bytes(b"downloaded")
        target.write_bytes(b"existing")
        notified = {}
        with (
            patch.object(bot, "_load_notified_tasks", return_value=notified),
            patch.object(bot, "_save_notified_tasks"),
            patch.object(bot, "_mark_plex_poll_sent", self.mark_sent),
        ):
            self.poll()
            self.poll()
        self.app.bot.send_message.assert_awaited_once()
        self.assertEqual(notified["task"]["plex_poll"]["normalization_failed"], ["100"])

    def test_scan_failure_is_retried_before_lookup(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        target = self.folder / "Oldschool.S02E01.mkv"
        source.write_bytes(b"episode")
        self.show.seasons[2].file_paths = [self.plex_path(target)]
        self.client.refresh_show_section.side_effect = [PlexTimeoutError("offline"), False, True]

        self.poll()

        self.assertEqual(self.client.refresh_show_section.call_count, 3)
        self.refresh.assert_awaited_once()
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn("Сезон 2", self.app.bot.send_message.await_args.kwargs["text"])

    def test_series_fallback_skips_wrong_season_and_finds_correct_duplicate_show(self):
        fp = f"/volume1/video/{self.title}/Oldschool.S02E01.mkv"
        wrong = PlexShow("Олдскул", 2025, "wrong", seasons={1: PlexSeason("season1", 1, 1, file_paths=[fp])})
        self.show.seasons[2].file_paths = [fp]
        with (
            patch.object(bot, "_plex_show_find", return_value=None),
            patch.object(bot, "_plex_shows_library", {("wrong", 2025): wrong, ("right", 2025): self.show}),
        ):
            target, _, title = asyncio.run(bot._plex_poll_lookup_target(self.title, self.meta))
        self.assertIs(target, self.show.seasons[2])
        self.assertIn("Сезон 2", title)
