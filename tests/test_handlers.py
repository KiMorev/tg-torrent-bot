"""Unit tests for bot.py handler logic.

Covers:
- Access-control helpers (_is_allowed, _is_admin_chat)
- search_cancel: photo-message cleanup on both callback and command paths
- search_timeout: silent photo deletion and user_data cleanup
- Task-card auto-refresh helpers (_cancel_task_card_refresh, _task_card_refresh_loop)
- Subscription loop: immediate check on startup, next-check timestamp tracking

Module-level singletons (ds_client, state_store, etc.) are patched per test
via unittest.mock.patch.object so the real services are never contacted.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Provide the minimum env vars that bot.py needs at import time.
os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from bot import (
    TASK_CARD_REFRESH_TASKS,
    _ACTIVE_STATUSES,
    _cancel_task_card_refresh,
    _is_admin_chat,
    _is_allowed,
    _start_task_card_refresh,
    _task_card_refresh_loop,
    admin_callback,
    admin_command,
    help_command,
    search_cancel,
    search_timeout,
)
from state_store import JsonStateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _make_callback_update(chat_id: int = 100, callback_data: str = "srch:cancel"):
    """Simulate an Update triggered by an InlineKeyboard button press."""
    msg = MagicMock()
    msg.chat_id = chat_id
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = 42
    msg.delete = AsyncMock()

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = msg

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query = query
    update.message = None
    return update


def _make_message_update(chat_id: int = 100):
    message = MagicMock()
    message.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = message
    return update


def _make_command_update(chat_id: int = 100, text: str = "/cancel"):
    """Simulate an Update triggered by a text command."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(user_data: dict | None = None):
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    return ctx


# ---------------------------------------------------------------------------
# Access-control tests
# ---------------------------------------------------------------------------


class IsAllowedTests(unittest.TestCase):
    def _update(self, chat_id: int):
        u = MagicMock()
        u.effective_chat = MagicMock()
        u.effective_chat.id = chat_id
        return u

    def test_configured_chat_id_is_allowed(self):
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set())
            )),
        ):
            self.assertTrue(_is_allowed(self._update(100)))

    def test_unknown_chat_id_is_not_allowed(self):
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set())
            )),
        ):
            self.assertFalse(_is_allowed(self._update(999)))

    def test_dynamically_approved_chat_id_is_allowed(self):
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value={555})
            )),
        ):
            self.assertTrue(_is_allowed(self._update(555)))

    def test_admin_chat_id_is_allowed(self):
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set())
            )),
        ):
            self.assertTrue(_is_allowed(self._update(300)))

    def test_is_admin_chat_true(self):
        with patch.object(bot, "ADMIN_CHAT_IDS", {300}):
            self.assertTrue(_is_admin_chat(300))

    def test_is_admin_chat_false(self):
        with patch.object(bot, "ADMIN_CHAT_IDS", {300}):
            self.assertFalse(_is_admin_chat(100))

    def test_is_admin_chat_none(self):
        with patch.object(bot, "ADMIN_CHAT_IDS", {300}):
            self.assertFalse(_is_admin_chat(None))


# ---------------------------------------------------------------------------
# help_command tests
# ---------------------------------------------------------------------------


class HelpCommandTests(unittest.TestCase):
    def test_help_mentions_jackett_only_search(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", False),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", False),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("сразу откроется поиск через Jackett", text)

    def test_help_mentions_admin_diagnostics_for_admins(self):
        update = _make_message_update(chat_id=300)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("/admin открывает админ-панель с диагностикой и главной сводкой", text)
        self.assertIn("/users управляет доступом пользователей", text)


# ---------------------------------------------------------------------------
# admin panel tests
# ---------------------------------------------------------------------------


class AdminPanelTests(unittest.TestCase):
    def test_admin_command_shows_summary_panel(self):
        update = _make_message_update(chat_id=300)
        context = _make_context()

        fake_store = MagicMock()
        fake_store.load_topic_subscriptions.return_value = {
            "123": {"chat_id": 300},
            "jk:abc": {"chat_id": 300, "type": "jackett"},
        }
        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = [
            {"id": "1", "status": "downloading"},
            {"id": "2", "status": "finished"},
            {"id": "3", "status": "error"},
        ]

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", fake_store),
            patch.object(bot, "ds_client", fake_ds),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", False),
            patch.object(bot, "PLEX_ENABLED", False),
        ):
            asyncio.run(admin_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Админ-панель", text)
        self.assertIn("Загрузки: всего 3, активных 1, завершённых 1, ошибок 1", text)
        self.assertIn("Подписки: 2 (Rutracker 1, Jackett 1)", text)
        self.assertIn("Сервисы:", text)
        update.message.reply_text.assert_called_once()

    def test_admin_diagnostics_callback_reuses_diagnostics_view(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:diagnostics")
        context = _make_context()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_build_diagnostics_text", AsyncMock(return_value="diag text")),
        ):
            asyncio.run(admin_callback(update, context))

        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_called_once()
        self.assertEqual(update.callback_query.edit_message_text.call_args.args[0], "diag text")


# ---------------------------------------------------------------------------
# search_cancel tests
# ---------------------------------------------------------------------------


class SearchCancelCallbackTests(unittest.TestCase):
    def test_no_photo_edits_message_text(self):
        update = _make_callback_update()
        context = _make_context()
        asyncio.run(search_cancel(update, context))
        update.callback_query.edit_message_text.assert_called_once_with("Поиск отменен.")
        update.callback_query.message.delete.assert_not_called()
        context.bot.send_message.assert_not_called()

    def test_with_photo_deletes_message_and_sends_text(self):
        update = _make_callback_update()
        context = _make_context(user_data={
            "srch_confirm_has_photo": True,
            "srch_confirm_message_id": 42,
            "srch_confirm_chat_id": 100,
        })
        asyncio.run(search_cancel(update, context))
        update.callback_query.message.delete.assert_called_once()
        context.bot.send_message.assert_called_once()
        # edit_message_text must NOT be called on a photo message
        update.callback_query.edit_message_text.assert_not_called()

    def test_clears_all_srch_keys(self):
        update = _make_callback_update()
        context = _make_context(user_data={
            "srch_query": "film",
            "srch_search_query": "film 1080p",
            "srch_settings": {},
            "srch_results": [{"title": "X"}],
            "srch_picked": 0,
            "srch_kp_info": {},
            "srch_results_page": 2,
            "srch_confirm_has_photo": False,
        })
        asyncio.run(search_cancel(update, context))
        for key in ("srch_query", "srch_search_query", "srch_settings",
                    "srch_results", "srch_picked", "srch_kp_info", "srch_results_page"):
            self.assertNotIn(key, context.user_data, f"key '{key}' was not cleared")


class SearchCancelCommandTests(unittest.TestCase):
    def test_replies_with_text(self):
        update = _make_command_update()
        context = _make_context()
        asyncio.run(search_cancel(update, context))
        update.message.reply_text.assert_called_once()

    def test_deletes_photo_message_when_present(self):
        update = _make_command_update()
        context = _make_context(user_data={
            "srch_confirm_has_photo": True,
            "srch_confirm_message_id": 77,
            "srch_confirm_chat_id": 100,
        })
        asyncio.run(search_cancel(update, context))
        update.message.reply_text.assert_called_once()
        context.bot.delete_message.assert_called_once_with(chat_id=100, message_id=77)

    def test_no_delete_when_no_photo(self):
        update = _make_command_update()
        context = _make_context()
        asyncio.run(search_cancel(update, context))
        context.bot.delete_message.assert_not_called()


# ---------------------------------------------------------------------------
# search_timeout tests
# ---------------------------------------------------------------------------


class SearchTimeoutTests(unittest.TestCase):
    def test_deletes_photo_message_silently(self):
        update = MagicMock()
        context = _make_context(user_data={
            "srch_confirm_has_photo": True,
            "srch_confirm_message_id": 55,
            "srch_confirm_chat_id": 100,
            "srch_query": "film",
        })
        asyncio.run(search_timeout(update, context))
        context.bot.delete_message.assert_called_once_with(chat_id=100, message_id=55)
        self.assertNotIn("srch_query", context.user_data)

    def test_no_delete_without_photo(self):
        update = MagicMock()
        context = _make_context(user_data={"srch_query": "film", "srch_results_page": 1})
        asyncio.run(search_timeout(update, context))
        context.bot.delete_message.assert_not_called()
        self.assertNotIn("srch_query", context.user_data)
        self.assertNotIn("srch_results_page", context.user_data)

    def test_clears_all_srch_keys(self):
        update = MagicMock()
        context = _make_context(user_data={
            "srch_query": "q",
            "srch_search_query": "q 1080p",
            "srch_results": [{}],
            "srch_results_page": 1,
            "srch_picked": 0,
        })
        asyncio.run(search_timeout(update, context))
        for key in ("srch_query", "srch_search_query", "srch_results", "srch_results_page", "srch_picked"):
            self.assertNotIn(key, context.user_data)


# ---------------------------------------------------------------------------
# Task-card auto-refresh tests
# ---------------------------------------------------------------------------


class CancelTaskCardRefreshTests(unittest.TestCase):
    def setUp(self):
        TASK_CARD_REFRESH_TASKS.clear()

    def test_cancel_nonexistent_key_does_not_raise(self):
        _cancel_task_card_refresh(chat_id=1, message_id=999)  # must not raise

    def test_cancel_stops_running_task(self):
        async def long_sleep():
            await asyncio.sleep(9999)

        async def run():
            t = asyncio.create_task(long_sleep())
            TASK_CARD_REFRESH_TASKS[(1, 2)] = t
            _cancel_task_card_refresh(1, 2)
            self.assertNotIn((1, 2), TASK_CARD_REFRESH_TASKS)
            # Yield to the event loop so the CancelledError is delivered to the task.
            await asyncio.sleep(0)
            self.assertTrue(t.cancelled() or t.done())

        asyncio.run(run())


class TaskCardRefreshLoopTests(unittest.TestCase):
    def setUp(self):
        TASK_CARD_REFRESH_TASKS.clear()

    def _run_loop(self, list_tasks_side_effect):
        mock_ds = MagicMock()
        mock_ds.list_tasks.side_effect = list_tasks_side_effect
        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        async def run():
            with (
                patch.object(bot, "ds_client", mock_ds),
                patch.object(bot, "PROGRESS_UPDATE_INTERVAL_SECONDS", 0),
            ):
                await _task_card_refresh_loop(app, chat_id=1, message_id=2, task_id="t1")

        asyncio.run(run())
        return app

    def test_stops_when_task_is_finished(self):
        task = {"id": "t1", "status": "finished", "title": "F",
                "size": 0, "type": "bt", "additional": {"transfer": {}}}
        app = self._run_loop([[task]])
        app.bot.edit_message_text.assert_not_called()

    def test_stops_when_task_is_paused(self):
        task = {"id": "t1", "status": "paused", "title": "F",
                "size": 0, "type": "bt", "additional": {"transfer": {}}}
        app = self._run_loop([[task]])
        app.bot.edit_message_text.assert_not_called()

    def test_stops_when_task_disappears(self):
        # DS returns empty list — task was deleted
        app = self._run_loop([[]])
        app.bot.edit_message_text.assert_not_called()

    def test_edits_message_for_active_task_then_stops(self):
        task_active = {
            "id": "t1", "status": "downloading", "title": "Film",
            "size": 1024, "type": "bt",
            "additional": {"transfer": {"size_downloaded": 512, "speed_download": 100}},
        }
        task_done = {
            "id": "t1", "status": "finished", "title": "Film",
            "size": 1024, "type": "bt", "additional": {"transfer": {}},
        }
        # First poll: active → edit; second poll: finished → stop
        app = self._run_loop([[task_active], [task_done]])
        app.bot.edit_message_text.assert_called_once()

    def test_cleanup_removes_key_from_dict(self):
        task = {"id": "t1", "status": "finished", "title": "F",
                "size": 0, "type": "bt", "additional": {"transfer": {}}}
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = [task]
        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        async def run():
            TASK_CARD_REFRESH_TASKS[(1, 2)] = asyncio.current_task()  # fake entry
            with (
                patch.object(bot, "ds_client", mock_ds),
                patch.object(bot, "PROGRESS_UPDATE_INTERVAL_SECONDS", 0),
            ):
                await _task_card_refresh_loop(app, chat_id=1, message_id=2, task_id="t1")
            self.assertNotIn((1, 2), TASK_CARD_REFRESH_TASKS)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Subscription loop startup check tests
# ---------------------------------------------------------------------------


class SubscriptionLoopStartupTests(unittest.TestCase):
    def test_check_runs_immediately_before_first_sleep(self):
        """_subscription_check_loop must call _check_subscriptions at startup,
        not only after the first interval has elapsed."""
        calls: list[str] = []

        async def fake_check(app):
            calls.append("check")

        async def run():
            with (
                patch.object(bot, "_check_subscriptions", fake_check),
                patch.object(bot, "SUBSCRIPTION_CHECK_INTERVAL_HOURS", 10000),
            ):
                task = asyncio.create_task(bot._subscription_check_loop(MagicMock()))
                # Give the event loop a couple of ticks to execute the immediate check
                for _ in range(5):
                    await asyncio.sleep(0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())
        self.assertIn("check", calls, "_check_subscriptions was not called on startup")


if __name__ == "__main__":
    unittest.main()
