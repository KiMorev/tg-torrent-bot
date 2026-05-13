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
    TELEGRAM_ALLOWED_UPDATES,
    TASK_CARD_REFRESH_TASKS,
    _ACTIVE_STATUSES,
    _cancel_task_card_refresh,
    _check_jackett_sub_via_rutracker_direct,
    _extract_rutracker_topic_id,
    _is_admin_chat,
    _is_allowed,
    _format_movie_discovery_cache,
    _movie_discovery_keyboard,
    _notification_keyboard,
    _plural,
    _run_polling,
    _run_progress_panel_update_once,
    _start_task_card_refresh,
    _task_card_refresh_loop,
    access_callback,
    admin_callback,
    admin_command,
    help_command,
    movie_new_close_callback,
    help_close_callback,
    search_cancel,
    search_timeout,
    setup_bot_commands,
    sub_callback,
    status,
)
from rutracker import RutrackerError, RutrackerTopicUnavailable
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
    message.message_id = 42

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = message
    update.effective_message = message
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
        self.assertNotIn("/search", text)

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
        progress_message = MagicMock()
        progress_message.edit_text = AsyncMock()
        update.message.reply_text.return_value = progress_message

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

        update.message.reply_text.assert_called_once_with("🛠️ Обновляю админ-панель…")
        text = progress_message.edit_text.call_args.args[0]
        self.assertIn("Админ-панель", text)
        self.assertIn("📊 <b>Состояние</b>", text)
        self.assertIn("• Загрузки: 3 всего · 1 активных · 1 завершённых · 1 ошибок", text)
        self.assertIn("• Подписки: 2 всего · Rutracker 1 · Jackett 1", text)
        self.assertIn("⚙️ <b>Правила и интеграции</b>", text)
        self.assertIn("🟢 Rutracker", text)
        self.assertIn("🔴 Кинопоиск", text)
        self.assertIn("Живой статус сервисов — в разделе «Диагностика»", text)
        self.assertIn("🎬 <b>Новинки</b>", text)

    def test_admin_diagnostics_callback_reuses_diagnostics_view(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:diagnostics")
        context = _make_context()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_build_diagnostics_text", AsyncMock(return_value="diag text")),
        ):
            asyncio.run(admin_callback(update, context))

        update.callback_query.answer.assert_called_once()
        self.assertEqual(update.callback_query.edit_message_text.call_count, 2)
        self.assertEqual(update.callback_query.edit_message_text.call_args_list[0].args[0], "🧭 Проверяю сервисы…")
        self.assertEqual(update.callback_query.edit_message_text.call_args_list[1].args[0], "diag text")

    def test_admin_subscriptions_callback_shows_all_owners(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:subscriptions")
        context = _make_context()
        fake_store = MagicMock()
        fake_store.load_topic_subscriptions.return_value = {
            "123": {
                "chat_id": 100,
                "title": "Клиника / Scrubs / Сезон: 1",
                "last_episode_end": 8,
                "total_episodes": 10,
            },
            "jackett:abc": {
                "chat_id": 200,
                "type": "jackett",
                "query": "Some show",
                "last_check": "2026-05-12 08:00",
            },
        }
        fake_store.load_approved_users.return_value = {
            100: {"name": "Ivan"},
            200: {"name": "Petr"},
        }

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", fake_store),
        ):
            asyncio.run(admin_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Подписки", text)
        self.assertIn("Rutracker", text)
        self.assertIn("Jackett", text)
        self.assertIn("100 (Ivan)", text)
        self.assertIn("200 (Petr)", text)

    def test_admin_subscription_delete_refreshes_panel(self):
        update = _make_callback_update(chat_id=300, callback_data="sub:admin_unsub:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {"chat_id": 100, "title": "Клиника", "last_episode_end": 1, "total_episodes": 2},
                "jackett:abc": {"chat_id": 200, "type": "jackett", "query": "Film"},
            })
            with (
                patch.object(bot, "ADMIN_CHAT_IDS", {300}),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

            self.assertNotIn("123", store.load_topic_subscriptions())
            self.assertIn("jackett:abc", store.load_topic_subscriptions())

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Подписки", text)
        self.assertIn("Jackett", text)

    def test_non_owner_cannot_delete_subscription(self):
        update = _make_callback_update(chat_id=100, callback_data="sub:unsub:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {"chat_id": 200, "title": "Клиника", "last_episode_end": 1, "total_episodes": 2},
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100, 200}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

            self.assertIn("123", store.load_topic_subscriptions())

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("не относится", text)

    def test_access_remove_revokes_owned_tasks_and_subscriptions(self):
        update = _make_callback_update(chat_id=300, callback_data="access:remove:200")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.add_approved_user(200, "Petr")
            store.save_task_owners({"tid1": 200, "tid2": 100})
            store.save_topic_subscriptions({
                "123": {"chat_id": 200, "title": "Series", "last_episode_end": 1, "total_episodes": 2},
                "456": {"chat_id": 100, "title": "Other", "last_episode_end": 1, "total_episodes": 2},
            })

            with (
                patch.object(bot, "ADMIN_CHAT_IDS", {300}),
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(access_callback(update, context))

            self.assertNotIn(200, store.load_approved_chat_ids())
            self.assertEqual(store.load_task_owners(), {"tid2": 100})
            self.assertEqual(set(store.load_topic_subscriptions()), {"456"})

    def test_admin_close_callback_deletes_panel_message(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:close")
        context = _make_context()

        with patch.object(bot, "ADMIN_CHAT_IDS", {300}):
            asyncio.run(admin_callback(update, context))

        update.callback_query.answer.assert_called_once()
        update.callback_query.message.delete.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_called()

    def test_admin_close_callback_delete_failure_does_not_raise(self):
        """If the message cannot be deleted, close still completes without editing."""
        update = _make_callback_update(chat_id=300, callback_data="admin:close")
        update.callback_query.message.delete.side_effect = Exception("cannot delete")
        context = _make_context()

        with patch.object(bot, "ADMIN_CHAT_IDS", {300}):
            asyncio.run(admin_callback(update, context))

        update.callback_query.answer.assert_called_once()
        # Fallback is an auto-delete notification task — edit_message_text must NOT be called
        update.callback_query.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# movie discovery tests
# ---------------------------------------------------------------------------


class MovieDiscoveryHandlerTests(unittest.TestCase):
    def test_movie_discovery_keyboard_has_close_button(self):
        keyboard = _movie_discovery_keyboard([{"title": "Невеста!"}])
        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }

        self.assertEqual(buttons["🎬 1. Невеста!"], "new:show:0")
        self.assertEqual(buttons["✖️ Закрыть"], "new:close")

    def test_movie_discovery_text_lists_unique_tracker_abbreviations(self):
        text = _format_movie_discovery_cache({
            "updated_at": "2026-05-12 13:00",
            "cards": [{
                "title": "Невеста!",
                "year": 2026,
                "best_quality": "1080p",
                "best_size": "3 GB",
                "best_seeders": 10,
                "release_count": 3,
                "releases": [
                    {"source": "rutracker", "tracker": "rutracker"},
                    {"source": "jackett", "tracker": "rutracker"},
                    {"source": "jackett", "tracker": "nnmclub"},
                ],
            }],
        })

        self.assertIn("Раздач: 3 · RT, NNM", text)

    def test_movie_discovery_close_deletes_message(self):
        update = _make_callback_update(chat_id=100, callback_data="new:close")
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
        ):
            asyncio.run(movie_new_close_callback(update, context))

        update.callback_query.answer.assert_called_once()
        update.callback_query.message.delete.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_called()

    def test_movie_discovery_close_delete_failure_does_not_raise(self):
        """If the message cannot be deleted, close still completes without editing the message."""
        update = _make_callback_update(chat_id=100, callback_data="new:close")
        update.callback_query.message.delete.side_effect = Exception("cannot delete")
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
        ):
            asyncio.run(movie_new_close_callback(update, context))

        update.callback_query.answer.assert_called_once()
        # Fallback is an auto-delete notification task — edit_message_text must NOT be called
        update.callback_query.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# /help close button tests
# ---------------------------------------------------------------------------


class HelpCloseCallbackTests(unittest.TestCase):
    def test_close_deletes_message(self):
        update = _make_callback_update(chat_id=100, callback_data="help:close")
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
        ):
            asyncio.run(help_close_callback(update, context))

        update.callback_query.answer.assert_called_once()
        update.callback_query.message.delete.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_called()

    def test_close_delete_failure_does_not_raise(self):
        update = _make_callback_update(chat_id=100, callback_data="help:close")
        update.callback_query.message.delete.side_effect = Exception("cannot delete")
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
        ):
            asyncio.run(help_close_callback(update, context))

        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# notification keyboard tests
# ---------------------------------------------------------------------------


class NotificationKeyboardTests(unittest.TestCase):
    def _labels(self, keyboard):
        return [button.text for row in keyboard.inline_keyboard for button in row]

    def test_plex_button_appears_only_for_final_download_statuses(self):
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "PLEX_URL", "plex://"),
        ):
            finished_labels = self._labels(_notification_keyboard("tid1", "finished", "bt"))
            seeding_labels = self._labels(_notification_keyboard("tid1", "seeding", "bt"))
            error_labels = self._labels(_notification_keyboard("tid1", "error", "bt"))

        self.assertIn("▶️ Открыть Plex (iOS)", finished_labels)
        self.assertIn("▶️ Открыть Plex (iOS)", seeding_labels)
        self.assertNotIn("▶️ Открыть Plex (iOS)", error_labels)


# ---------------------------------------------------------------------------
# status command tests
# ---------------------------------------------------------------------------


class StatusCommandTests(unittest.TestCase):
    def test_status_replaces_progress_message_with_download_panel(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        progress_message = MagicMock()
        progress_message.message_id = 77
        progress_message.edit_text = AsyncMock()
        context.bot.send_message.return_value = progress_message

        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = [{"id": "tid1", "title": "Film", "status": "finished"}]
        bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", {100}),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "ds_client", fake_ds),
        ):
            asyncio.run(status(update, context))

        context.bot.send_message.assert_called_once_with(chat_id=100, text="📋 Получаю список загрузок…")
        self.assertIn("Film", progress_message.edit_text.call_args.args[0])
        self.assertEqual(bot.DOWNLOAD_PANEL_MESSAGES[100], 77)
        bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)

    def test_progress_panel_gets_final_update_when_last_task_finished(self):
        active_task = {
            "id": "t1", "status": "downloading", "title": "Film",
            "size": 1024, "type": "bt",
            "additional": {"transfer": {"size_downloaded": 512, "speed_download": 100}},
        }
        done_task = {
            "id": "t1", "status": "finished", "title": "Film",
            "size": 1024, "type": "bt", "additional": {"transfer": {}},
        }
        mock_ds = MagicMock()
        mock_ds.list_tasks.side_effect = [[active_task], [done_task], [done_task]]
        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        bot.DOWNLOAD_PANEL_MESSAGES[100] = 77
        bot.DOWNLOAD_PANEL_PAGES[100] = 0
        bot.DOWNLOAD_PANEL_SCOPES[100] = bot.TASK_LIST_SCOPE_ALL
        bot.DOWNLOAD_PANEL_HAD_ACTIVE[100] = True

        try:
            with (
                patch.object(bot, "ds_client", mock_ds),
                patch.object(bot, "ADMIN_CHAT_IDS", {100}),
            ):
                asyncio.run(_run_progress_panel_update_once(app))
                asyncio.run(_run_progress_panel_update_once(app))
                asyncio.run(_run_progress_panel_update_once(app))

            self.assertEqual(app.bot.edit_message_text.await_count, 2)
            self.assertFalse(bot.DOWNLOAD_PANEL_HAD_ACTIVE[100])
        finally:
            bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
            bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
            bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
            bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)


# ---------------------------------------------------------------------------
# search_cancel tests
# ---------------------------------------------------------------------------


class SearchCancelCallbackTests(unittest.TestCase):
    def test_callback_deletes_message(self):
        """Cancel via button must delete the search UI message, never edit it."""
        update = _make_callback_update()
        context = _make_context()
        asyncio.run(search_cancel(update, context))
        update.callback_query.message.delete.assert_called_once()
        update.callback_query.edit_message_text.assert_not_called()

    def test_with_photo_deletes_message(self):
        """Photo confirm card: the callback message is deleted; edit_message_text not called."""
        update = _make_callback_update()
        context = _make_context(user_data={
            "srch_confirm_has_photo": True,
            "srch_confirm_message_id": 42,
            "srch_confirm_chat_id": 100,
        })
        asyncio.run(search_cancel(update, context))
        update.callback_query.message.delete.assert_called_once()
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
    def test_does_not_use_reply_text(self):
        """Command cancel sends notification via bot.send_message (auto-delete task), not reply_text."""
        update = _make_command_update()
        context = _make_context()
        asyncio.run(search_cancel(update, context))
        update.message.reply_text.assert_not_called()

    def test_deletes_photo_message_when_present(self):
        update = _make_command_update()
        context = _make_context(user_data={
            "srch_confirm_has_photo": True,
            "srch_confirm_message_id": 77,
            "srch_confirm_chat_id": 100,
        })
        asyncio.run(search_cancel(update, context))
        # reply_text is NOT called; notification goes via auto-delete task
        update.message.reply_text.assert_not_called()
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
                patch.object(bot, "_can_access_task_id", return_value=True),
            ):
                await _task_card_refresh_loop(app, chat_id=1, message_id=2, task_id="t1")

        asyncio.run(run())
        return app

    def test_stops_when_task_is_finished(self):
        task = {"id": "t1", "status": "finished", "title": "F",
                "size": 0, "type": "bt", "additional": {"transfer": {}}}
        app = self._run_loop([[task]])
        app.bot.edit_message_text.assert_awaited_once()

    def test_stops_when_task_is_paused(self):
        task = {"id": "t1", "status": "paused", "title": "F",
                "size": 0, "type": "bt", "additional": {"transfer": {}}}
        app = self._run_loop([[task]])
        app.bot.edit_message_text.assert_awaited_once()

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
        self.assertEqual(app.bot.edit_message_text.await_count, 2)

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
                patch.object(bot, "_can_access_task_id", return_value=True),
            ):
                await _task_card_refresh_loop(app, chat_id=1, message_id=2, task_id="t1")
            self.assertNotIn((1, 2), TASK_CARD_REFRESH_TASKS)

        asyncio.run(run())

    def test_stops_when_access_is_revoked(self):
        mock_ds = MagicMock()
        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        async def run():
            with (
                patch.object(bot, "ds_client", mock_ds),
                patch.object(bot, "PROGRESS_UPDATE_INTERVAL_SECONDS", 0),
                patch.object(bot, "_can_access_task_id", return_value=False),
            ):
                await _task_card_refresh_loop(app, chat_id=1, message_id=2, task_id="t1")

        asyncio.run(run())
        mock_ds.list_tasks.assert_not_called()
        app.bot.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# Subscription loop startup check tests
# ---------------------------------------------------------------------------


class SubscriptionLoopStartupTests(unittest.TestCase):
    def test_run_polling_limits_allowed_updates(self):
        app = MagicMock()

        _run_polling(app)

        app.run_polling.assert_called_once_with(
            drop_pending_updates=True,
            allowed_updates=TELEGRAM_ALLOWED_UPDATES,
        )

    def test_setup_starts_subscription_loop_for_jackett_only_mode(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()

        def close_task(coro):
            coro.close()
            return MagicMock()

        app.create_task.side_effect = close_task

        async def run():
            with (
                patch.object(bot, "_cleanup_tmp_dir"),
                patch.object(bot, "_tracker_background_enabled", return_value=False),
                patch.object(bot, "_task_maintenance_enabled", return_value=False),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "RUTRACKER_ENABLED", False),
                patch.object(bot, "JACKETT_ENABLED", True),
                patch.object(bot, "rutracker_client", None),
                patch.object(bot, "jackett_client", object()),
                patch.object(bot, "MOVIE_DISCOVERY_ENABLED", False),
            ):
                await setup_bot_commands(app)

        asyncio.run(run())

        public_commands = app.bot.set_my_commands.await_args_list[-1].args[0]
        self.assertIn("subs", [command.command for command in public_commands])
        self.assertEqual(app.create_task.call_count, 2)

    def test_setup_starts_tracker_and_maintenance_loops_separately(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        created: list[str] = []

        def close_task(coro):
            created.append(coro.cr_code.co_name)
            coro.close()
            return MagicMock()

        app.create_task.side_effect = close_task

        async def run():
            with (
                patch.object(bot, "_cleanup_tmp_dir"),
                patch.object(bot, "_tracker_background_enabled", return_value=True),
                patch.object(bot, "_task_maintenance_enabled", return_value=True),
                patch.object(bot, "_subscription_monitor_enabled", return_value=False),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
            ):
                await setup_bot_commands(app)

        asyncio.run(run())

        self.assertIn("_tracker_background_loop", created)
        self.assertIn("_task_maintenance_loop", created)
        self.assertIn("_progress_update_loop", created)

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


class PluralTests(unittest.TestCase):
    """Tests for the Russian plural helper _plural()."""

    def test_one(self) -> None:
        self.assertEqual(_plural(1, "запись", "записи", "записей"), "запись")
        self.assertEqual(_plural(21, "запись", "записи", "записей"), "запись")
        self.assertEqual(_plural(101, "запись", "записи", "записей"), "запись")

    def test_few(self) -> None:
        self.assertEqual(_plural(2, "запись", "записи", "записей"), "записи")
        self.assertEqual(_plural(3, "запись", "записи", "записей"), "записи")
        self.assertEqual(_plural(4, "запись", "записи", "записей"), "записи")
        self.assertEqual(_plural(22, "запись", "записи", "записей"), "записи")

    def test_many(self) -> None:
        self.assertEqual(_plural(0, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(5, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(11, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(12, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(19, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(20, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(111, "запись", "записи", "записей"), "записей")
        self.assertEqual(_plural(156, "запись", "записи", "записей"), "записей")


class ExtractRutrackerTopicIdTests(unittest.TestCase):
    def test_standard_viewtopic_url(self) -> None:
        url = "https://rutracker.org/forum/viewtopic.php?t=1234567"
        self.assertEqual(_extract_rutracker_topic_id(url), "1234567")

    def test_rutracker_net_domain(self) -> None:
        url = "https://rutracker.net/forum/viewtopic.php?t=9876543"
        self.assertEqual(_extract_rutracker_topic_id(url), "9876543")

    def test_extra_query_params(self) -> None:
        url = "https://rutracker.org/forum/viewtopic.php?p=999&t=555&sid=abc"
        self.assertEqual(_extract_rutracker_topic_id(url), "555")

    def test_non_rutracker_url_returns_empty(self) -> None:
        self.assertEqual(_extract_rutracker_topic_id("https://nnmclub.to/forum/viewtopic.php?t=111"), "")

    def test_empty_url_returns_empty(self) -> None:
        self.assertEqual(_extract_rutracker_topic_id(""), "")

    def test_url_without_t_param_returns_empty(self) -> None:
        self.assertEqual(_extract_rutracker_topic_id("https://rutracker.org/forum/index.php"), "")


# ---------------------------------------------------------------------------
# _check_jackett_sub_via_rutracker_direct tests
# ---------------------------------------------------------------------------


class CheckJackettSubViaRutrackerDirectTests(unittest.TestCase):
    """Unit tests for the Rutracker-direct fast path in Jackett subscription checks."""

    def _make_app(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        return app

    def _make_sub(
        self,
        topic_url: str = "https://rutracker.org/forum/viewtopic.php?t=123",
        last_episode_end: int = 8,
        total_episodes: int = 10,
        season: int = 1,
        chat_id: int = 100,
    ) -> dict:
        return {
            "type": "jackett",
            "version": 2,
            "query": "Клиника 1080p",
            "tracker": "rutracker",
            "topic_url": topic_url,
            "title": "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]",
            "season": season,
            "last_episode_end": last_episode_end,
            "total_episodes": total_episodes,
            "chat_id": chat_id,
        }

    def test_returns_false_for_non_rutracker_url(self):
        """Non-Rutracker topic_url → return False (fall through to Jackett)."""
        sub = self._make_sub(topic_url="https://kinozal.tv/details.php?id=999")
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        result = asyncio.run(
            _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
        )

        self.assertFalse(result)
        app.bot.send_message.assert_not_called()

    def test_returns_false_when_rutracker_client_is_none(self):
        """If rutracker_client is None → return False (Rutracker not configured)."""
        sub = self._make_sub()
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        with patch.object(bot, "rutracker_client", None):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertFalse(result)
        app.bot.send_message.assert_not_called()

    def test_returns_true_and_marks_unavailable_on_topic_unavailable(self):
        """RutrackerTopicUnavailable → marks sub unavailable, returns True, notifies user."""
        sub = self._make_sub()
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        mock_rt.get_topic_title.side_effect = RutrackerTopicUnavailable("deleted")

        with patch.object(bot, "rutracker_client", mock_rt):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertTrue(result)
        self.assertIn("unavailable_at", sub)
        self.assertIn("unavailable_reason", sub)
        app.bot.send_message.assert_awaited_once()
        sent_text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("недоступна", sent_text)

    def test_returns_false_on_rutracker_error(self):
        """RutrackerError (network/auth) → returns False to fall through to Jackett."""
        sub = self._make_sub()
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        mock_rt.get_topic_title.side_effect = RutrackerError("timeout")

        with patch.object(bot, "rutracker_client", mock_rt):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertFalse(result)
        app.bot.send_message.assert_not_called()

    def test_returns_true_with_no_download_when_no_episode_progress(self):
        """Same or fewer episodes → return True, no download, no notification."""
        sub = self._make_sub(last_episode_end=8)
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        # Title with same episode count as stored
        mock_rt.get_topic_title.return_value = (
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]"
        )

        with patch.object(bot, "rutracker_client", mock_rt):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertTrue(result)
        app.bot.send_message.assert_not_called()
        # last_check should have been updated
        self.assertIn("last_check", sub)

    def test_returns_true_and_notifies_on_new_episodes(self):
        """New episode count → download attempted, notification sent, sub state updated."""
        sub = self._make_sub(last_episode_end=8, total_episodes=10)
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = (
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]"
        )
        mock_rt.download_torrent.return_value = b"fake-torrent-bytes"

        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "new_task_id"

        with (
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_remember_task_owner"),
        ):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertTrue(result)
        # Sub state should be updated
        self.assertEqual(sub["last_episode_end"], 9)
        self.assertEqual(sub["total_episodes"], 10)
        # Notification sent
        app.bot.send_message.assert_awaited_once()
        sent_text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("обновилась", sent_text)

    def test_season_complete_removes_subscription(self):
        """When last episode equals total, subscription is removed from subs dict."""
        sub = self._make_sub(last_episode_end=9, total_episodes=10)
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = (
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-10 из 10 [WEB-DL]"
        )
        mock_rt.download_torrent.return_value = b"fake-torrent-bytes"

        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "final_task_id"

        with (
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_remember_task_owner"),
        ):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertTrue(result)
        # Subscription should be removed once season is complete
        self.assertNotIn("jackett:aaa", subs)
        # Completion notification sent
        app.bot.send_message.assert_awaited_once()
        sent_text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("завершён", sent_text)

    def test_no_notification_for_missing_chat_id(self):
        """If sub has no chat_id, download still proceeds but send_message is skipped."""
        sub = self._make_sub(last_episode_end=8, chat_id=None)
        sub.pop("chat_id", None)
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = (
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]"
        )
        mock_rt.download_torrent.return_value = b"bytes"

        mock_ds = MagicMock()
        mock_ds.create_torrent_file.return_value = "t1"

        with (
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "_remember_task_owner"),
        ):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertTrue(result)
        app.bot.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
