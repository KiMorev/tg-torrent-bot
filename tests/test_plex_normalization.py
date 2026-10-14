"""Episode hints, scan ordering, verified assignments and manual guards."""
import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree import ElementTree

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "999")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from filename_normalizer import RenameItem
from plex import PlexClient, PlexParseError, PlexSeason, PlexShow, PlexTimeoutError
from state_store import JsonStateStore


class PlexEpisodeMatchTests(unittest.TestCase):
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
        self.client.get_show_seasons.side_effect = lambda rating_key: self.show.seasons
        self.refresh = AsyncMock()
        self.history = MagicMock()
        self.done = MagicMock()
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
            "_remember_task_meta": MagicMock(),
        }.items():
            self.stack.enter_context(patch.object(bot, name, value))

    def poll(self, *, title=None, attempts=3):
        asyncio.run(bot._plex_poll_after_finish(
            self.app, "task", title or self.title, [100], meta=self.meta,
            max_attempts=attempts, interval_seconds=0,
        ))

    def plex_path(self, path):
        return "/volume1/video/" + path.relative_to(self.storage).as_posix()

    def index_files(self, season_number, assignments):
        paths = {self.plex_path(path): tuple(numbers) for path, numbers in assignments.items()}
        season = PlexSeason("season", season_number, len(paths), file_paths=list(paths), episode_file_numbers=paths)
        self.show.seasons = {season_number: season}
        return season

    def test_pack_writes_hints_before_scan_and_waits_for_correct_season_and_each_episode(self):
        files = [self.folder / f"Oldschool.S02.E{ep:02d}.2026.mkv" for ep in (1, 2)]
        for path in files:
            path.write_bytes(b"episode")

        def scan():
            hints = (self.folder / ".plexmatch").read_text(encoding="utf-8")
            for ep, path in enumerate(files, start=1):
                self.assertIn(f"ep: S02E{ep:02d}: {path.name}", hints)
                self.assertEqual(path.read_bytes(), b"episode")
                self.assertFalse(path.with_name(path.name.replace("S02.E", "S02E")).exists())
            return True

        async def refresh(*args, **kwargs):
            self.client.refresh_show_section.assert_called_once()
            if self.refresh.await_count == 1:
                self.index_files(1, {files[0]: (1,), files[1]: (2,)})
            elif self.refresh.await_count == 2:
                self.index_files(2, {files[0]: (1,), files[1]: (1,)})
            else:
                self.index_files(2, {files[0]: (1,), files[1]: (2,)})

        self.client.refresh_show_section.side_effect = scan
        self.refresh.side_effect = refresh
        self.poll()
        self.assertEqual(self.refresh.await_count, 3)
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn("Сезон 2", self.app.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.history.call_args_list[0].args[0], "plex_match_hints")
        self.assertEqual(self.history.call_args_list[0].kwargs["hint_count"], 2)

    def test_catch_up_waits_for_new_raw_episode_path(self):
        old = self.folder / "Oldschool.S02E01.mkv"
        new = self.folder / "Oldschool.S02.E02.mkv"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        self.index_files(2, {old: (1,)})

        async def refresh(*args, **kwargs):
            if self.refresh.await_count == 2:
                self.index_files(2, {old: (1,), new: (2,)})

        self.refresh.side_effect = refresh
        self.poll()
        self.assertEqual(old.read_bytes(), b"old")
        self.assertEqual(new.read_bytes(), b"new")
        self.assertEqual(self.refresh.await_count, 2)
        self.assertIn("добавлен в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_single_episode_repeat_preserves_ds_title_and_only_records_changed_hints(self):
        source = self.storage / "Oldschool.S02.E13.2026.mkv"
        source.write_bytes(b"episode 13")
        self.index_files(2, {source: (13,)})
        self.poll(title=source.name, attempts=1)
        self.poll(title=source.name, attempts=1)
        self.assertEqual(source.read_bytes(), b"episode 13")
        self.assertFalse(source.with_name("Oldschool.S02E13.2026.mkv").exists())
        self.assertEqual(self.app.bot.send_message.await_count, 2)
        self.assertEqual(sum(call.args[0] == "plex_match_hints" for call in self.history.call_args_list), 1)

    def test_standard_format_needs_no_hints_but_still_verifies_episode_index(self):
        source = self.folder / "Oldschool.S02E01.mkv"
        source.write_bytes(b"episode")
        self.index_files(2, {source: (2,)})
        self.poll(attempts=1)
        self.assertFalse((self.folder / ".plexmatch").exists())
        self.assertIn("не появился в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_multi_episode_file_requires_all_and_only_expected_indices(self):
        source = self.folder / "Oldschool.S02.E01-E02.mkv"
        source.write_bytes(b"two episodes")
        expected = (source.relative_to(self.storage).as_posix(),)
        season = self.index_files(2, {source: (1,)})
        self.assertFalse(bot._plex_season_matches_ds_title(season, self.title, expected_files=expected))
        season.episode_file_numbers[self.plex_path(source)] = (1, 2)
        self.assertTrue(bot._plex_season_matches_ds_title(season, self.title, expected_files=expected))
        season.episode_file_numbers[self.plex_path(source)] = (1, 2, 3)
        self.assertFalse(bot._plex_season_matches_ds_title(season, self.title, expected_files=expected))

    def test_cross_season_identity_collision_keeps_poll_retryable_and_background_retries(self):
        source = self.folder / "Oldschool.S02.E17.2026.mkv"
        source.write_bytes(b"episode 17")
        season = self.index_files(2, {source: (17,)})
        season.episode_identity_conflicts = {
            self.plex_path(source): {"plex://episode/shared": (1, 2)},
        }
        bot._PLEX_POLLING_TASKS["task"] = None
        with self.assertLogs("tg_torrent_drop", level="ERROR") as logs:
            with self.assertRaisesRegex(PlexParseError, r"guid=plex://episode/shared seasons=\(1, 2\)"):
                self.poll(attempts=1)
        self.assertIn("will retry", "\n".join(logs.output))
        self.done.assert_not_called()
        self.assertNotIn("task", bot._PLEX_POLLING_TASKS)
        self.app.bot.send_message.assert_not_awaited()
        self.assertFalse(any(call.args[0] in {"plex_found", "plex_not_found"} for call in self.history.call_args_list))

        season.episode_identity_conflicts = {}
        task = {"id": "task", "title": self.title, "type": "bt", "status": "seeding", "size": 0}
        notified = {"task": {"status": "done", "sent": ["100"], "failures": {}}}
        ds = MagicMock()
        ds.list_tasks.return_value = [task]
        ds.get_task_info.return_value = None

        async def next_background_pass():
            await bot._run_task_notifications_once(self.app)
            await bot._PLEX_POLLING_TASKS["task"]

        with (
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATIONS_ENABLED", True),
            patch.object(bot, "TASK_NOTIFICATION_STATUSES", {"seeding"}),
            patch.object(bot, "TASK_NOTIFY_EXTERNAL_TASKS", True),
            patch.object(bot, "_load_task_owners", return_value={"task": 100}),
            patch.object(bot, "_load_notified_tasks", return_value=notified),
            patch.object(bot, "_save_notified_tasks"),
            patch.object(bot, "_get_task_meta", return_value=self.meta),
            patch.object(bot, "_inspect_completed_task_normalization", return_value={"status": "no_action"}),
        ):
            asyncio.run(next_background_pass())
        self.done.assert_called_once_with("task")
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn("добавлен в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_path_fallback_rejects_colliding_identity_only_for_expected_files(self):
        source = self.folder / "Oldschool.S02.E17.mkv"
        season = self.index_files(2, {source: (17,)})
        expected = (source.relative_to(self.storage).as_posix(),)
        season.episode_identity_conflicts = {
            self.plex_path(source): {"plex://episode/shared": (1, 2)},
        }
        with patch.object(bot, "_plex_show_find", return_value=None):
            with self.assertRaises(PlexParseError):
                asyncio.run(bot._plex_poll_lookup_target(self.title, self.meta, expected_files=expected))
            with self.assertRaises(PlexParseError):
                asyncio.run(bot._plex_poll_lookup_target(self.title, self.meta))
        season.episode_identity_conflicts = {"/video/unrelated.S02E16.mkv": {"plex://episode/other": (1, 2)}}
        self.assertTrue(bot._plex_season_matches_ds_title(season, self.title, expected_files=expected))

    def test_other_season_read_failure_defers_success_and_can_retry_with_complete_metadata(self):
        source = self.folder / "Oldschool.S02.E17.mkv"
        source.write_bytes(b"episode 17")
        self.index_files(2, {source: (17,)})
        seasons_xml = ElementTree.fromstring('<MediaContainer><Directory ratingKey="s1" index="1" leafCount="17"/><Directory ratingKey="s2" index="2" leafCount="17"/></MediaContainer>')
        old_xml = ElementTree.fromstring('<MediaContainer><Video index="17" guid="plex://episode/season1"><Media><Part file="/video/Oldschool/Oldschool.S01E17.mkv"/></Media></Video></MediaContainer>')
        new_xml = ElementTree.fromstring(f'<MediaContainer><Video index="17" guid="plex://episode/season2"><Media><Part file="{self.plex_path(source)}"/></Media></Video></MediaContainer>')
        parser = PlexClient("http://unit.invalid", "unit-token")
        self.client.get_show_seasons.side_effect = parser.get_show_seasons
        with patch.object(parser, "_get", side_effect=[seasons_xml, PlexTimeoutError("season 1 timeout"), seasons_xml, old_xml, new_xml]):
            with self.assertLogs("tg_torrent_drop", level="ERROR"):
                with self.assertRaisesRegex(PlexParseError, "identity check incomplete season=1"):
                    self.poll(attempts=1)
            self.done.assert_not_called()
            self.assertNotIn("task", bot._PLEX_POLLING_TASKS)
            self.app.bot.send_message.assert_not_awaited()
            self.assertFalse(any(call.args[0] in {"plex_found", "plex_not_found"} for call in self.history.call_args_list))
            self.poll(attempts=1)
        self.done.assert_called_once_with("task")
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn("добавлен в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_write_failure_defers_without_marking_done_and_can_retry(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        self.index_files(2, {source: (1,)})
        with patch.object(bot, "prepare_episode_match_hints", side_effect=PermissionError("read only")):
            self.poll()
        self.client.refresh_show_section.assert_not_called()
        self.refresh.assert_not_awaited()
        self.done.assert_not_called()
        buttons = [button for row in self.app.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == "task:norm_skip:task" for button in buttons))
        self.assertTrue(any(button.callback_data == "task:close:" for button in buttons))
        self.poll()
        self.done.assert_called_once()
        self.assertEqual(source.read_bytes(), b"episode")
        self.assertIn("добавлен в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_missing_storage_cannot_accept_old_season_by_folder_name(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        self.index_files(2, {source: (1,)})
        self.poll()
        self.client.refresh_show_section.assert_not_called()
        self.refresh.assert_not_awaited()
        self.done.assert_not_called()
        self.assertIn("Plex-проверка отложена", self.app.bot.send_message.await_args.kwargs["text"])

    def test_malformed_hint_encoding_is_a_controlled_retryable_error(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        (self.folder / ".plexmatch").write_bytes("ep: S02E01: x.mkv".encode("utf-16"))
        self.poll()
        self.done.assert_not_called()
        self.assertEqual(source.read_bytes(), b"episode")
        self.assertIn("Plex-проверка отложена", self.app.bot.send_message.await_args.kwargs["text"])

    def test_preparation_error_notification_is_not_repeated_while_waiting_for_fix(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        notified = {}
        with (
            patch.object(bot, "_load_notified_tasks", return_value=notified),
            patch.object(bot, "_save_notified_tasks"),
            patch.object(bot, "_mark_plex_poll_sent", self.mark_sent),
            patch.object(bot, "prepare_episode_match_hints", side_effect=bot.RenamePlanError("bad hints")),
        ):
            self.poll()
            self.poll()
        self.app.bot.send_message.assert_awaited_once()
        self.assertEqual(notified["task"]["plex_poll"]["normalization_failed"], ["100"])

    def test_scan_failure_is_retried_before_lookup(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        self.index_files(2, {source: (1,)})
        self.client.refresh_show_section.side_effect = [PlexTimeoutError("offline"), False, True]
        self.poll()
        self.assertEqual(self.client.refresh_show_section.call_count, 3)
        self.refresh.assert_awaited_once()
        self.assertIn("Сезон 2", self.app.bot.send_message.await_args.kwargs["text"])

    def test_unexpected_poll_error_keeps_retry_available(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        self.refresh.side_effect = RuntimeError("unexpected response")
        with self.assertRaises(RuntimeError):
            self.poll()
        self.done.assert_not_called()
        self.assertNotIn("task", bot._PLEX_POLLING_TASKS)

    def test_exhausted_scan_failures_do_not_permanently_complete_polling(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        self.client.refresh_show_section.return_value = False
        self.poll(attempts=1)
        self.done.assert_not_called()
        self.assertNotIn("task", bot._PLEX_POLLING_TASKS)
        self.refresh.assert_not_awaited()

    def test_poll_refetches_episode_assignments_even_if_show_list_refresh_keeps_old_cache(self):
        source = self.folder / "Oldschool.S02.E01.mkv"
        source.write_bytes(b"episode")
        self.index_files(2, {source: (2,)})
        fresh = PlexSeason("fresh", 2, 1, file_paths=[self.plex_path(source)],
                           episode_file_numbers={self.plex_path(source): (1,)})
        self.client.get_show_seasons.side_effect = None
        self.client.get_show_seasons.return_value = {2: fresh}
        self.poll(attempts=1)
        self.client.get_show_seasons.assert_called_once_with("show")
        self.assertIn("добавлен в Plex", self.app.bot.send_message.await_args.kwargs["text"])

    def test_series_fallback_skips_wrong_season(self):
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


class ManualRenameCompletionGuardTests(unittest.TestCase):
    def test_stale_apply_callback_does_not_rename_unfinished_task(self):
        for status in ("downloading", "paused", "waiting", "error"):
            with self.subTest(status=status):
                task = {"id": "task", "title": "Old series", "type": "bt", "status": status}
                query = MagicMock(data="task:norm_apply:task:1")
                query.edit_message_text = AsyncMock()
                context = MagicMock()
                with (
                    patch.object(bot, "_can_access_task_id", return_value=True),
                    patch.object(bot, "ds_client") as ds,
                    patch.object(bot, "_inspect_completed_task_normalization") as inspect,
                    patch.object(bot, "apply_rename_plan") as rename,
                    patch.object(bot, "_is_complete_despite_error", return_value=False),
                ):
                    ds.list_tasks.return_value = [task]
                    asyncio.run(bot._handle_normalization_callback(query, context, "norm_apply", "task:1", 100))
                inspect.assert_not_called()
                rename.assert_not_called()
                self.assertIn("после завершения", query.edit_message_text.await_args.args[0])

    def test_completed_task_still_physically_renames_and_starts_plex_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "01.mkv"
            target = root / "Season 01" / "Old series - S01E01.mkv"
            source.write_bytes(b"old series")
            plan = bot.RenamePlan("Old series", 1, root, target.parent, (
                RenameItem(source, target, 1, "Episode", 1, 1),
            ))
            task = {"id": "task", "title": "Old series", "type": "bt", "status": "finished"}
            query = MagicMock(data="task:norm_apply:task:1")
            query.edit_message_text = AsyncMock()
            context = MagicMock()
            with (
                patch.object(bot, "_can_access_task_id", return_value=True),
                patch.object(bot, "ds_client") as ds,
                patch.object(bot, "_get_task_meta", return_value={"kind": "series"}),
                patch.object(bot, "_inspect_completed_task_normalization", return_value={"plan": plan}),
                patch.object(bot, "_remember_normalized_task_meta"),
                patch.object(bot, "_record_download_history"),
                patch.object(bot, "_normalization_poll_recipients", return_value=[100]),
                patch.object(bot, "_start_plex_polling_for_completed_task", AsyncMock(return_value=True)) as poll,
            ):
                ds.list_tasks.return_value = [task]
                asyncio.run(bot._handle_normalization_callback(query, context, "norm_apply", "task:1", 100))
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"old series")
            poll.assert_awaited_once_with(context.application, task, [100], restart=True)

    def test_restart_after_rename_resets_only_plex_delivery_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonStateStore(**{
                name + "_file": root / (name + ".json") for name in (
                    "approved_chat_ids", "tracker_processed", "task_owners", "notified_tasks", "auto_delete_tasks",
                )
            })
            expected = {"status": "finished", "sent": ["200"], "failures": {"200": 1}, "subscribers": ["300"]}
            store.save_notified_tasks({"task": {**expected, "plex_done": True, "plex_poll": {"found": ["100"]}}})
            polling = {"task": None}
            poll = AsyncMock()
            with (
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "_PLEX_POLLING_TASKS", polling),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_get_task_meta", return_value={"kind": "series"}),
                patch.object(bot, "_plex_poll_after_finish", poll),
            ):
                async def run():
                    started = await bot._start_plex_polling_for_completed_task(
                        MagicMock(), {"id": "task", "title": "Old series"}, {100}, restart=True,
                    )
                    await polling["task"]
                    return started
                self.assertTrue(asyncio.run(run()))
            self.assertEqual(store.load_notified_tasks()["task"], expected)
            poll.assert_awaited_once()

    def test_restart_cancellation_cannot_race_with_automatic_poll_start(self):
        polling = {}
        poll = AsyncMock()
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_PLEX_POLLING_TASKS", polling),
            patch.object(bot, "_load_notified_tasks", return_value={}),
            patch.object(bot, "_get_task_meta", return_value={"kind": "series"}),
            patch.object(bot, "_plex_poll_after_finish", poll),
        ):
            async def run():
                cancelled = asyncio.Event()
                cleanup = asyncio.Event()
                async def previous_poll():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                        await cleanup.wait()
                        polling.pop("task", None)
                previous = asyncio.create_task(previous_poll())
                polling["task"] = previous
                await asyncio.sleep(0)
                restarting = asyncio.create_task(bot._start_plex_polling_for_completed_task(
                    MagicMock(), {"id": "task", "title": "Old series"}, {100}, restart=True,
                ))
                await cancelled.wait()
                async def automatic_start():
                    async with bot._task_notifications_lock_for_current_loop():
                        return "task" not in polling
                automatic = asyncio.create_task(automatic_start())
                await asyncio.sleep(0)
                self.assertFalse(automatic.done())
                cleanup.set()
                self.assertTrue(await restarting)
                self.assertFalse(await automatic)
                await polling["task"]
                self.assertTrue(previous.cancelled())
            asyncio.run(run())
        poll.assert_awaited_once()
