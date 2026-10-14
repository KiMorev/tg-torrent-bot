"""Plex rules must be ready before any torrent creation route starts downloading."""

import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from jackett import JackettError, JackettResult


class TorrentPlexPreparationTests(unittest.TestCase):
    def test_rules_exist_before_download_station_creates_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "video"
            storage.mkdir()
            folder = storage / "Oldschool.S02"
            name = b"Oldschool.S02.E01.2026.mkv"
            torrent = Path(tmp) / "show.torrent"
            torrent.write_bytes(
                b"d4:infod4:name13:Oldschool.S025:filesld6:lengthi10e4:pathl"
                + str(len(name)).encode() + b":" + name + b"eeeee"
            )
            ds = MagicMock()

            def create(file_path, filename):
                self.assertIn("pattern: *.S{se}.E{ep}.*", (folder / ".plexmatch").read_text())
                self.assertFalse((folder / name.decode()).exists())
                return "task-1"

            ds.create_torrent_file.side_effect = create
            with (
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "STORAGE_MOUNT_PATH", str(storage)),
                patch.object(bot, "ds_client", ds),
            ):
                self.assertEqual(bot._create_plex_prepared_torrent_task(torrent, "show.torrent"), "task-1")

    def test_disabled_plex_does_not_prepare_rules(self):
        ds = MagicMock()
        ds.create_torrent_file.return_value = "task-1"
        with (
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "prepare_torrent_plex_files") as prepare,
            patch.object(bot, "ds_client", ds),
        ):
            self.assertEqual(bot._create_plex_prepared_torrent_task(Path("file.torrent"), "file.torrent"), "task-1")
        prepare.assert_not_called()
        ds.create_torrent_file.assert_called_once()

    def test_preparation_errors_stop_creation_and_preserve_oserror_cause(self):
        for error in (bot.RenamePlanError("conflict"), PermissionError("read only")):
            with self.subTest(error=type(error).__name__):
                ds = MagicMock()
                with (
                    patch.object(bot, "PLEX_ENABLED", True),
                    patch.object(bot, "prepare_torrent_plex_files", side_effect=error),
                    patch.object(bot, "ds_client", ds),
                ):
                    with self.assertRaises(bot.RenamePlanError) as caught:
                        bot._create_plex_prepared_torrent_task(Path("file.torrent"), "file.torrent")
                ds.create_torrent_file.assert_not_called()
                if isinstance(error, OSError):
                    self.assertIs(caught.exception.__cause__, error)


class TorrentCreationRoutesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ds = MagicMock()
        self.ds.list_tasks.return_value = []
        self.jackett = MagicMock()
        self.jackett._api_key = "test-key"
        self.jackett.download_torrent.return_value = b"torrent metadata"
        self.rt = MagicMock()
        self.rt.download_torrent.return_value = b"torrent metadata"
        self.events = []

        def prepare(path, storage):
            self.assertEqual(path.read_bytes(), b"torrent metadata")
            self.events.append("prepare")
            return 1

        def create(path, filename):
            self.assertEqual(self.events[-1], "prepare")
            self.events.append("create")
            return "task-1"

        self.ds.create_torrent_file.side_effect = create
        self.prepare = self.stack.enter_context(patch.object(bot, "prepare_torrent_plex_files", side_effect=prepare))
        for name, value in (
            ("PLEX_ENABLED", True), ("TMP_DIR", Path(self.tmp.name)),
            ("ds_client", self.ds), ("jackett_client", self.jackett), ("rutracker_client", self.rt),
        ):
            self.stack.enter_context(patch.object(bot, name, value))
        for name in (
            "_remember_task_owner", "_remember_task_meta", "_record_download_added_history",
            "_record_download_added_from_title_history", "_record_download_history",
            "_schedule_task_notification_fast_wake", "_register_task_card_from_query",
            "_register_task_card_from_message", "_start_task_card_refresh", "_mark_tracker_processed_if_final",
        ):
            self.stack.enter_context(patch.object(bot, name))
        self.stack.enter_context(patch.object(bot, "_check_disk_space_for_download", return_value=None))
        self.stack.enter_context(patch.object(bot, "_torrent_file_is_private", return_value=True))

    def _result(self, source="jackett"):
        return {
            "title": "Oldschool Сезон 2", "source": source, "topic_id": "12345",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "torrent_url": "http://jackett/torrent" if source == "jackett" else "",
            "magnet_url": "magnet:?xt=urn:btih:test", "tracker_name": "rutracker",
        }

    def _query_and_context(self, result):
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        query.message.chat.id = 100
        query.message.chat_id = 100
        query.message.message_id = 42
        context = MagicMock()
        context.user_data = {"srch_results": [result]}
        return query, context

    async def test_search_and_its_reserve_paths_prepare_before_creation(self):
        for route in ("jackett", "rutracker", "rutracker_fallback", "fresh_jackett"):
            with self.subTest(route=route):
                self.events.clear()
                self.jackett.download_torrent.side_effect = None
                result = self._result("rutracker" if route == "rutracker" else "jackett")
                if route == "rutracker_fallback":
                    self.jackett.download_torrent.side_effect = JackettError("HTTP 404")
                elif route == "fresh_jackett":
                    result["tracker_name"] = "public"
                    result["url"] = "https://example.test/topic"
                    self.jackett.download_torrent.side_effect = [JackettError("HTTP 404"), b"torrent metadata"]
                query, context = self._query_and_context(result)
                with patch.object(bot, "_refresh_jackett_torrent_url", AsyncMock(return_value="http://jackett/fresh")):
                    await bot._download_and_add(query, context, 0, _skip_plex_check=True)
                self.assertEqual(self.events, ["prepare", "create"])
                self.ds.create_magnet.assert_not_called()

    async def test_pending_and_bulk_delivery_paths_prepare_before_creation(self):
        for route in ("jackett", "rutracker", "rutracker_fallback"):
            with self.subTest(route=route):
                self.events.clear()
                self.jackett.download_torrent.side_effect = JackettError("HTTP 404") if route == "rutracker_fallback" else None
                entry = {
                    "title": "Oldschool Сезон 2", "tracker": "rutracker",
                    "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                    "torrent_url": "" if route == "rutracker" else "http://jackett/torrent",
                    "magnet_url": "magnet:?xt=urn:btih:test",
                }
                task_id, method = await bot._attempt_pending_download(entry)
                self.assertEqual(task_id, "task-1")
                self.assertEqual(self.events, ["prepare", "create"])
                self.ds.create_magnet.assert_not_called()

    async def test_subscription_prepares_before_creation(self):
        candidate = JackettResult(
            title="Oldschool S2E1-15 of 17", size="10 GB", seeders=10,
            torrent_url="http://jackett/torrent", magnet_url="magnet:?xt=urn:btih:test",
            tracker="rutracker", topic_url="https://rutracker.org/forum/viewtopic.php?t=12345",
        )
        self.assertEqual(await bot._jackett_subscription_auto_download(candidate, 100), "task-1")
        self.assertEqual(self.events, ["prepare", "create"])

    async def test_subscription_preparation_failure_does_not_fall_back_to_magnet(self):
        candidate = JackettResult(
            title="Oldschool S2E1-15 of 17", size="10 GB", seeders=10,
            torrent_url="http://jackett/torrent", magnet_url="magnet:?xt=urn:btih:test",
            tracker="rutracker", topic_url="https://rutracker.org/forum/viewtopic.php?t=12345",
        )
        self.prepare.side_effect = PermissionError("read only")
        with self.assertRaises(bot.RenamePlanError):
            await bot._jackett_subscription_auto_download(candidate, 100)
        self.ds.create_torrent_file.assert_not_called()
        self.ds.create_magnet.assert_not_called()

    async def test_search_preparation_failure_keeps_retry_and_does_not_fall_back(self):
        self.prepare.side_effect = PermissionError("private storage path")
        query, context = self._query_and_context(self._result())
        with patch.object(bot, "_refresh_jackett_torrent_url", AsyncMock()) as refresh:
            state = await bot._download_and_add(query, context, 0, _skip_plex_check=True)
        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.ds.create_torrent_file.assert_not_called()
        self.ds.create_magnet.assert_not_called()
        self.rt.download_torrent.assert_not_called()
        refresh.assert_not_awaited()
        final = query.edit_message_text.await_args
        self.assertIn("Загрузка не началась", final.args[0])
        self.assertNotIn("private storage path", final.args[0])
        callbacks = [b.callback_data for row in final.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("srch:retry_dl:0", callbacks)
        self.assertIn("task:close:", callbacks)
        self.assertNotIn("srch:queue_dl:0", callbacks)

    async def test_pending_failure_does_not_fall_back_to_magnet(self):
        self.prepare.side_effect = PermissionError("read only")
        with self.assertRaises(bot.RenamePlanError):
            await bot._attempt_pending_download({
                "title": "Oldschool", "torrent_url": "http://jackett/torrent",
                "tracker": "rutracker", "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                "magnet_url": "magnet:?xt=urn:btih:test",
            })
        self.ds.create_torrent_file.assert_not_called()
        self.ds.create_magnet.assert_not_called()
        self.rt.download_torrent.assert_not_called()

    async def test_uploaded_torrent_prepares_before_creation(self):
        torrent = Path(self.tmp.name) / "upload.torrent"
        torrent.write_bytes(b"torrent metadata")
        message = MagicMock()
        message.edit_text = AsyncMock()
        context = MagicMock()
        with patch.object(bot, "_build_task_meta_from_title_with_gpt", AsyncMock(return_value={})):
            await bot._do_process_torrent(message, context, torrent, "upload.torrent", chat_id=100)
        self.assertEqual(self.events, ["prepare", "create"])
        self.assertFalse(torrent.exists())

    async def test_uploaded_torrent_can_retry_preparation_without_reupload(self):
        torrent = Path(self.tmp.name) / "upload.torrent"
        torrent.write_bytes(b"torrent metadata")
        message = MagicMock()
        message.edit_text = AsyncMock()
        message.message_id = 42
        message.chat.id = 100
        message.chat_id = 100
        query = MagicMock()
        query.answer = AsyncMock()
        query.message = message
        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {}
        self.prepare.side_effect = PermissionError("read only")
        with (
            patch.object(bot, "_build_task_meta_from_title_with_gpt", AsyncMock(return_value={})),
            patch.object(bot, "_schedule_task_notification_fast_wake") as wake,
        ):
            await bot._do_process_torrent(message, context, torrent, "upload.torrent", chat_id=100)
            self.ds.create_torrent_file.assert_not_called()
            wake.assert_not_called()
            self.assertTrue(torrent.exists())
            callbacks = [b.callback_data for row in message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
            self.assertEqual(callbacks, ["plex:confirm", "task:close:"])
            self.assertEqual(context.user_data["plex_pending"]["retry_message_id"], 42)

            self.prepare.side_effect = None
            self.prepare.return_value = 0
            self.ds.create_torrent_file.side_effect = None
            self.ds.create_torrent_file.return_value = "task-1"
            await bot.plex_confirm_standalone(update, context)

            self.ds.create_torrent_file.assert_called_once()
            wake.assert_called_once()
        self.assertNotIn("plex_pending", context.user_data)
        self.assertFalse(torrent.exists())

    async def test_close_cleans_only_the_torrent_retry_for_that_message(self):
        for message_id in (99, 42):
            with self.subTest(message_id=message_id):
                torrent = Path(self.tmp.name) / "retry.torrent"
                torrent.write_bytes(b"torrent metadata")
                context = MagicMock()
                context.user_data = {"plex_pending": {
                    "type": "torrent", "temp_path": str(torrent), "safe_name": "retry.torrent",
                    "retry_message_id": 42,
                }}
                query = MagicMock()
                query.answer = AsyncMock()
                query.data = "task:close:"
                query.message.message_id = message_id
                query.message.chat.id = 100
                query.message.delete = AsyncMock()
                update = MagicMock()
                update.callback_query = query
                with (
                    patch.object(bot, "_is_allowed", return_value=True),
                    patch.object(bot, "_send_auto_delete", AsyncMock()),
                ):
                    await bot.task_callback(update, context)
                self.assertEqual(torrent.exists(), message_id != 42)
                self.assertEqual("plex_pending" in context.user_data, message_id != 42)

    async def test_old_upload_retry_cannot_start_a_newer_pending_torrent(self):
        context = MagicMock()
        context.user_data = {}
        self.prepare.side_effect = PermissionError("read only")
        messages = []
        paths = []
        for message_id in (42, 43):
            path = Path(self.tmp.name) / f"upload-{message_id}.torrent"
            path.write_bytes(b"torrent metadata")
            message = MagicMock()
            message.message_id = message_id
            message.chat.id = 100
            message.chat_id = 100
            message.edit_text = AsyncMock()
            await bot._do_process_torrent(message, context, path, path.name, chat_id=100)
            paths.append(path)
            messages.append(message)
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())
        query = MagicMock()
        query.answer = AsyncMock()
        query.message = messages[0]
        update = MagicMock()
        update.callback_query = query
        await bot.plex_confirm_standalone(update, context)
        query.answer.assert_awaited_once_with(
            "Это сообщение устарело. Повторите в последнем сообщении.", show_alert=True,
        )
        self.assertEqual(context.user_data["plex_pending"]["temp_path"], str(paths[1]))
        self.ds.create_torrent_file.assert_not_called()

        query.message = messages[1]
        self.prepare.side_effect = None
        self.prepare.return_value = 0
        self.ds.create_torrent_file.side_effect = None
        self.ds.create_torrent_file.return_value = "task-1"
        with patch.object(bot, "_build_task_meta_from_title_with_gpt", AsyncMock(return_value={})):
            await bot.plex_confirm_standalone(update, context)
        self.ds.create_torrent_file.assert_called_once_with(paths[1], paths[1].name)
        self.assertFalse(paths[1].exists())
        self.assertNotIn("plex_pending", context.user_data)
