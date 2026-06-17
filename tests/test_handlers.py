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
from contextlib import contextmanager
from datetime import datetime
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
    _enrich_cards_with_plex,
    _format_kp_votes,
    _format_movie_discovery_cache,
    _get_movie_subscriptions,
    _is_movie_subscribed,
    _set_movie_subscription,
    _run_movie_discovery_notifications,
    _is_in_notification_window,
    _plex_find_by_ds_title,
    _plex_is_series,
    _plex_poll_after_finish,
    _plex_pre_check,
    _plex_confirm_text,
    _plex_quality_from_title,
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
    start,
    _reply_access_pending,
    movie_new_close_callback,
    movie_new_command,
    movie_new_refresh_callback,
    help_close_callback,
    search_cancel,
    search_timeout,
    setup_bot_commands,
    subs_command,
    sub_callback,
    status,
    text_message_entry,
    TASK_CARD_MESSAGES,
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
        download_history_file=d / "download_history.jsonl",
        jackett_guard_file=d / "jackett_guard.json",
        youtube_downloads_file=d / "youtube_downloads.json",
        youtube_plex_refresh_file=d / "youtube_plex_refresh_pending.json",
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
    message.chat = MagicMock()
    message.chat.id = chat_id

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
    update.message.message_id = 42
    update.message.chat = MagicMock()
    update.message.chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    return update


def _make_context(user_data: dict | None = None):
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    return ctx


# ---------------------------------------------------------------------------
# Access-control tests
# ---------------------------------------------------------------------------


class SafeEditMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot._TELEGRAM_EDIT_GENERATIONS.clear()

    async def asyncTearDown(self):
        bot._TELEGRAM_EDIT_GENERATIONS.clear()

    async def test_safe_edit_message_retries_network_error(self):
        message = MagicMock()
        message.chat_id = 100
        message.message_id = 10
        message.edit_text = AsyncMock(side_effect=[bot.NetworkError("temporary"), None])
        sleep = AsyncMock()

        with patch.object(bot.asyncio, "sleep", sleep):
            result = await bot._safe_edit_message(message, "Готово")

        self.assertTrue(result)
        self.assertEqual(message.edit_text.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_safe_edit_callback_retries_network_error(self):
        query = MagicMock()
        query.message.chat_id = 100
        query.message.message_id = 11
        query.edit_message_text = AsyncMock(side_effect=[bot.TimedOut("temporary"), None])
        sleep = AsyncMock()

        with patch.object(bot.asyncio, "sleep", sleep):
            result = await bot._safe_edit_callback(query, "Готово")

        self.assertTrue(result)
        self.assertEqual(query.edit_message_text.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_safe_edit_message_skips_stale_retry(self):
        message = MagicMock()
        message.chat_id = 100
        message.message_id = 12
        message.edit_text = AsyncMock(side_effect=bot.NetworkError("temporary"))

        async def mark_stale(_delay):
            bot._TELEGRAM_EDIT_GENERATIONS[(100, 12)] += 1

        with patch.object(bot.asyncio, "sleep", AsyncMock(side_effect=mark_stale)):
            result = await bot._safe_edit_message(message, "Устаревший экран")

        self.assertFalse(result)
        self.assertEqual(message.edit_text.await_count, 1)


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
    def test_help_shows_search_bullet_when_only_jackett_configured(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", False),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", False),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        # Free-text search bullet is present (Jackett alone counts as a search source).
        self.assertIn("Пришлите название фильма/сериала", text)
        # Without KP API key we don't suggest pasting Kinopoisk links.
        self.assertNotIn("ссылку с Кинопоиска", text)
        # Legacy "сразу откроется поиск" framing is gone.
        self.assertNotIn("сразу откроется поиск", text)

    def test_help_mentions_admin_commands_for_admins(self):
        update = _make_message_update(chat_id=300)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        # Admin-only commands appear in the «Служебное» section.
        self.assertIn("/admin", text)
        self.assertIn("/users", text)
        # /status text varies between admin (мои/все) and non-admin (ваши).
        self.assertIn("переключатель «мои / все»", text)

    def test_help_priority_order_search_then_new(self):
        """Same ordering principle as /start: free-text search before /new."""
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertLess(text.index("Пришлите название"), text.index("/new"))
        # Plex push bullet is shown when PLEX_ENABLED.
        self.assertIn("Plex", text)
        # Subscription bullets appear when search sources are configured.
        self.assertIn("Подписаться на новые серии", text)

    def test_help_mentions_current_settings_subs_and_new_push_flow(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("/settings", text)
        self.assertIn("качество, Original, субтитры и озвучка", text)
        self.assertIn("/subs", text)
        self.assertIn("правила уведомлений/скачивания", text)
        self.assertIn("1-3 новинки", text)
        self.assertIn("постером", text)
        self.assertIn("ссылкой на КП", text)
        self.assertIn("быстрыми кнопками скачивания", text)

    def test_help_for_regular_user_uses_download_queue_wording(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", False),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", False),
            patch.object(bot, "PLEX_ENABLED", False),
        ):
            asyncio.run(help_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("очередь загрузок", text)
        self.assertNotIn("Download Station", text)

    def test_help_mentions_voice_search_when_enabled(self):
        """Voice-search line surfaces only when OPENAI_API_KEY is configured."""
        update = _make_message_update(chat_id=100)
        context = _make_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "VOICE_SEARCH_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))
        text = update.message.reply_text.call_args.args[0]
        # Voice search bullet present (use emoji to avoid false positive on the
        # word «голос» appearing in unrelated context).
        self.assertIn("🎙", text)

    def test_help_omits_voice_when_disabled(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "VOICE_SEARCH_ENABLED", False),
        ):
            asyncio.run(help_command(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertNotIn("🎙", text)

    def test_help_mentions_partial_series_download_and_notify_actions(self):
        """Help text describes separate download and notification actions."""
        update = _make_message_update(chat_id=100)
        context = _make_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "VOICE_SEARCH_ENABLED", True),
        ):
            asyncio.run(help_command(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("⬇️", text)
        self.assertIn("🔔", text)
        self.assertIn("финал", text.lower())  # final-only option exists in help
        self.assertIn("уведомлен", text.lower())
        self.assertIn("скачиван", text.lower())


class StartCommandTests(unittest.TestCase):
    """Welcome messages: authenticated /start + access-pending response.

    These exist to lock in the post-rewrite centre of gravity (movie
    discovery + Plex + auto-notifications) and to ensure regressions don't
    silently revert to the legacy "send a .torrent or magnet" wording.
    """

    def test_start_for_approved_user_prioritises_search_and_mentions_help(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
        ):
            asyncio.run(start(update, context))

        text = update.message.reply_text.call_args.args[0]
        # Centre of gravity: /new comes first.
        self.assertIn("/new", text)
        # /help is referenced at the bottom — confirms we didn't drop it again.
        self.assertIn("/help", text)
        # Legacy framing must be gone: bot should not lead with magnet/.torrent talk.
        self.assertNotIn(".torrent файлом", text)
        self.assertNotIn("magnet-ссылку сообщением", text)
        # /status still mentioned as one of the entry points.
        self.assertIn("/status", text)
        # Order check: free-text search bullet comes BEFORE /new
        # (user prefers active "i know what i want" framing over discovery).
        self.assertLess(text.index("Пришлите название"), text.index("/new"))

    def test_start_omits_search_bullet_when_no_search_sources(self):
        """If neither Rutracker nor Jackett is configured, the search bullet
        and the /new bullet (which depends on search sources) both disappear."""
        update = _make_message_update(chat_id=100)
        context = _make_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "RUTRACKER_ENABLED", False),
            patch.object(bot, "JACKETT_ENABLED", False),
            patch.object(bot, "KINOPOISK_ENABLED", False),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
        ):
            asyncio.run(start(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertNotIn("Пришлите название", text)
        # /new also gone — relies on search sources for the actual download step.
        self.assertNotIn("/new", text)
        # /status survives as a baseline entry point.
        self.assertIn("/status", text)

    def test_access_pending_introduces_bot_and_lists_value_props(self):
        update = _make_message_update(chat_id=999)
        context = _make_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "RUTRACKER_ENABLED", True),
            patch.object(bot, "JACKETT_ENABLED", True),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_send_access_request_to_admins",
                        AsyncMock(return_value=False)),
        ):
            asyncio.run(_reply_access_pending(update, context))

        text = update.effective_message.reply_text.call_args.args[0]
        # Brand mention — replaces the old terse "Доступ пока не настроен" only message.
        self.assertIn("PlexLoader", text)
        # User's chat_id is still shown (admin needs it).
        self.assertIn("999", text)
        # Tail string for the "couldn't reach admin" branch.
        self.assertIn("Передайте этот chat_id администратору", text)
        # At least one value-prop bullet (with emoji prefix) is present.
        self.assertTrue(
            any(marker in text for marker in ("🎬", "🔍", "▶️", "🔔")),
            "expected at least one value-prop bullet",
        )


# ---------------------------------------------------------------------------
# admin panel tests
# ---------------------------------------------------------------------------


class JackettWarmupTests(unittest.IsolatedAsyncioTestCase):
    def test_next_batch_rotates_indexers(self):
        with (
            patch.object(bot, "_jackett_warmup_cursor", 0),
            patch.object(bot, "JACKETT_WARMUP_BATCH_SIZE", 2),
        ):
            self.assertEqual(bot._jackett_warmup_next_batch(["rt", "kz", "nnm"]), ["rt", "kz"])
            self.assertEqual(bot._jackett_warmup_next_batch(["rt", "kz", "nnm"]), ["nnm", "rt"])

    async def test_run_once_warms_rotated_batch(self):
        jackett = MagicMock()
        jackett.get_indexers_if_idle.return_value = [
            {"id": "rutracker"},
            {"id": "kinozal"},
            {"id": "nnmclub"},
        ]
        jackett.warmup.return_value = {
            "ok": True,
            "results_count": 4,
            "elapsed_seconds": 0.2,
            "failed_indexers": [],
        }

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bot, "state_store", _make_store(tmp)),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "JACKETT_WARMUP_ENABLED", True),
            patch.object(bot, "JACKETT_WARMUP_INDEXERS", "auto"),
            patch.object(bot, "JACKETT_INDEXERS", "all"),
            patch.object(bot, "JACKETT_WARMUP_QUERY", "1080p"),
            patch.object(bot, "JACKETT_WARMUP_BATCH_SIZE", 2),
            patch.object(bot, "_jackett_warmup_cursor", 0),
            patch.object(bot, "_JACKETT_WARMUP_STATUS", {}),
        ):
            status = await bot._run_jackett_warmup_once()

        jackett.warmup.assert_called_once()
        self.assertEqual(jackett.warmup.call_args.args[0], "1080p")
        self.assertEqual(jackett.warmup.call_args.kwargs["indexers"], ["rutracker", "kinozal"])
        self.assertEqual(status["last_state"], "ok")
        self.assertEqual(status["last_results_count"], 4)

    async def test_run_once_records_guard_statuses(self):
        class Status:
            indexer_id = "kinozal"
            name = "Kinozal"
            status = 0
            results = 5
            error = ""

            @property
            def is_ok(self):
                return True

        jackett = MagicMock()
        jackett.get_indexers_if_idle.return_value = [{"id": "kinozal"}]
        jackett.warmup.return_value = {
            "ok": True,
            "results_count": 5,
            "elapsed_seconds": 0.2,
            "failed_indexers": [],
            "indexer_statuses": [Status()],
        }
        jackett.get_last_indexer_statuses.side_effect = AssertionError("side-channel must not be used")

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_jackett_guard({
                "version": 1,
                "indexers": {
                    "kinozal": {
                        "id": "kinozal",
                        "name": "Kinozal",
                        "state": "degraded",
                        "fail_streak": 1,
                        "next_retry_ts": 0,
                    }
                },
            })
            with (
                patch.object(bot, "state_store", store),
                patch.object(bot, "jackett_client", jackett),
                patch.object(bot, "JACKETT_WARMUP_ENABLED", True),
                patch.object(bot, "JACKETT_WARMUP_INDEXERS", "auto"),
                patch.object(bot, "JACKETT_INDEXERS", "all"),
                patch.object(bot, "JACKETT_WARMUP_QUERY", "1080p"),
                patch.object(bot, "JACKETT_WARMUP_BATCH_SIZE", 2),
                patch.object(bot, "_JACKETT_WARMUP_STATUS", {}),
            ):
                await bot._run_jackett_warmup_once()

            payload = store.load_jackett_guard()

        self.assertEqual(payload["indexers"]["kinozal"]["state"], "ok")
        self.assertEqual(payload["indexers"]["kinozal"]["fail_streak"], 0)
        jackett.get_last_indexer_statuses.assert_not_called()

    def test_movie_discovery_search_uses_returned_statuses_not_side_channel(self):
        class Status:
            indexer_id = "kinozal"
            name = "Kinozal"
            status = 1
            results = 0
            error = "timeout"

            @property
            def is_ok(self):
                return False

        jackett = MagicMock()
        jackett.search_with_statuses.return_value = ([], [Status()])
        jackett.get_last_indexer_statuses.side_effect = AssertionError("side-channel must not be used")

        with patch.object(bot, "jackett_client", jackett):
            results, statuses = bot._jackett_movie_discovery_search("2026 1080p")

        self.assertEqual(results, [])
        self.assertEqual([st.indexer_id for st in statuses], ["kinozal"])
        jackett.get_last_indexer_statuses.assert_not_called()

    def test_guard_summary_and_prune_use_active_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_jackett_guard({
                "version": 1,
                "indexers": {
                    "rutracker": {"id": "rutracker", "state": "degraded", "fail_streak": 1},
                    "old-indexer": {"id": "old-indexer", "state": "degraded", "fail_streak": 1},
                },
            })

            with patch.object(bot, "state_store", store):
                summary = bot._jackett_guard_unready_summary(None, {"rutracker"})
                removed = bot._prune_jackett_guard_state({"rutracker"})

            payload = store.load_jackett_guard()

        self.assertEqual(summary["enabled"], ["rutracker"])
        self.assertEqual(removed, ["old-indexer"])
        self.assertEqual(set(payload["indexers"].keys()), {"rutracker"})

    async def test_guardian_loop_continues_after_unexpected_failure(self):
        sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        next_due = MagicMock(side_effect=[RuntimeError("boom"), 0])

        with (
            patch.object(bot, "_jackett_warmup_enabled", return_value=True),
            patch.object(bot, "_jackett_guard_next_due_delay", next_due),
            patch.object(bot.asyncio, "sleep", sleeps),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot._jackett_guardian_loop(MagicMock())

        self.assertEqual(next_due.call_count, 2)

    async def test_run_once_skips_when_indexer_lookup_is_busy(self):
        jackett = MagicMock()
        jackett.get_indexers_if_idle.return_value = None

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bot, "state_store", _make_store(tmp)),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "JACKETT_WARMUP_ENABLED", True),
            patch.object(bot, "JACKETT_WARMUP_INDEXERS", "auto"),
            patch.object(bot, "JACKETT_INDEXERS", "all"),
            patch.object(bot, "_JACKETT_WARMUP_STATUS", {}),
        ):
            status = await bot._run_jackett_warmup_once()

        jackett.warmup.assert_not_called()
        self.assertEqual(status["last_state"], "skipped")
        self.assertEqual(status["last_error"], "busy")


class MovieDiscoveryAdminNotificationTests(unittest.TestCase):
    def test_ready_notification_uses_protected_mode_for_enabled_failures(self):
        text = bot._format_movie_discovery_ready_notification(
            failed_enabled=["kinozal"],
            failed_disabled=["noname-club"],
        )

        self.assertIn("защищённом режиме", text)
        self.assertIn("kinozal", text)
        self.assertIn("не влияют на /new", text)
        self.assertNotIn("полноценно функционирует", text)

    def test_recovery_notification_lists_recovered_indexers(self):
        text = bot._format_movie_discovery_recovery_notification(
            2,
            ["kinozal", "rutracker"],
        )

        self.assertIn("Поиск восстановился после 2", text)
        self.assertIn("Jackett снова готов: kinozal, rutracker", text)


class AdminPanelTests(unittest.TestCase):
    def _assert_access_result_keyboard(self, markup):
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks, {"admin:home", "admin:close"})

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

    def test_admin_panel_shows_ok_when_no_stuck_notifications(self):
        """Status line in /admin panel reports OK when no failure counters are at cap."""
        update = _make_message_update(chat_id=300)
        context = _make_context()
        progress_message = MagicMock()
        progress_message.edit_text = AsyncMock()
        update.message.reply_text.return_value = progress_message

        fake_store = MagicMock()
        fake_store.load_topic_subscriptions.return_value = {}
        # No notified_tasks with failures.
        fake_store.load_notified_tasks.return_value = {
            "t1": {"status": "done", "sent": ["300"], "failures": {}},
        }
        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = []

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", fake_store),
            patch.object(bot, "ds_client", fake_ds),
            patch.object(bot, "PLEX_ENABLED", False),
        ):
            asyncio.run(admin_command(update, context))

        text = progress_message.edit_text.call_args.args[0]
        self.assertIn("Уведомления о завершении: ✅ всё доставляется", text)
        # No reset button when count == 0
        markup = progress_message.edit_text.call_args.kwargs.get("reply_markup")
        button_labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertFalse(
            any("Сбросить счётчики" in lbl for lbl in button_labels),
            f"Reset button should be hidden when count=0, got: {button_labels}",
        )

    def test_admin_panel_shows_count_and_button_when_stuck(self):
        """Status line + reset button reflect the actual number of stuck tasks."""
        update = _make_message_update(chat_id=300)
        context = _make_context()
        progress_message = MagicMock()
        progress_message.edit_text = AsyncMock()
        update.message.reply_text.return_value = progress_message

        fake_store = MagicMock()
        fake_store.load_topic_subscriptions.return_value = {}
        # Two tasks with failures at cap, one without — count should be 2.
        fake_store.load_notified_tasks.return_value = {
            "t1": {"status": "done", "sent": [], "failures": {"300": 3}},
            "t2": {"status": "done", "sent": [], "failures": {"300": 3, "400": 1}},
            "t3": {"status": "done", "sent": ["300"], "failures": {}},
            "t4": {"status": "done", "sent": [], "failures": {"300": 2}},  # below cap
        }
        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = []

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "state_store", fake_store),
            patch.object(bot, "ds_client", fake_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "MAX_TASK_NOTIFICATION_FAILURES", 3),
        ):
            asyncio.run(admin_command(update, context))

        text = progress_message.edit_text.call_args.args[0]
        self.assertIn("Уведомления о завершении: ⚠️ зависших 2", text)
        markup = progress_message.edit_text.call_args.kwargs.get("reply_markup")
        button_labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertTrue(
            any("Сбросить счётчики (2)" in lbl for lbl in button_labels),
            f"Expected reset button with count 2, got: {button_labels}",
        )

    def test_count_stuck_notifications_helper(self):
        """Direct unit test for the helper that powers the admin status line."""
        fake_store = MagicMock()
        fake_store.load_notified_tasks.return_value = {
            "t1": {"failures": {"300": 3}},                # at cap → counted
            "t2": {"failures": {"300": 3, "400": 1}},      # one at cap → counted
            "t3": {"failures": {}},                         # empty → skipped
            "t4": {"failures": {"300": 2}},                 # below cap → skipped
            "t5": "done",                                   # legacy string → skipped
            "t6": {"failures": "bad"},                      # malformed → skipped
            "t7": None,                                     # missing → skipped
        }
        with (
            patch.object(bot, "state_store", fake_store),
            patch.object(bot, "MAX_TASK_NOTIFICATION_FAILURES", 3),
        ):
            self.assertEqual(bot._count_stuck_notifications(), 2)

    def test_reset_notify_failures_callback_clears_all(self):
        """Tapping «🔄 Сбросить счётчики» wipes failures across all entries."""
        update = _make_callback_update(chat_id=300, callback_data="admin:reset_notify_failures")
        context = _make_context()

        # Build a real state_store backed by a tmp dir so save/load round-trips.
        tmp = tempfile.TemporaryDirectory()
        try:
            store = _make_store(tmp.name)
            store.save_notified_tasks({
                "t1": {"status": "done", "sent": [], "failures": {"300": 3}},
                "t2": {"status": "done", "sent": ["300"], "failures": {"400": 3}},
                "t3": {"status": "done", "sent": ["300"], "failures": {}},
            })
            with (
                patch.object(bot, "ADMIN_CHAT_IDS", {300}),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(admin_callback(update, context))

            after = store.load_notified_tasks()
            self.assertEqual(after["t1"]["failures"], {})
            self.assertEqual(after["t2"]["failures"], {})
            self.assertEqual(after["t3"]["failures"], {})
        finally:
            tmp.cleanup()

        # Response text mentions affected count (2 — t3 had no failures so skipped).
        edit_calls = update.callback_query.edit_message_text.call_args_list
        last_text = edit_calls[-1].args[0] if edit_calls[-1].args else edit_calls[-1].kwargs.get("text", "")
        self.assertIn("Сброшено", last_text)
        self.assertIn("2", last_text)

    def test_admin_panel_times_out_download_station_summary(self):
        async def slow_to_thread(_func, *args, **kwargs):
            await asyncio.sleep(0.05)
            return []

        fake_store = MagicMock()
        fake_store.load_topic_subscriptions.return_value = {}
        fake_store.load_notified_tasks.return_value = {}

        with (
            patch.object(bot, "state_store", fake_store),
            patch.object(bot.asyncio, "to_thread", slow_to_thread),
            patch.object(bot, "_ADMIN_TASKS_TIMEOUT_SECONDS", 0.001),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "MOVIE_DISCOVERY_ENABLED", False),
            patch.object(bot, "VOICE_SEARCH_ENABLED", False),
            patch.object(bot, "GPT_ENABLED", False),
            patch.object(bot, "get_unified_disk_info", return_value=None),
        ):
            text = asyncio.run(bot._build_admin_panel_text())

        self.assertIn("Download Station", text)
        self.assertIn("0.001", text)

    def test_cached_diagnostics_text_includes_snapshot_time(self):
        context = _make_context()
        context.chat_data = {
            bot._ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY: object(),
            bot._ADMIN_DIAGNOSTICS_REPORT_TS_CACHE_KEY: "31.05 12:34",
        }

        with patch.object(bot, "format_diagnostics", MagicMock(return_value="Diag\n\nBody")):
            text = asyncio.run(bot._build_cached_diagnostics_text(context))

        self.assertEqual(text.splitlines()[:2], ["Diag", "Снимок: 31.05 12:34"])

    def test_admin_movie_status_callback_renders_details(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:movie_status")
        context = _make_context()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "KINOPOISK_ENABLED", True),
            patch.object(bot, "_format_admin_movie_discovery_details", MagicMock(return_value="movie details")),
        ):
            asyncio.run(admin_callback(update, context))

        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_called_once()
        self.assertEqual(update.callback_query.edit_message_text.call_args.args[0], "movie details")
        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertIn("admin:force_kp_refresh", callbacks)

    def test_admin_force_kp_refresh_callback_renders_budget(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:force_kp_refresh")
        context = _make_context()
        today = datetime.now(bot.DISPLAY_TIMEZONE).strftime("%Y-%m-%d")
        cache = {
            "kp_cache": {"hit": {"kp_id": "1"}, "miss": {}},
            "kp_api_stats": {"date": today, "searches": 0},
        }

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_load_movie_discovery_cache", MagicMock(return_value=cache)),
        ):
            asyncio.run(admin_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("KP", text)
        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertIn("admin:confirm_force_kp_refresh_gradual", callbacks)

    def test_admin_clear_kp_cache_callback_renders_confirmation(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:clear_kp_cache")
        context = _make_context()
        cache = {"kp_cache": {"one": {"kp_id": "1"}}}

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_load_movie_discovery_cache", MagicMock(return_value=cache)),
        ):
            asyncio.run(admin_callback(update, context))

        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertIn("admin:confirm_clear_kp_cache", callbacks)

    def test_admin_movie_trackers_callback_renders_panel(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:movie_trackers")
        context = _make_context()
        keyboard = MagicMock()
        panel = AsyncMock(return_value=("trackers", keyboard))

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_movie_trackers_panel", panel),
        ):
            asyncio.run(admin_callback(update, context))

        panel.assert_awaited_once()
        update.callback_query.edit_message_text.assert_called_once()
        self.assertEqual(update.callback_query.edit_message_text.call_args.args[0], "trackers")
        self.assertIs(update.callback_query.edit_message_text.call_args.kwargs["reply_markup"], keyboard)

    def test_admin_tracker_toggle_saves_selection_and_schedules_recompute(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:tracker_toggle:kinozal")
        context = _make_context()
        saved = {}
        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            coro.close()
            return MagicMock()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_load_movie_discovery_settings", MagicMock(return_value={
                "jackett_trackers_known": ["kinozal", "rutracker"],
                "jackett_trackers_enabled": ["kinozal", "rutracker"],
            })),
            patch.object(bot, "_save_movie_discovery_settings", MagicMock(side_effect=saved.update)),
            patch.object(bot, "_movie_trackers_panel", AsyncMock(return_value=("trackers", MagicMock()))),
            patch.object(bot, "_recompute_movie_discovery_from_cache", AsyncMock()),
            patch.object(bot.asyncio, "create_task", MagicMock(side_effect=fake_create_task)),
        ):
            asyncio.run(admin_callback(update, context))

        self.assertEqual(saved["jackett_trackers_enabled"], ["rutracker"])
        self.assertEqual(len(scheduled), 1)

    def test_admin_tracker_enable_all_clears_selection_override(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:tracker_enable_all")
        context = _make_context()
        saved = {}

        def fake_create_task(coro):
            coro.close()
            return MagicMock()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_load_movie_discovery_settings", MagicMock(return_value={
                "jackett_trackers_enabled": ["kinozal"],
            })),
            patch.object(bot, "_save_movie_discovery_settings", MagicMock(side_effect=saved.update)),
            patch.object(bot, "_movie_trackers_panel", AsyncMock(return_value=("trackers", MagicMock()))),
            patch.object(bot, "_recompute_movie_discovery_from_cache", AsyncMock()),
            patch.object(bot.asyncio, "create_task", MagicMock(side_effect=fake_create_task)),
        ):
            asyncio.run(admin_callback(update, context))

        self.assertIsNone(saved["jackett_trackers_enabled"])

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
        self.assertIn("reply_markup", update.callback_query.edit_message_text.call_args_list[0].kwargs)
        self.assertEqual(update.callback_query.edit_message_text.call_args_list[1].args[0], "diag text")

    def test_admin_diagnostics_detail_callback_uses_section_view(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:diag_jackett")
        context = _make_context()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_build_diagnostics_section_text", AsyncMock(return_value="jackett detail")),
        ):
            asyncio.run(admin_callback(update, context))

        update.callback_query.answer.assert_called_once()
        self.assertEqual(update.callback_query.edit_message_text.call_count, 2)
        self.assertEqual(update.callback_query.edit_message_text.call_args_list[0].args[0], "🧭 Проверяю раздел…")
        self.assertIn("reply_markup", update.callback_query.edit_message_text.call_args_list[0].kwargs)
        self.assertEqual(update.callback_query.edit_message_text.call_args_list[1].args[0], "jackett detail")

    def test_admin_diagnostics_back_uses_cached_report_without_refresh(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:diagnostics_back")
        context = _make_context()
        cached_report = object()
        context.chat_data = {bot._ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY: cached_report}
        build_report = AsyncMock()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_build_diagnostics_report", build_report),
            patch.object(bot, "format_diagnostics", MagicMock(return_value="cached diag")),
        ):
            asyncio.run(admin_callback(update, context))

        build_report.assert_not_awaited()
        update.callback_query.answer.assert_called_once()
        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertTrue(text.startswith("cached diag\nСнимок: "))

    def test_admin_diagnostics_detail_uses_cached_report_without_refresh(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:diag_plex")
        context = _make_context()
        cached_report = object()
        context.chat_data = {bot._ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY: cached_report}
        build_report = AsyncMock()
        format_section = MagicMock(return_value="plex detail")

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_build_diagnostics_report", build_report),
            patch.object(bot, "format_diagnostics_section", format_section),
        ):
            asyncio.run(admin_callback(update, context))

        build_report.assert_not_awaited()
        format_section.assert_called_once_with(cached_report, "plex")
        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertTrue(text.startswith("plex detail\nСнимок: "))

    def test_admin_diagnostics_detail_refresh_updates_cached_report(self):
        update = _make_callback_update(chat_id=300, callback_data="admin:diag_refresh:plex")
        context = _make_context()
        context.chat_data = {}
        fresh_report = object()

        with (
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "_build_diagnostics_report", AsyncMock(return_value=fresh_report)),
            patch.object(bot, "format_diagnostics_section", MagicMock(return_value="fresh plex detail")),
        ):
            asyncio.run(admin_callback(update, context))

        self.assertIs(context.chat_data[bot._ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY], fresh_report)
        self.assertEqual(update.callback_query.edit_message_text.call_count, 2)
        text = update.callback_query.edit_message_text.call_args_list[1].args[0]
        self.assertTrue(text.startswith("fresh plex detail\nСнимок: "))

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

    def test_format_tasks_uses_approved_user_name_for_admin_owner_label(self):
        task = {
            "id": "tid1",
            "title": "Movie",
            "status": "downloading",
            "size": 100,
            "additional": {"transfer": {"size_downloaded": 0, "speed_download": 0}},
        }
        fake_store = MagicMock()
        fake_store.load_task_owners.return_value = {"tid1": 100}
        fake_store.load_approved_users.return_value = {100: {"name": "Ivan @ivan"}}

        with (
            patch.object(bot, "state_store", fake_store),
            patch.object(bot, "_format_updated_at", MagicMock(return_value="12:00:00")),
        ):
            text = bot._format_tasks([task], scope="all")

        self.assertIn("Владелец: Ivan @ivan (100)", text)

    def test_admin_subscription_mode_toggle_updates_policy_fields(self):
        from subscription_policy import (
            DOWNLOAD_NOTIFY_ONLY,
            NOTIFY_EACH_UPDATE,
            NOTIFY_FINAL_ONLY,
        )

        update = _make_callback_update(chat_id=300, callback_data="sub:admin_set_mode:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Клиника",
                    "last_episode_end": 1,
                    "total_episodes": 2,
                    "notify_policy": NOTIFY_EACH_UPDATE,
                    "download_policy": DOWNLOAD_NOTIFY_ONLY,
                },
            })
            with (
                patch.object(bot, "ADMIN_CHAT_IDS", {300}),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

            sub = store.load_topic_subscriptions()["123"]
            self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
            # The admin quick-toggle changes only notifications; download
            # preferences must not be silently overwritten.
            self.assertEqual(sub["download_policy"], DOWNLOAD_NOTIFY_ONLY)

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("только когда сезон завершится", text)
        self.assertIn("не скачивать автоматически", text)

    def test_subs_command_renders_subscription_details(self):
        from subscription_policy import (
            DOWNLOAD_NOTIFY_ONLY,
            DOWNLOAD_ONLY_WHEN_COMPLETE,
            NOTIFY_EACH_UPDATE,
            NOTIFY_FINAL_ONLY,
        )

        update = _make_message_update(chat_id=100)
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Фарго / Fargo / Сезон 5, Серии 1-5 из 8",
                    "last_episode_end": 5,
                    "total_episodes": 8,
                    "notify_policy": NOTIFY_FINAL_ONLY,
                    "download_policy": DOWNLOAD_ONLY_WHEN_COMPLETE,
                },
                "jackett:abc": {
                    "type": "jackett",
                    "chat_id": 100,
                    "query": "Одни из нас сезон 2",
                    "title": "The Last of Us S02E03 of 8",
                    "tracker": "rutracker",
                    "last_episode_end": 3,
                    "total_episodes": 8,
                    "last_check": "2000-01-01 10:00",
                    "notify_policy": NOTIFY_EACH_UPDATE,
                    "download_policy": DOWNLOAD_NOTIFY_ONLY,
                },
            })
            next_check = datetime.now(bot.DISPLAY_TIMEZONE).replace(
                hour=18, minute=0, second=0, microsecond=0
            ).timestamp()
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_is_movie_subscribed", return_value=False),
                patch.object(bot, "_next_subscription_check_at", next_check),
            ):
                asyncio.run(subs_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("<b>Подписки</b> (2)", text)
        self.assertIn("Следующая проверка: сегодня 18:00", text)
        self.assertIn("Источник: Rutracker", text)
        self.assertIn("Прогресс: 5 из 8 эп.", text)
        self.assertIn("Уведомления: только когда сезон завершится", text)
        self.assertIn("Скачивание: когда сезон завершится", text)
        self.assertIn("Статус: ждём финал сезона", text)
        self.assertIn("Источник: Jackett · rutracker", text)
        self.assertIn("Проверено: 01.01 10:00", text)
        self.assertIn("Скачивание: не скачивать автоматически", text)

        keyboard = update.message.reply_text.call_args.kwargs["reply_markup"]
        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        callbacks = {button.text: button.callback_data for button in buttons}
        self.assertEqual(callbacks["⚙️ 1. Настроить"], "sub:settings:123")
        self.assertEqual(callbacks["🔕 1. Отписаться"], "sub:unsub:123")
        self.assertEqual(callbacks["⚙️ 2. Настроить"], "sub:settings:jackett:abc")
        self.assertEqual(callbacks["🔕 2. Отписаться"], "sub:jackett_unsub:jackett:abc")
        self.assertEqual(callbacks["✖️ Закрыть"], "task:close:")
        self.assertFalse(any("jackett_view" in value for value in callbacks.values()))

    def test_subscription_settings_screen_shows_current_policy(self):
        from subscription_policy import (
            DOWNLOAD_ONLY_WHEN_COMPLETE,
            NOTIFY_FINAL_ONLY,
        )

        update = _make_callback_update(chat_id=100, callback_data="sub:settings:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Фарго / Fargo / Сезон 5, Серии 1-5 из 8",
                    "last_episode_end": 5,
                    "total_episodes": 8,
                    "notify_policy": NOTIFY_FINAL_ONLY,
                    "download_policy": DOWNLOAD_ONLY_WHEN_COMPLETE,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("⚙️ <b>Подписка</b>", text)
        self.assertIn("Уведомления: <b>только когда сезон завершится</b>", text)
        self.assertIn("Скачивание: <b>когда сезон завершится</b>", text)

        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["🔔 Уведомления"], "sub:settings_notify:123")
        self.assertEqual(buttons["⬇️ Скачивание"], "sub:settings_download:123")
        self.assertEqual(buttons["⬅️ К подпискам"], "sub:list")
        self.assertEqual(buttons["✖️ Закрыть"], "task:close:")

    def test_subscription_notify_settings_hides_silent_when_download_disabled(self):
        from subscription_policy import (
            DOWNLOAD_NOTIFY_ONLY,
            NOTIFY_EACH_UPDATE,
        )

        update = _make_callback_update(chat_id=100, callback_data="sub:settings_notify:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Фарго",
                    "last_episode_end": 5,
                    "total_episodes": 8,
                    "notify_policy": NOTIFY_EACH_UPDATE,
                    "download_policy": DOWNLOAD_NOTIFY_ONLY,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Когда уведомлять", text)
        self.assertIn("Нужно оставить хотя бы одно действие", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("✅ 🔔 О каждой новой серии", labels)
        self.assertIn("🎯 Только когда сезон завершится", labels)
        self.assertFalse(any("Не уведомлять" in label for label in labels))

    def test_subscription_set_notify_updates_jackett_subscription(self):
        from subscription_policy import (
            DOWNLOAD_NOTIFY_ONLY,
            NOTIFY_EACH_UPDATE,
            NOTIFY_FINAL_ONLY,
        )

        update = _make_callback_update(
            chat_id=100,
            callback_data=f"sub:set_notify:{NOTIFY_FINAL_ONLY}:jackett:abc",
        )
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "jackett:abc": {
                    "type": "jackett",
                    "chat_id": 100,
                    "query": "Одни из нас сезон 2",
                    "last_episode_end": 3,
                    "total_episodes": 8,
                    "notify_policy": NOTIFY_EACH_UPDATE,
                    "download_policy": DOWNLOAD_NOTIFY_ONLY,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

            sub = store.load_topic_subscriptions()["jackett:abc"]
            self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
            self.assertEqual(sub["download_policy"], DOWNLOAD_NOTIFY_ONLY)

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("✅ Настройки обновлены", text)
        self.assertIn("Уведомления: <b>только когда сезон завершится</b>", text)

    def test_subscription_set_download_rejects_do_nothing_pair(self):
        from subscription_policy import (
            DOWNLOAD_AUTO_EACH_UPDATE,
            DOWNLOAD_NOTIFY_ONLY,
            NOTIFY_SILENT,
        )

        update = _make_callback_update(
            chat_id=100,
            callback_data=f"sub:set_download:{DOWNLOAD_NOTIFY_ONLY}:123",
        )
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Фарго",
                    "last_episode_end": 5,
                    "total_episodes": 8,
                    "notify_policy": NOTIFY_SILENT,
                    "download_policy": DOWNLOAD_AUTO_EACH_UPDATE,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

            sub = store.load_topic_subscriptions()["123"]
            self.assertEqual(sub["notify_policy"], NOTIFY_SILENT)
            self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("ничего не будет делать", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks["🔔 Настроить уведомления"], "sub:settings_notify:123")
        self.assertEqual(callbacks["⬇️ Настроить скачивание"], "sub:settings_download:123")

    def test_subscription_download_settings_hides_notify_only_when_silent(self):
        from subscription_policy import (
            DOWNLOAD_AUTO_EACH_UPDATE,
            NOTIFY_SILENT,
        )

        update = _make_callback_update(chat_id=100, callback_data="sub:settings_download:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Фарго",
                    "last_episode_end": 5,
                    "total_episodes": 8,
                    "notify_policy": NOTIFY_SILENT,
                    "download_policy": DOWNLOAD_AUTO_EACH_UPDATE,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Когда скачивать", text)
        self.assertIn("Нужно оставить хотя бы одно действие", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("✅ ⬇️ Новые серии по мере выхода", labels)
        self.assertIn("📦 Когда сезон завершится", labels)
        self.assertFalse(any("Не скачивать" in label for label in labels))

    def test_subscription_list_callback_returns_to_list(self):
        update = _make_callback_update(chat_id=100, callback_data="sub:list")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Фарго",
                    "last_episode_end": 5,
                    "total_episodes": 8,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_is_movie_subscribed", return_value=False),
                patch.object(bot, "_next_subscription_check_at", None),
            ):
                asyncio.run(sub_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("<b>Подписки</b> (1)", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["⚙️ 1. Настроить"], "sub:settings:123")

    def test_non_owner_cannot_open_subscription_settings(self):
        update = _make_callback_update(chat_id=100, callback_data="sub:settings:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 200,
                    "title": "Клиника",
                    "last_episode_end": 1,
                    "total_episodes": 2,
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100, 200}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("не относится", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertEqual(buttons["⬅️ К подпискам"], "sub:list")
        self.assertEqual(buttons["✖️ Закрыть"], "task:close:")

    def test_subs_command_renders_new_subscription_without_download_policy(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({})
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_is_movie_subscribed", return_value=True),
                patch.object(bot, "_next_subscription_check_at", None),
            ):
                asyncio.run(subs_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("🎬 <b>Новинки /new</b>", text)
        self.assertIn("Уведомления: включены", text)
        self.assertIn("присылаю новые фильмы и мультфильмы", text)
        self.assertNotIn("Скачивание:", text)

        keyboard = update.message.reply_text.call_args.kwargs["reply_markup"]
        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        callbacks = {button.text: button.callback_data for button in buttons}
        self.assertEqual(callbacks["🔕 Отписаться от /new"], "sub:new_unsub")
        self.assertEqual(callbacks["✖️ Закрыть"], "task:close:")

    def test_subs_command_empty_state_explains_how_to_add_subscription(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({})
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_is_movie_subscribed", return_value=False),
            ):
                asyncio.run(subs_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Подписок пока нет", text)
        self.assertIn("следит за новыми сериями", text)
        self.assertIn("Как добавить подписку", text)

        keyboard = update.message.reply_text.call_args.kwargs["reply_markup"]
        callbacks = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks["✖️ Закрыть"], "task:close:")

    def test_subs_command_marks_unavailable_subscription(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {
                    "chat_id": 100,
                    "title": "Клиника / Scrubs / Сезон 2, Серии 1-4 из 9",
                    "last_episode_end": 4,
                    "total_episodes": 9,
                    "unavailable_at": "2026-05-25 11:00",
                    "unavailable_reason": "тема удалена",
                },
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_is_movie_subscribed", return_value=False),
                patch.object(bot, "_next_subscription_check_at", None),
            ):
                asyncio.run(subs_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Прогресс: 4 из 9 эп.", text)
        self.assertIn("Статус: ⚠️ проверка приостановлена: тема удалена", text)

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
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertEqual(buttons["⬅️ К подпискам"], "sub:list")
        self.assertEqual(buttons["✖️ Закрыть"], "task:close:")

    def test_unsubscribe_subscription_keeps_navigation_buttons(self):
        update = _make_callback_update(chat_id=100, callback_data="sub:unsub:123")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_topic_subscriptions({
                "123": {"chat_id": 100, "title": "Клиника", "last_episode_end": 1, "total_episodes": 2},
            })
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(sub_callback(update, context))

            self.assertNotIn("123", store.load_topic_subscriptions())

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Подписка отменена", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertEqual(buttons["⬅️ К подпискам"], "sub:list")
        self.assertEqual(buttons["✖️ Закрыть"], "task:close:")

    def test_access_approve_sends_post_approval_welcome(self):
        update = _make_callback_update(chat_id=300, callback_data="access:approve:200")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            with (
                patch.dict(bot.ACCESS_PENDING_USERS, {200: "Petr"}, clear=True),
                patch.object(bot, "ADMIN_CHAT_IDS", {300}),
                patch.object(bot, "ALLOWED_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "RUTRACKER_ENABLED", True),
                patch.object(bot, "JACKETT_ENABLED", True),
                patch.object(bot, "KINOPOISK_ENABLED", True),
                patch.object(bot, "MOVIE_DISCOVERY_ENABLED", True),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "VOICE_SEARCH_ENABLED", True),
            ):
                asyncio.run(access_callback(update, context))

        context.bot.send_message.assert_awaited_once()
        sent = context.bot.send_message.call_args.kwargs
        self.assertEqual(sent["chat_id"], 200)
        text = sent["text"]
        self.assertIn("Доступ разрешён", text)
        self.assertIn("название фильма", text)
        self.assertIn("Кинопоиска", text)
        self.assertIn("/help", text)
        self.assertNotIn(".torrent файлом", text)
        self.assertNotIn("magnet-ссылку сообщением", text)
        self._assert_access_result_keyboard(
            update.callback_query.edit_message_text.call_args.kwargs["reply_markup"],
        )

    def test_access_deny_shows_admin_result_keyboard(self):
        update = _make_callback_update(chat_id=300, callback_data="access:deny:200")
        context = _make_context()

        with (
            patch.dict(bot.ACCESS_PENDING_USERS, {200: "Petr"}, clear=True),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
        ):
            asyncio.run(access_callback(update, context))

        self._assert_access_result_keyboard(
            update.callback_query.edit_message_text.call_args.kwargs["reply_markup"],
        )

    def test_access_malformed_callback_shows_admin_result_keyboard(self):
        update = _make_callback_update(chat_id=300, callback_data="access:approve:not-a-number")
        context = _make_context()

        with patch.object(bot, "ADMIN_CHAT_IDS", {300}):
            asyncio.run(access_callback(update, context))

        self._assert_access_result_keyboard(
            update.callback_query.edit_message_text.call_args.kwargs["reply_markup"],
        )

    def test_access_users_refresh_shows_pending_requests(self):
        update = _make_callback_update(chat_id=300, callback_data="access:users_refresh")
        context = _make_context()
        fake_store = MagicMock()
        fake_store.load_approved_users.return_value = {}

        with (
            patch.dict(bot.ACCESS_PENDING_USERS, {200: "Petr"}, clear=True),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "state_store", fake_store),
        ):
            asyncio.run(access_callback(update, context))

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("⏳ Ожидают решения:", text)
        self.assertIn("200 — Petr", text)
        buttons = {
            button.text: button.callback_data
            for row in update.callback_query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["✅ Petr"], "access:approve:200")
        self.assertEqual(buttons["🚫 Отклонить"], "access:deny:200")

    def test_access_remove_confirm_does_not_revoke_access(self):
        update = _make_callback_update(chat_id=300, callback_data="access:remove_confirm:200")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.add_approved_user(200, "Petr")

            with (
                patch.object(bot, "ADMIN_CHAT_IDS", {300}),
                patch.object(bot, "state_store", store),
            ):
                asyncio.run(access_callback(update, context))

            self.assertIn(200, store.load_approved_chat_ids())

        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Удалить доступ?", text)
        self.assertIn("Petr", text)
        buttons = {
            button.text: button.callback_data
            for row in update.callback_query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["✅ Удалить доступ"], "access:remove:200")
        self.assertEqual(buttons["⬅️ Назад"], "access:users_refresh")

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

        self.assertRegex(buttons["🎬 1. Невеста!"], r"^new:show:0:[0-9a-f]{12}$")
        self.assertEqual(buttons["✖️ Закрыть"], "new:close")

    def test_movie_new_show_recovers_card_when_cache_order_changed(self):
        old_first = {
            "key": "2026:old",
            "title": "Old",
            "year": 2026,
            "releases": [{"title": "Old release", "source": "rutracker"}],
        }
        target = {
            "key": "2026:target",
            "title": "Target",
            "year": 2026,
            "releases": [{"title": "Target release", "source": "rutracker"}],
        }
        token = bot._movie_discovery_card_token(target)
        update = _make_callback_update(chat_id=100, callback_data=f"new:show:0:{token}")
        context = _make_context()
        cache = {"cards": [old_first, target]}

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_discovery_cache", return_value=cache),
            patch.object(bot, "_build_results_text", return_value="results"),
        ):
            result = asyncio.run(bot.movie_new_show_releases(update, context))

        self.assertEqual(result, bot.SEARCH_RESULTS)
        self.assertEqual(context.user_data["srch_results"][0]["title"], "Target release")

    def test_movie_new_show_stale_token_asks_to_refresh(self):
        card = {
            "key": "2026:current",
            "title": "Current",
            "year": 2026,
            "releases": [{"title": "Current release", "source": "rutracker"}],
        }
        update = _make_callback_update(chat_id=100, callback_data="new:show:0:deadbeefdead")
        context = _make_context()
        cache = {"cards": [card]}

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_discovery_cache", return_value=cache),
        ):
            result = asyncio.run(bot.movie_new_show_releases(update, context))

        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertNotIn("srch_results", context.user_data)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Обновите список", text)

    def test_movie_new_show_without_releases_keeps_new_navigation(self):
        card = {
            "key": "2026:empty",
            "title": "Empty",
            "year": 2026,
            "releases": [],
        }
        token = bot._movie_discovery_card_token(card)
        update = _make_callback_update(chat_id=100, callback_data=f"new:show:0:{token}")
        context = _make_context()
        cache = {"cards": [card]}

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_discovery_cache", return_value=cache),
        ):
            result = asyncio.run(bot.movie_new_show_releases(update, context))

        self.assertEqual(result, bot.ConversationHandler.END)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("нет подходящих раздач", text)
        keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertEqual(buttons["🔄 Обновить"], "new:refresh")
        self.assertEqual(buttons["✖️ Закрыть"], "new:close")

    def test_unconfirmed_consensus_card_is_hidden_from_new_list(self):
        cache = {
            "updated_at": "2026-06-05 12:12",
            "prev_top10_kp_ids": [1, 2],
            "cards": [
                {"title": "Confirmed A", "year": 2026, "kp_id": 1},
                {"title": "Transient", "year": 2026, "kp_id": 99},
                {"title": "Confirmed B", "year": 2026, "kp_id": 2},
            ],
        }

        text = _format_movie_discovery_cache(cache)

        self.assertIn("Confirmed A", text)
        self.assertIn("Confirmed B", text)
        self.assertNotIn("Transient", text)

    def test_confirmed_cards_fill_from_below_raw_top10(self):
        cache = {
            "prev_top10_kp_ids": [1, 11],
            "cards": [
                {"title": "Confirmed A", "kp_id": 1},
                {"title": "Transient", "kp_id": 99},
                {"title": "Confirmed From Below", "kp_id": 11},
            ],
        }

        visible = bot._movie_discovery_confirmed_cards(cache)

        self.assertEqual([card["kp_id"] for card in visible], [1, 11])

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

    def test_movie_discovery_empty_state_explains_filters_and_refresh(self):
        text = _format_movie_discovery_cache({
            "updated_at": "2026-05-12 13:00",
            "cards": [],
        })

        self.assertIn("Пока нет подходящих новинок", text)
        self.assertIn("фильтрам выше", text)
        self.assertIn("Почему может быть пусто", text)
        self.assertIn("нажать «Обновить»", text)

    def test_movie_discovery_refresh_start_text_explains_wait(self):
        text = bot._movie_discovery_refresh_start_text()
        self.assertIn("Обновляю новинки", text)
        self.assertIn("трекеры", text)
        self.assertIn("Plex-метки", text)
        self.assertIn("пару минут", text)

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

    def test_movie_new_command_disables_link_preview(self):
        """/new reply must have link preview disabled."""
        update = _make_message_update(chat_id=100)
        context = _make_context()
        fake_cache = {
            "cards": [{"title": "Тест", "year": 2026, "score": 0.8}],
            "updated_at": "2026-05-14 22:00",
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_movie_discovery_enabled", return_value=True),
            patch.object(bot, "_load_movie_discovery_cache", return_value=fake_cache),
        ):
            asyncio.run(movie_new_command(update, context))

        call_kwargs = update.message.reply_text.call_args.kwargs
        lpo = call_kwargs.get("link_preview_options")
        self.assertIsNotNone(lpo, "link_preview_options must be set")
        self.assertTrue(lpo.is_disabled, "link preview must be disabled in /new")
        context.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)

    def test_movie_new_recomputes_score_on_cache_hit(self):
        """Cache hit must recompute score under current formula/year and resort.

        Regression: the cache stores `score` snapshotted at last refresh — if
        the year boundary crosses or the formula changes, the cached order
        becomes stale. /new must re-sort before display.
        """
        update = _make_message_update(chat_id=100)
        context = _make_context()
        # Two cards: one with high stored score but low current_year recency
        # (year=2020), one with low stored score but matches current_year
        # (year matches today). After recompute the high-recency card wins.
        from datetime import datetime as _dt
        current_year = _dt.now(bot.DISPLAY_TIMEZONE).year
        fake_cache = {
            "updated_at": "2026-05-14 22:00",
            "cards": [
                # Stored score=0.99 but year is far in the past — recency hurts.
                {
                    "title": "Old But Stored First",
                    "year": 2020,
                    "score": 0.99,
                    "rating": 7.5,
                    "best_seeders": 50,
                    "best_quality": "1080p",
                    "releases": [{"title": "x", "score": 1}],
                },
                # Stored score=0.10 but year is current — recency dominates.
                {
                    "title": "Fresh But Stored Last",
                    "year": current_year,
                    "score": 0.10,
                    "rating": 7.5,
                    "best_seeders": 50,
                    "best_quality": "1080p",
                    "releases": [{"title": "x", "score": 1}],
                },
            ],
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_movie_discovery_enabled", return_value=True),
            patch.object(bot, "_load_movie_discovery_cache", return_value=fake_cache),
        ):
            asyncio.run(movie_new_command(update, context))

        text = update.message.reply_text.call_args.args[0]
        # The fresh-year card must appear before the old card in the rendered list.
        pos_fresh = text.find("Fresh But Stored Last")
        pos_old = text.find("Old But Stored First")
        self.assertGreater(pos_fresh, 0)
        self.assertGreater(pos_old, 0)
        self.assertLess(pos_fresh, pos_old, "recomputed score must place fresh-year card first")

    def test_movie_new_open_recomputes_score_on_cache_hit(self):
        """The notification 'open /new' callback must render the same sorted view as /new."""
        update = _make_callback_update(chat_id=100, callback_data="new:open")
        context = _make_context()
        from datetime import datetime as _dt
        current_year = _dt.now(bot.DISPLAY_TIMEZONE).year
        fake_cache = {
            "updated_at": "2026-05-14 22:00",
            "cards": [
                {
                    "title": "Old From Push",
                    "year": 2020,
                    "score": 0.99,
                    "rating": 7.5,
                    "best_seeders": 50,
                    "best_quality": "1080p",
                    "releases": [{"title": "x", "score": 1}],
                },
                {
                    "title": "Fresh From Push",
                    "year": current_year,
                    "score": 0.10,
                    "rating": 7.5,
                    "best_seeders": 50,
                    "best_quality": "1080p",
                    "releases": [{"title": "x", "score": 1}],
                },
            ],
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_discovery_cache", return_value=fake_cache),
        ):
            asyncio.run(bot.movie_new_open_callback(update, context))

        context.bot.send_message.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_called()
        update.callback_query.message.delete.assert_awaited_once()
        call_kwargs = context.bot.send_message.call_args.kwargs
        text = call_kwargs["text"]
        lpo = call_kwargs.get("link_preview_options")
        self.assertIsNotNone(lpo)
        self.assertTrue(lpo.is_disabled)
        pos_fresh = text.find("Fresh From Push")
        pos_old = text.find("Old From Push")
        self.assertGreater(pos_fresh, 0)
        self.assertGreater(pos_old, 0)
        self.assertLess(pos_fresh, pos_old)

    def test_movie_new_open_from_photo_notification_sends_message_and_deletes_notification(self):
        """Photo notifications have caption, not text, so /new must be sent as a new message."""
        update = _make_callback_update(chat_id=100, callback_data="new:open")
        update.callback_query.message.text = None
        update.callback_query.message.caption = "New movie notification"
        context = _make_context()
        fake_cache = {
            "updated_at": "2026-05-14 22:00",
            "cards": [{
                "title": "Photo Push Movie",
                "year": 2026,
                "score": 0.8,
                "rating": 7.5,
                "best_seeders": 50,
                "best_quality": "1080p",
                "releases": [{"title": "x", "score": 1}],
            }],
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_discovery_cache", return_value=fake_cache),
        ):
            asyncio.run(bot.movie_new_open_callback(update, context))

        context.bot.send_message.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_called()
        update.callback_query.message.delete.assert_awaited_once()
        self.assertIn("Photo Push Movie", context.bot.send_message.call_args.kwargs["text"])

    def test_movie_new_open_with_push_id_uses_notification_snapshot(self):
        update = _make_callback_update(chat_id=100, callback_data="new:open:abc123def0")
        context = _make_context()
        snapshot = {
            "items": [{
                "card": {
                    "kp_id": 77,
                    "title": "Snapshot Only Movie",
                    "year": 2026,
                    "rating": 7.5,
                    "best_quality": "1080p",
                    "releases": [{"title": "snapshot release", "score": 1}],
                },
                "result": {"title": "Snapshot Only Movie 1080p", "source": "jackett"},
            }],
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_notification_snapshot", return_value=snapshot),
            patch.object(bot, "_movie_notification_snapshot_changed", return_value=False) as snapshot_changed,
            patch.object(bot, "_mark_user_shown_in_new") as mark_shown,
        ):
            asyncio.run(bot.movie_new_open_callback(update, context))

        snapshot_changed.assert_called_once()
        context.bot.send_message.assert_awaited_once()
        sent = context.bot.send_message.call_args.kwargs
        self.assertIn("Snapshot Only Movie", sent["text"])
        self.assertNotIn("Свежий /new уже изменился", sent["text"])
        mark_shown.assert_called_once()
        self.assertEqual(mark_shown.call_args.args[0], 100)
        self.assertEqual(mark_shown.call_args.args[1][0]["kp_id"], 77)
        update.callback_query.message.delete.assert_awaited_once()

    def test_movie_new_open_with_changed_snapshot_points_to_fresh_new(self):
        update = _make_callback_update(chat_id=100, callback_data="new:open:abc123def0")
        context = _make_context()
        snapshot = {
            "items": [{
                "card": {
                    "kp_id": 77,
                    "title": "Historical Movie",
                    "year": 2026,
                    "rating": 7.5,
                    "best_quality": "1080p",
                    "releases": [{"title": "historical release", "score": 1}],
                },
                "result": {"title": "Historical Movie 1080p", "source": "jackett"},
            }],
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_load_movie_notification_snapshot", return_value=snapshot),
            patch.object(bot, "_movie_notification_snapshot_changed", return_value=True),
            patch.object(bot, "_mark_user_shown_in_new"),
        ):
            asyncio.run(bot.movie_new_open_callback(update, context))

        sent = context.bot.send_message.call_args.kwargs
        self.assertIn("Свежий /new уже изменился", sent["text"])
        buttons = {
            button.text: button.callback_data
            for row in sent["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["🔄 Свежий /new"], "new:open")

    def test_recompute_and_resort_resorts_in_place(self):
        """_recompute_and_resort_cards: cards with stale wrong-order scores get re-sorted."""
        from datetime import datetime as _dt
        current_year = _dt.now(bot.DISPLAY_TIMEZONE).year
        cards = [
            {
                "title": "A (old, stored high)",
                "year": 2018,
                "score": 0.99,
                "rating": 7.0,
                "best_seeders": 100,
                "best_quality": "1080p",
                "releases": [{"title": "x", "score": 1}],
            },
            {
                "title": "B (current year, stored low)",
                "year": current_year,
                "score": 0.10,
                "rating": 7.0,
                "best_seeders": 100,
                "best_quality": "1080p",
                "releases": [{"title": "x", "score": 1}],
            },
        ]
        bot._recompute_and_resort_cards(cards)
        self.assertEqual(cards[0]["title"], "B (current year, stored low)")
        self.assertEqual(cards[1]["title"], "A (old, stored high)")

    def test_recompute_and_resort_empty_is_noop(self):
        """Empty list must not raise."""
        cards: list[dict] = []
        bot._recompute_and_resort_cards(cards)
        self.assertEqual(cards, [])

    def test_movie_new_refresh_callback_disables_link_preview(self):
        """«Обновить» callback must have link preview disabled."""
        update = _make_callback_update(chat_id=100, callback_data="new:refresh")
        context = _make_context()
        fake_cache = {
            "cards": [{"title": "Тест", "year": 2026, "score": 0.8}],
            "updated_at": "2026-05-14 22:00",
        }
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_movie_discovery_enabled", return_value=True),
            patch.object(bot, "_refresh_movie_discovery_cache", AsyncMock(return_value=fake_cache)),
        ):
            asyncio.run(movie_new_refresh_callback(update, context))

        edit_calls = update.callback_query.edit_message_text.call_args_list
        # Last call is the final result (not the «Обновляю…» intermediate)
        last_kwargs = edit_calls[-1].kwargs
        lpo = last_kwargs.get("link_preview_options")
        self.assertIsNotNone(lpo, "link_preview_options must be set on refresh")
        self.assertTrue(lpo.is_disabled, "link preview must be disabled after refresh")


class MovieDiscoveryRefreshSingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_normal_refreshes_coalesce_into_one_inner_call(self):
        cache = {"cards": [{"title": "A"}], "updated_at": "2026-05-24 12:00"}
        saved = {"cache": cache}
        calls: list[int | None] = []

        async def fake_inner(max_stale_kp_refresh: int | None = bot._KP_MAX_STALE_REFRESH) -> dict:
            calls.append(max_stale_kp_refresh)
            await asyncio.sleep(0.01)
            saved["cache"] = cache
            return cache

        with (
            patch.object(bot, "_movie_discovery_refresh_lock", None),
            patch.object(bot, "_movie_discovery_refresh_current_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_finished_at", 0.0),
            patch.object(bot, "_refresh_movie_discovery_cache_inner", fake_inner),
            patch.object(bot, "_load_movie_discovery_cache", side_effect=lambda: saved["cache"]),
        ):
            results = await asyncio.gather(*[bot._refresh_movie_discovery_cache() for _ in range(5)])

        self.assertEqual(calls, [bot._KP_MAX_STALE_REFRESH])
        self.assertTrue(all(result is cache for result in results))

    async def test_force_refresh_does_not_reuse_recent_normal_cache(self):
        first_cache = {"cards": [{"title": "first"}]}
        second_cache = {"cards": [{"title": "second"}]}
        saved = {"cache": first_cache}
        calls: list[int | None] = []

        async def fake_inner(max_stale_kp_refresh: int | None = bot._KP_MAX_STALE_REFRESH) -> dict:
            calls.append(max_stale_kp_refresh)
            cache = first_cache if len(calls) == 1 else second_cache
            saved["cache"] = cache
            return cache

        with (
            patch.object(bot, "_movie_discovery_refresh_lock", None),
            patch.object(bot, "_movie_discovery_refresh_current_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_finished_at", 0.0),
            patch.object(bot, "_refresh_movie_discovery_cache_inner", fake_inner),
            patch.object(bot, "_load_movie_discovery_cache", side_effect=lambda: saved["cache"]),
        ):
            first_result = await bot._refresh_movie_discovery_cache()
            second_result = await bot._refresh_movie_discovery_cache(force_refresh=True)

        self.assertEqual(calls, [bot._KP_MAX_STALE_REFRESH, bot._KP_MAX_STALE_REFRESH])
        self.assertIs(first_result, first_cache)
        self.assertIs(second_result, second_cache)

    async def test_full_kp_refresh_is_not_coalesced_by_normal_refresh(self):
        saved = {"cache": {"cards": []}}
        calls: list[int | None] = []
        normal_started = asyncio.Event()
        normal_release = asyncio.Event()

        async def fake_inner(max_stale_kp_refresh: int | None = bot._KP_MAX_STALE_REFRESH) -> dict:
            calls.append(max_stale_kp_refresh)
            if max_stale_kp_refresh is not None:
                normal_started.set()
                await normal_release.wait()
                cache = {"cards": [{"title": "normal"}]}
            else:
                cache = {"cards": [{"title": "full"}]}
            saved["cache"] = cache
            return cache

        with (
            patch.object(bot, "_movie_discovery_refresh_lock", None),
            patch.object(bot, "_movie_discovery_refresh_current_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_finished_at", 0.0),
            patch.object(bot, "_refresh_movie_discovery_cache_inner", fake_inner),
            patch.object(bot, "_load_movie_discovery_cache", side_effect=lambda: saved["cache"]),
        ):
            normal_task = asyncio.create_task(bot._refresh_movie_discovery_cache())
            await normal_started.wait()
            full_task = asyncio.create_task(bot._refresh_movie_discovery_cache(max_stale_kp_refresh=None))
            await asyncio.sleep(0)
            normal_release.set()
            normal_result, full_result = await asyncio.gather(normal_task, full_task)

        self.assertEqual(calls, [bot._KP_MAX_STALE_REFRESH, None])
        self.assertEqual(normal_result["cards"][0]["title"], "normal")
        self.assertEqual(full_result["cards"][0]["title"], "full")

    async def test_full_kp_refresh_covers_waiting_normal_refresh(self):
        full_cache = {"cards": [{"title": "full"}], "updated_at": "2026-05-24 12:00"}
        saved = {"cache": full_cache}
        calls: list[int | None] = []
        full_started = asyncio.Event()
        full_release = asyncio.Event()

        async def fake_inner(max_stale_kp_refresh: int | None = bot._KP_MAX_STALE_REFRESH) -> dict:
            calls.append(max_stale_kp_refresh)
            if max_stale_kp_refresh is None:
                full_started.set()
                await full_release.wait()
            saved["cache"] = full_cache
            return full_cache

        with (
            patch.object(bot, "_movie_discovery_refresh_lock", None),
            patch.object(bot, "_movie_discovery_refresh_current_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_mode", ""),
            patch.object(bot, "_movie_discovery_refresh_last_finished_at", 0.0),
            patch.object(bot, "_refresh_movie_discovery_cache_inner", fake_inner),
            patch.object(bot, "_load_movie_discovery_cache", side_effect=lambda: saved["cache"]),
        ):
            full_task = asyncio.create_task(bot._refresh_movie_discovery_cache(max_stale_kp_refresh=None))
            await full_started.wait()
            normal_task = asyncio.create_task(bot._refresh_movie_discovery_cache())
            await asyncio.sleep(0)
            full_release.set()
            full_result, normal_result = await asyncio.gather(full_task, normal_task)

        self.assertEqual(calls, [None])
        self.assertIs(full_result, full_cache)
        self.assertIs(normal_result, full_cache)

    async def test_refresh_callback_mentions_in_progress_refresh(self):
        update = _make_callback_update(chat_id=100, callback_data="new:refresh")
        context = _make_context()
        fake_cache = {
            "cards": [{"title": "Тест", "year": 2026, "score": 0.8}],
            "updated_at": "2026-05-24 12:00",
        }
        lock = asyncio.Lock()
        await lock.acquire()
        refresh_mock = AsyncMock(return_value=fake_cache)

        try:
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
                patch.object(bot, "_movie_discovery_refresh_lock", lock),
                patch.object(bot, "_movie_discovery_refresh_current_mode", "normal"),
                patch.object(bot, "_refresh_movie_discovery_cache", refresh_mock),
            ):
                await movie_new_refresh_callback(update, context)
        finally:
            lock.release()

        first_text = update.callback_query.edit_message_text.call_args_list[0].args[0]
        self.assertIn("Новинки уже обновляются", first_text)
        self.assertIn("Plex-метки", first_text)
        self.assertIn("пару минут", first_text)
        refresh_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# _enrich_cards_with_plex + _format_movie_discovery_cache (Plex badge)
# ---------------------------------------------------------------------------


class PlexEnrichmentTests(unittest.TestCase):
    """Tests for _enrich_cards_with_plex() and ✅ badge rendering in /new."""

    def _make_movie(self, title: str, year: int, resolution: str = "1080"):
        """Return a minimal PlexMovie-like object (real import avoided — use MagicMock)."""
        m = MagicMock()
        m.title = title
        m.year = year
        m.resolution = resolution
        return m

    def test_enrich_sets_in_plex_true_when_found(self):
        """Card matching a Plex title should get in_plex=True."""
        cards = [{"title": "Dune", "alt_title": "", "year": 2021}]
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune", 2021): self._make_movie("Dune", 2021)}),
        ):
            _enrich_cards_with_plex(cards)

        self.assertTrue(cards[0]["in_plex"])
        self.assertEqual(cards[0]["plex_resolution"], "1080")

    def test_enrich_sets_in_plex_false_when_not_found(self):
        """Card not present in Plex library should get in_plex=False and plex_resolution=None."""
        # Library is non-empty (has a different movie) but not this one
        cards = [{"title": "Unknown Film", "alt_title": "", "year": 2024}]
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("other movie", 2020): self._make_movie("Other Movie", 2020)}),
        ):
            _enrich_cards_with_plex(cards)

        self.assertFalse(cards[0]["in_plex"])
        self.assertIsNone(cards[0]["plex_resolution"])

    def test_enrich_falls_back_to_alt_title(self):
        """When main title misses, alt_title is tried."""
        cards = [{"title": "Дюна", "alt_title": "Dune", "year": 2021}]
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune", 2021): self._make_movie("Dune", 2021)}),
        ):
            _enrich_cards_with_plex(cards)

        self.assertTrue(cards[0]["in_plex"])

    def test_enrich_no_op_when_plex_disabled(self):
        """_enrich_cards_with_plex must be a no-op when PLEX_ENABLED is False."""
        cards = [{"title": "Dune", "alt_title": "", "year": 2021}]
        with (
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_plex_library", {("dune", 2021): self._make_movie("Dune", 2021)}),
        ):
            _enrich_cards_with_plex(cards)

        self.assertNotIn("in_plex", cards[0])

    def test_format_shows_plex_badge_with_resolution(self):
        """Formatted /new text must include ✅ 1080 for a Plex-matched card."""
        cache = {
            "updated_at": "2026-05-14 12:00",
            "cards": [{
                "title": "Dune",
                "year": 2021,
                "in_plex": True,
                "plex_resolution": "1080",
                "best_quality": "1080p",
                "best_size": "12 GB",
                "best_seeders": 50,
                "release_count": 2,
            }],
        }
        text = _format_movie_discovery_cache(cache)
        self.assertIn("✅ 1080", text)

    def test_format_shows_plex_badge_without_resolution(self):
        """When plex_resolution is empty, just ✅ appears (no extra text)."""
        cache = {
            "updated_at": "2026-05-14 12:00",
            "cards": [{
                "title": "Dune",
                "year": 2021,
                "in_plex": True,
                "plex_resolution": "",
                "best_quality": "1080p",
                "best_size": "12 GB",
                "best_seeders": 50,
                "release_count": 2,
            }],
        }
        text = _format_movie_discovery_cache(cache)
        self.assertIn("✅", text)
        self.assertNotIn("✅ ", text.split("Dune")[1].split("\n")[0])  # no trailing space+resolution

    def test_format_no_plex_badge_when_not_in_plex(self):
        """Card without Plex match must NOT contain ✅."""
        cache = {
            "updated_at": "2026-05-14 12:00",
            "cards": [{
                "title": "Some Film",
                "year": 2025,
                "in_plex": False,
                "plex_resolution": None,
                "best_quality": "720p",
                "best_size": "4 GB",
                "best_seeders": 5,
                "release_count": 1,
            }],
        }
        text = _format_movie_discovery_cache(cache)
        self.assertNotIn("✅", text)

    def test_format_shows_vote_count_next_to_rating(self):
        """Vote count appears in parentheses right after the KP rating."""
        cache = {
            "updated_at": "2026-05-14 12:00",
            "cards": [{
                "title": "Дюна",
                "year": 2021,
                "rating": 7.8,
                "kp_votes": 125_000,
                "best_quality": "1080p",
                "best_size": "10 GB",
                "best_seeders": 30,
                "release_count": 1,
            }],
        }
        text = _format_movie_discovery_cache(cache)
        self.assertIn("КП 7.8 (125K)", text)

    def test_format_shows_countries_between_year_and_rating(self):
        cache = {
            "updated_at": "2026-05-14 12:00",
            "cards": [{
                "title": "Dune",
                "year": 2021,
                "countries": ["USA", "Canada"],
                "rating": 7.8,
                "best_quality": "1080p",
                "best_size": "10 GB",
                "best_seeders": 30,
                "release_count": 1,
            }],
        }

        text = _format_movie_discovery_cache(cache)
        self.assertIn("2021 · USA, Canada · КП 7.8", text)

    def test_format_no_vote_count_when_votes_is_none(self):
        """When kp_votes is absent, only the rating appears without parentheses."""
        cache = {
            "updated_at": "2026-05-14 12:00",
            "cards": [{
                "title": "Дюна",
                "year": 2021,
                "rating": 7.8,
                "kp_votes": None,
                "best_quality": "1080p",
                "best_size": "10 GB",
                "best_seeders": 30,
                "release_count": 1,
            }],
        }
        text = _format_movie_discovery_cache(cache)
        self.assertIn("КП 7.8", text)
        self.assertNotIn("(", text.split("КП 7.8")[1].split("\n")[0])


class KpVoteFormatterTests(unittest.TestCase):
    """Tests for _format_kp_votes helper."""

    def test_none_returns_empty(self):
        self.assertEqual(_format_kp_votes(None), "")

    def test_zero_returns_empty(self):
        self.assertEqual(_format_kp_votes(0), "")

    def test_small_number_returned_as_is(self):
        self.assertEqual(_format_kp_votes(500), "500")

    def test_thousands_formatted_as_K(self):
        self.assertEqual(_format_kp_votes(125_000), "125K")
        self.assertEqual(_format_kp_votes(1_500), "2K")

    def test_millions_formatted_as_M(self):
        self.assertEqual(_format_kp_votes(1_500_000), "1.5M")
        self.assertEqual(_format_kp_votes(2_000_000), "2.0M")


# ---------------------------------------------------------------------------
# Movie discovery subscription feature
# ---------------------------------------------------------------------------

import bot as _bot_module  # noqa: E402  (needed for monkeypatching settings)


class MovieSubscriptionStorageTests(unittest.TestCase):
    """Unit tests for _get/_is/_set_movie_subscription helpers."""

    def setUp(self):
        # Patch _load/_save to use an in-memory dict
        self._settings: dict = {}
        self._orig_load = _bot_module._load_movie_discovery_settings
        self._orig_save = _bot_module._save_movie_discovery_settings
        _bot_module._load_movie_discovery_settings = lambda: self._settings
        _bot_module._save_movie_discovery_settings = lambda s: self._settings.update(s)

    def tearDown(self):
        _bot_module._load_movie_discovery_settings = self._orig_load
        _bot_module._save_movie_discovery_settings = self._orig_save

    def test_not_subscribed_by_default(self):
        self.assertFalse(_is_movie_subscribed(12345))

    def test_subscribe_adds_entry(self):
        _set_movie_subscription(12345, True)
        self.assertTrue(_is_movie_subscribed(12345))
        subs = _get_movie_subscriptions()
        self.assertIn("12345", subs)
        self.assertIn("subscribed_at", subs["12345"])

    def test_unsubscribe_removes_entry(self):
        _set_movie_subscription(12345, True)
        _set_movie_subscription(12345, False)
        self.assertFalse(_is_movie_subscribed(12345))

    def test_multiple_subscribers_independent(self):
        _set_movie_subscription(100, True)
        _set_movie_subscription(200, True)
        _set_movie_subscription(100, False)
        self.assertFalse(_is_movie_subscribed(100))
        self.assertTrue(_is_movie_subscribed(200))


class MovieSubscriptionKeyboardTests(unittest.TestCase):
    """Tests for subscribe/unsubscribe button in _movie_discovery_keyboard."""

    def _make_cards(self):
        return [{"title": "Тест", "year": 2026}]

    def test_subscribe_button_shown_when_not_subscribed(self):
        import bot as _bot_module
        orig = _bot_module._is_movie_subscribed
        _bot_module._is_movie_subscribed = lambda cid: False
        try:
            kb = _movie_discovery_keyboard(self._make_cards(), chat_id=999)
            buttons = {btn.text: btn.callback_data for row in kb.inline_keyboard for btn in row}
            self.assertIn("🔔 Подписаться на /new", buttons)
            self.assertEqual(buttons["🔔 Подписаться на /new"], "new:subscribe")
        finally:
            _bot_module._is_movie_subscribed = orig

    def test_unsubscribe_button_shown_when_subscribed(self):
        import bot as _bot_module
        orig = _bot_module._is_movie_subscribed
        _bot_module._is_movie_subscribed = lambda cid: True
        try:
            kb = _movie_discovery_keyboard(self._make_cards(), chat_id=999)
            buttons = {btn.text: btn.callback_data for row in kb.inline_keyboard for btn in row}
            self.assertIn("🔕 Отписаться от /new", buttons)
            self.assertEqual(buttons["🔕 Отписаться от /new"], "new:unsubscribe")
        finally:
            _bot_module._is_movie_subscribed = orig

    def test_no_sub_button_when_chat_id_is_none(self):
        kb = _movie_discovery_keyboard(self._make_cards(), chat_id=None)
        buttons = {btn.text for row in kb.inline_keyboard for btn in row}
        # Without chat_id: subscribe defaults to False (not subscribed)
        self.assertIn("🔔 Подписаться на /new", buttons)


class MovieDiscoveryDegradedCacheGuardTests(unittest.TestCase):
    def _cards(self, count: int) -> list[dict]:
        return [{"title": f"Film {idx}", "kp_id": idx} for idx in range(count)]

    def test_keeps_previous_cache_when_degraded_candidate_shrinks(self):
        previous = {
            "cards": self._cards(12),
            "all_releases": [{"title": "old"}],
            "updated_at": "2026-05-30 20:00",
        }
        candidate = {
            "cards": self._cards(5),
            "all_releases": [{"title": "new"}],
            "kp_api_stats": {"date": "2026-05-30", "searches": 3},
            "kp_cache": {"new|2026": {"kp_id": 100}},
        }

        result, rejected = bot._movie_discovery_guard_degraded_cache(
            candidate,
            previous,
            failed_specs=[[2026, "1080p"]],
            failed_enabled=[],
            failed_disabled=[],
            prev_top10_kp_ids=[1, 2, 3],
        )

        self.assertTrue(rejected)
        self.assertEqual(result["cards"], previous["cards"])
        self.assertEqual(result["all_releases"], previous["all_releases"])
        self.assertEqual(result["kp_api_stats"], candidate["kp_api_stats"])
        self.assertEqual(result["kp_cache"], candidate["kp_cache"])
        self.assertEqual(result["last_failed_specs"], [[2026, "1080p"]])
        self.assertTrue(result["last_degraded_refresh"]["rejected"])

    def test_does_not_keep_previous_cache_for_info_only_failures(self):
        previous = {"cards": self._cards(12)}
        candidate = {"cards": self._cards(5)}

        result, rejected = bot._movie_discovery_guard_degraded_cache(
            candidate,
            previous,
            failed_specs=[],
            failed_enabled=[],
            failed_disabled=["noname-club"],
            prev_top10_kp_ids=[1, 2, 3],
        )

        self.assertFalse(rejected)
        self.assertEqual(result["cards"], candidate["cards"])
        self.assertFalse(bot._movie_discovery_cache_has_gating_degradation(result))


class MovieDiscoveryNotificationTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _run_movie_discovery_notifications — per-user seen semantics."""

    def setUp(self):
        self._allowed_patch = unittest.mock.patch("bot.ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def _patch_settings(self, settings: dict):
        import bot as _bot_module
        self._orig_load = _bot_module._load_movie_discovery_settings
        self._orig_save = _bot_module._save_movie_discovery_settings
        _bot_module._load_movie_discovery_settings = lambda: settings
        _bot_module._save_movie_discovery_settings = lambda s: settings.update(s)

    def tearDown(self):
        import bot as _bot_module
        if hasattr(self, "_orig_load"):
            _bot_module._load_movie_discovery_settings = self._orig_load
            _bot_module._save_movie_discovery_settings = self._orig_save
        self._allowed_patch.stop()

    def _make_card(
        self,
        title: str,
        kp_id: int | None = None,
        year: int = 2026,
        poster_preview_url: str = "",
    ) -> dict:
        card = {"title": title, "year": year, "rating": 7.5}
        if kp_id is not None:
            card["kp_id"] = kp_id
        if poster_preview_url:
            card["poster_preview_url"] = poster_preview_url
        return card

    async def test_sends_notification_only_for_unseen_films(self):
        """A subscriber gets push only for cards whose IDs aren't in their seen-set."""
        settings = {
            "movie_subscriptions": {"100": {"subscribed_at": "2026-05-14 11:00"}},
            "movie_seen_by_user": {
                "100": {"kp:1": "2026-05-14 10:00"},
            },
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [
            self._make_card("Старый фильм", kp_id=1),
            self._make_card("Новый фильм", kp_id=2),
        ]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_called_once()
        kwargs = app.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], 100)
        self.assertIn("Новый фильм", kwargs["text"])
        self.assertNotIn("Старый фильм", kwargs["text"])

    async def test_marks_films_as_seen_after_successful_push(self):
        """After successful send, the pushed films' IDs go into the user's seen-set."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("Совсем новый", kp_id=42)]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        seen = settings.get("movie_seen_by_user", {}).get("100", {})
        self.assertIn("kp:42", seen)

    async def test_first_time_subscriber_gets_initial_summary(self):
        """A subscriber with empty seen-set receives all current top-10 as new."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [
            self._make_card("Фильм 1", kp_id=1),
            self._make_card("Фильм 2", kp_id=2),
        ]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        kwargs = app.bot.send_message.call_args.kwargs
        self.assertIn("Фильм 1", kwargs["text"])
        self.assertIn("Фильм 2", kwargs["text"])

    async def test_notification_skips_cards_without_kp_id(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [
            self._make_card("Raw tracker only"),
            self._make_card("KP enriched", kp_id=2),
        ]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        kwargs = app.bot.send_message.call_args.kwargs
        self.assertNotIn("Raw tracker only", kwargs["text"])
        self.assertIn("KP enriched", kwargs["text"])

    async def test_notification_skips_cards_already_in_plex(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [
            self._make_card("Plexed", kp_id=1),
            self._make_card("Fresh", kp_id=2),
        ]}

        def enrich(cards):
            for card in cards:
                if card.get("title") == "Plexed":
                    card["in_plex"] = True

        with (
            unittest.mock.patch("bot._is_in_notification_window", return_value=True),
            unittest.mock.patch("bot._enrich_cards_with_plex", side_effect=enrich),
        ):
            await _run_movie_discovery_notifications(cache, app)
        kwargs = app.bot.send_message.call_args.kwargs
        self.assertNotIn("Plexed", kwargs["text"])
        self.assertIn("Fresh", kwargs["text"])

    async def test_only_top10_cards_are_considered(self):
        """Cards beyond position 10 must NOT trigger notifications."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        # 12 cards — only first 10 are notification candidates
        cards = [self._make_card(f"Старый {i}", kp_id=i) for i in range(10)]
        cards.append(self._make_card("Вне топ10 #1", kp_id=100))
        cards.append(self._make_card("Вне топ10 #2", kp_id=101))
        cache = {"cards": cards}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        kwargs = app.bot.send_message.call_args.kwargs
        # The cards outside top-10 should not be in the message
        self.assertNotIn("Вне топ10", kwargs["text"])

    async def test_no_notification_when_no_subscribers(self):
        settings = {
            "movie_subscriptions": {},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("Новый", kp_id=1)]}
        await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()

    async def test_quiet_hours_queues_push_and_does_not_mark_seen(self):
        """Outside quiet hours we queue a snapshot and do not mark it notified."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("Новый", kp_id=1)]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=False):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()
        # Seen set must remain empty — film will be delivered later.
        self.assertEqual(settings.get("movie_seen_by_user", {}).get("100", {}), {})
        queued = settings.get("movie_notification_pending", {}).get("100", {})
        self.assertEqual([card.get("kp_id") for card in queued.get("cards", [])], [1])

    async def test_queued_quiet_hours_push_delivers_even_if_cache_lost_card(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
            "movie_notification_pending": {
                "100": {"cards": [self._make_card("Ночной", kp_id=7)]},
            },
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": []}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_message.assert_called_once()
        self.assertNotIn("100", settings.get("movie_notification_pending", {}))
        self.assertIn("kp:7", settings.get("movie_seen_by_user", {}).get("100", {}))

    async def test_permanent_delivery_failures_unsubscribe_after_cap(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        app.bot.send_message = AsyncMock(side_effect=bot.Forbidden("blocked"))
        cache = {"cards": [self._make_card("Blocked", kp_id=1)]}

        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            for _ in range(bot._MOVIE_NOTIFICATION_MAX_FAILURES):
                await _run_movie_discovery_notifications(cache, app)

        self.assertNotIn("100", settings.get("movie_subscriptions", {}))
        self.assertEqual(app.bot.send_message.await_count, bot._MOVIE_NOTIFICATION_MAX_FAILURES)

    async def test_notification_selection_loads_settings_once(self):
        settings = {
            "movie_subscriptions": {"100": {}, "200": {}},
            "movie_seen_by_user": {},
        }
        app = AsyncMock()
        cache = {"cards": [self._make_card(f"Movie {i}", kp_id=i) for i in range(1, 6)]}
        with (
            unittest.mock.patch("bot._load_movie_discovery_settings", side_effect=lambda: settings) as load,
            unittest.mock.patch("bot._save_movie_discovery_settings"),
            unittest.mock.patch("bot._is_in_notification_window", return_value=True),
            unittest.mock.patch("bot.ALLOWED_CHAT_IDS", {100, 200}),
            unittest.mock.patch(
                "bot._send_movie_notification_push_to_user_result",
                new=AsyncMock(return_value=(False, "network", False)),
            ),
        ):
            await _run_movie_discovery_notifications(cache, app)

        self.assertEqual(load.call_count, 1)

    async def test_notification_keyboard_has_open_and_unsub_buttons(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("Новый", kp_id=1)]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        keyboard = app.bot.send_message.call_args.kwargs["reply_markup"]
        buttons = {btn.text: btn.callback_data for row in keyboard.inline_keyboard for btn in row}
        self.assertRegex(buttons["🎬 Открыть /new"], r"^new:open:[0-9a-f]{10}$")
        self.assertTrue(buttons["🔕 Отписаться"].endswith(":new_unsub"))

    async def test_notification_uses_top_card_poster_when_available(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        app.bot.send_photo = AsyncMock()
        app.bot.send_message = AsyncMock()
        cache = {"cards": [
            self._make_card("Постерный", kp_id=1, poster_preview_url="https://img.example/poster.jpg"),
            self._make_card("Второй", kp_id=2),
        ]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_photo.assert_awaited_once()
        app.bot.send_message.assert_not_called()
        kwargs = app.bot.send_photo.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], 100)
        self.assertEqual(kwargs["photo"], "https://img.example/poster.jpg")
        self.assertIn("Постерный", kwargs["caption"])
        self.assertIn("Второй", kwargs["caption"])

    async def test_notification_includes_country_when_available(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        card = self._make_card("Country Film", kp_id=1)
        card["countries"] = ["USA"]
        cache = {"cards": [card]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_message.assert_awaited_once()
        self.assertIn("Country Film (2026, USA)", app.bot.send_message.call_args.kwargs["text"])

    async def test_notification_falls_back_to_text_when_poster_send_fails(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        app.bot.send_photo = AsyncMock(side_effect=RuntimeError("photo failed"))
        app.bot.send_message = AsyncMock()
        cache = {"cards": [
            self._make_card("Постерный", kp_id=1, poster_preview_url="https://img.example/poster.jpg"),
        ]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_photo.assert_awaited_once()
        app.bot.send_message.assert_awaited_once()
        self.assertIn("Постерный", app.bot.send_message.call_args.kwargs["text"])

    async def test_different_users_have_independent_seen_sets(self):
        """User A has seen film X (no push); user B hasn't (gets push for X)."""
        settings = {
            "movie_subscriptions": {"100": {}, "200": {}},
            "movie_seen_by_user": {"100": {"kp:42": "old-ts"}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("X", kp_id=42)]}
        with (
            unittest.mock.patch("bot._is_in_notification_window", return_value=True),
            unittest.mock.patch("bot.ALLOWED_CHAT_IDS", {100, 200}),
        ):
            await _run_movie_discovery_notifications(cache, app)
        # Only user 200 gets a push (user 100 already seen)
        recipient_ids = [call.kwargs["chat_id"] for call in app.bot.send_message.await_args_list]
        self.assertEqual(recipient_ids, [200])

    async def test_push_sets_notified_at_but_NOT_shown_at(self):
        """Regression: after a push the film has notified_at but shown_at must
        remain empty — so the user still sees the 🆕 badge when they click
        'Открыть /new' from the push, and can visually locate the film."""
        from bot import _is_card_notified, _is_card_shown_in_new
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        card = self._make_card("Новый", kp_id=42)
        cache = {"cards": [card]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        # Notified yes, shown_at no — badge will remain visible
        with unittest.mock.patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = settings
            self.assertTrue(_is_card_notified(card, 100))
            self.assertFalse(_is_card_shown_in_new(card, 100))

    async def test_notification_push_is_limited_to_three_cards(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card(f"Movie {i}", kp_id=i) for i in range(1, 6)]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Movie 1", text)
        self.assertIn("Movie 3", text)
        self.assertNotIn("Movie 4", text)
        seen = settings.get("movie_seen_by_user", {}).get("100", {})
        self.assertIn("kp:1", seen)
        self.assertIn("kp:3", seen)
        self.assertNotIn("kp:4", seen)

    async def test_notification_push_saves_snapshot_for_download_buttons(self):
        settings = {"movie_seen_by_user": {}}
        self._patch_settings(settings)
        app = AsyncMock()
        card = self._make_card("Snapshot Movie", kp_id=77)
        card["releases"] = [{
            "source": "jackett",
            "tracker": "rutracker",
            "title": "Snapshot Movie 1080p Original",
            "url": "https://rutracker.org/forum/viewtopic.php?t=123",
            "torrent_url": "https://jackett.local/dl/123",
            "size": "2.5 GB",
            "seeders": 10,
        }]

        with (
            patch.object(bot, "_enrich_cards_with_plex"),
            patch.object(bot, "_search_defaults_for_chat", return_value={
                "quality": "1080p",
                "audio": False,
                "subs": False,
                "preferred_voices": [],
            }),
        ):
            sent = await bot._send_movie_notification_push_to_user([card], 100, app)

        self.assertTrue(sent)
        snapshots = settings.get("movie_notification_snapshots", {})
        self.assertEqual(len(snapshots), 1)
        snapshot = next(iter(snapshots.values()))
        self.assertEqual(snapshot["chat_id"], "100")
        self.assertEqual(snapshot["items"][0]["result"]["title"], "Snapshot Movie 1080p Original")
        keyboard = app.bot.send_message.call_args.kwargs["reply_markup"]
        buttons = {btn.text: btn.callback_data for row in keyboard.inline_keyboard for btn in row}
        self.assertIn("⬇️ 1", buttons)

    async def test_already_shown_in_new_blocks_push(self):
        """If a user opened /new and saw the film, no push is sent (subscriber's
        time isn't wasted on a redundant notification)."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {"100": {"kp:42": {"shown_at": "ts", "notified_at": None}}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("X", kp_id=42)]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()

    # ---- False-push protection: A (skip on first refresh after startup) ----

    async def test_skip_push_true_suppresses_notification_entirely(self):
        """Layer A: caller passes skip_push=True on the very first refresh
        after startup (cold Jackett). Cache is fully populated but no push
        fires — we wait for the next refresh to reconfirm stability."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("Транзиентный", kp_id=99)]}
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app, skip_push=True)
        app.bot.send_message.assert_not_called()

    async def test_skip_push_allows_confirmed_cards_when_prev_top10_exists(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "cards": [
                self._make_card("Confirmed", kp_id=1),
                self._make_card("Transient", kp_id=99),
            ],
            "prev_top10_kp_ids": [1],
        }

        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app, skip_push=True)

        kwargs = app.bot.send_message.call_args.kwargs
        self.assertIn("Confirmed", kwargs["text"])
        self.assertNotIn("Transient", kwargs["text"])

    async def test_degraded_refresh_signal_suppresses_notification_entirely(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "cards": [self._make_card("Preserved", kp_id=42)],
            "last_failed_specs": [[2026, "1080p"]],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()

    async def test_info_only_indexer_failure_does_not_suppress_notification(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "cards": [self._make_card("New", kp_id=42)],
            "last_failed_indexer_ids_disabled": ["noname-club"],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_called_once()

    # ---- False-push protection: B (regression guard) ----

    async def test_regression_above_60pct_skips_push(self):
        """Layer B: if removed_pct from prev top-10 exceeds 60%, the whole
        push cycle is skipped (likely an unstable refresh — Jackett
        warm-up carried over, or a source briefly outaged)."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        # Prev top-10 had kp_ids 1..10; current has only 1, 2, 3, 11, 12, 13
        # → 7 of 10 prev are missing = 70% removed.
        cache = {
            "cards": [self._make_card(str(i), kp_id=i) for i in (1, 2, 3, 11, 12, 13)],
            "prev_top10_kp_ids": list(range(1, 11)),
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()

    async def test_regression_at_50pct_still_pushes(self):
        """Below the 60% threshold the push proceeds (consensus then filters
        individual kp_ids — but the cycle as a whole isn't blocked)."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        # Prev top-10 = [1..10], current = [1..5, 11..15] — 5/10 = 50% removed.
        cache = {
            "cards": [self._make_card(str(i), kp_id=i) for i in [1, 2, 3, 4, 5, 11, 12, 13, 14, 15]],
            "prev_top10_kp_ids": list(range(1, 11)),
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        # At least one push went out (for kp_ids 1..5 which are in both top-10s).
        self.assertTrue(app.bot.send_message.called)

    # ---- False-push protection: C-lite (consensus filter) ----

    async def test_consensus_blocks_kp_id_not_in_prev_top10(self):
        """Layer C-lite: a kp_id present in the current top-10 but NOT in the
        previous top-10 is suppressed — wait one more cycle for confirmation
        before pushing. This is what would have blocked the «Грехи Запада»
        false push observed in the cold-Jackett bug.

        Prev top-10 = [1, 2, 3, 4]. Current top-10 = [1, 2, 3, 99].
        Common = 3/4 → only 25% removed → regression guard passes.
        kp=99 is new (not in prev) → consensus blocks it.
        kp=1,2,3 are confirmed → eligible for push.
        """
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "cards": [
                self._make_card("Confirmed-A", kp_id=1),
                self._make_card("Confirmed-B", kp_id=2),
                self._make_card("Confirmed-C", kp_id=3),
                self._make_card("Transient", kp_id=99),
            ],
            "prev_top10_kp_ids": [1, 2, 3, 4],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        kwargs = app.bot.send_message.call_args.kwargs
        self.assertIn("Confirmed-A", kwargs["text"])
        self.assertNotIn("Transient", kwargs["text"])

    async def test_consensus_only_card_is_not_pushed_or_marked_seen(self):
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "cards": [self._make_card("Transient", kp_id=99)],
            "prev_top10_kp_ids": [1, 2, 3],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_message.assert_not_called()
        self.assertEqual(settings.get("movie_seen_by_user", {}).get("100", {}), {})

    async def test_consensus_disabled_when_no_prev_top10(self):
        """If `prev_top10_kp_ids` is absent (e.g. first refresh ever, fresh
        install), the consensus filter is bypassed — otherwise the very first
        refresh on a fresh cache could never push anything."""
        settings = {
            "movie_subscriptions": {"100": {}},
            "movie_seen_by_user": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {"cards": [self._make_card("Новый", kp_id=1)]}  # no prev_top10_kp_ids
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_called_once()


class RestoreFirstSeenFromPreviousTests(unittest.TestCase):
    """Tests for _restore_first_seen_from_previous — keeps the original first_seen_at
    when a card already existed in the previous cache, even if its key changed
    because KP enrichment status flipped between refreshes."""

    def test_matches_by_exact_key(self):
        from bot import _restore_first_seen_from_previous
        previous = [{
            "key": "kp:777", "title": "Dune", "year": 2024,
            "first_seen_at": "2026-05-01 10:00",
        }]
        new_cards = [{
            "key": "kp:777", "title": "Dune", "year": 2024,
            "first_seen_at": "2026-05-15 12:00",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-05-01 10:00")

    def test_matches_by_title_year_when_key_flips_kp_resolved(self):
        """Real-world regression: card was keyed 'movie_key(...)' last refresh
        (KP not yet resolved), now keyed 'kp:N' because KP came back. Must still
        recognise it's the same film and preserve first_seen_at."""
        from bot import _restore_first_seen_from_previous
        previous = [{
            "key": "2026:project hail mary",   # was: movie_key("Project Hail Mary", 2026)
            "title": "Project Hail Mary",
            "year": 2026,
            "first_seen_at": "2026-05-01 10:00",
        }]
        new_cards = [{
            "key": "kp:12345",                  # now KP-enriched
            "title": "Project Hail Mary",
            "year": 2026,
            "first_seen_at": "2026-05-15 18:05",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-05-01 10:00")

    def test_matches_by_title_year_when_kp_lost(self):
        """Reverse direction: previously KP-resolved, now KP cache missed."""
        from bot import _restore_first_seen_from_previous
        previous = [{
            "key": "kp:777",
            "title": "Dune: Part Two", "year": 2024,
            "first_seen_at": "2026-04-01 10:00",
        }]
        new_cards = [{
            "key": "2024:dune part two",
            "title": "Dune: Part Two", "year": 2024,
            "first_seen_at": "2026-05-15 12:00",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-04-01 10:00")

    def test_keeps_now_when_film_is_actually_new(self):
        """A genuinely new film (not in previous cache) must keep its now_text stamp."""
        from bot import _restore_first_seen_from_previous
        previous = [{
            "key": "kp:111", "title": "Old Film", "year": 2020,
            "first_seen_at": "2026-04-01 10:00",
        }]
        new_cards = [{
            "key": "kp:999", "title": "Brand New", "year": 2026,
            "first_seen_at": "2026-05-15 12:00",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-05-15 12:00")

    def test_picks_earliest_when_duplicate_title_year_in_previous(self):
        """If two previous cards collide on (title, year) we keep the older stamp."""
        from bot import _restore_first_seen_from_previous
        previous = [
            {"key": "k1", "title": "Foo", "year": 2024, "first_seen_at": "2026-05-10 10:00"},
            {"key": "k2", "title": "Foo", "year": 2024, "first_seen_at": "2026-04-01 10:00"},
        ]
        new_cards = [{
            "key": "kp:999", "title": "Foo", "year": 2024,
            "first_seen_at": "2026-05-15 12:00",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-04-01 10:00")

    def test_handles_empty_previous(self):
        from bot import _restore_first_seen_from_previous
        new_cards = [{
            "key": "k1", "title": "X", "year": 2020,
            "first_seen_at": "2026-05-15 12:00",
        }]
        _restore_first_seen_from_previous(new_cards, [])
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-05-15 12:00")

    def test_matches_by_kp_id_when_title_was_overwritten_by_kp(self):
        """KP can overwrite the raw release title with the canonical name on next
        refresh (e.g. 'Project Hail Mary' → 'Проект «Конец света»'). The card's
        kp_id stays the same — match by it."""
        from bot import _restore_first_seen_from_previous
        previous = [{
            "key": "kp:12345",
            "kp_id": 12345,
            "title": "Project Hail Mary",  # raw English title from release
            "year": 2026,
            "first_seen_at": "2026-05-01 10:00",
        }]
        new_cards = [{
            "key": "kp:12345",
            "kp_id": 12345,
            "title": "Проект «Конец света»",  # canonical RU title from KP
            "year": 2026,
            "first_seen_at": "2026-05-15 18:05",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-05-01 10:00")

    def test_matches_via_alt_title_when_primary_title_changed(self):
        """If KP overwrote 'title' but 'alt_title' still holds the original name,
        the (alt_title, year) bucket should catch the match."""
        from bot import _restore_first_seen_from_previous
        previous = [{
            "key": "1",
            "title": "Project Hail Mary",
            "year": 2026,
            "first_seen_at": "2026-05-01 10:00",
        }]
        new_cards = [{
            "key": "2",   # different key, no kp_id either
            "title": "Проект Конец света",
            "alt_title": "Project Hail Mary",  # original English preserved here
            "year": 2026,
            "first_seen_at": "2026-05-15 18:05",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-05-01 10:00")

    def test_kp_id_priority_over_title_collision(self):
        """If two different films collide on title (unlikely but possible),
        kp_id wins — it's a stronger identifier than the title."""
        from bot import _restore_first_seen_from_previous
        previous = [
            {"key": "kp:1", "kp_id": 1, "title": "Foo", "year": 2024,
             "first_seen_at": "2026-03-01 10:00"},
            {"key": "kp:2", "kp_id": 2, "title": "Foo", "year": 2024,
             "first_seen_at": "2026-04-01 10:00"},
        ]
        new_cards = [{
            "key": "kp:2",
            "kp_id": 2,
            "title": "Foo Renamed",
            "year": 2024,
            "first_seen_at": "2026-05-15 12:00",
        }]
        _restore_first_seen_from_previous(new_cards, previous)
        # Matches kp_id=2, not the title-bucket which collides with kp:1 too.
        self.assertEqual(new_cards[0]["first_seen_at"], "2026-04-01 10:00")


class PlexUnmatchedSettingsTests(unittest.TestCase):
    """Tests for the runtime toggle + persisted 'seen' set used by the admin
    Plex-unmatched radar."""

    def _isolated_settings(self) -> dict:
        return {}

    def test_toggle_defaults_to_false(self):
        from bot import _is_plex_unmatched_notify_enabled
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = self._isolated_settings()
            self.assertFalse(_is_plex_unmatched_notify_enabled())

    def test_toggle_round_trip(self):
        from bot import _is_plex_unmatched_notify_enabled, _set_plex_unmatched_notify_enabled
        store: dict = {}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _set_plex_unmatched_notify_enabled(True)
            self.assertTrue(_is_plex_unmatched_notify_enabled())
            _set_plex_unmatched_notify_enabled(False)
            self.assertFalse(_is_plex_unmatched_notify_enabled())

    def test_seen_round_trip_sorts_and_dedupes(self):
        from bot import _load_plex_unmatched_seen, _save_plex_unmatched_seen
        store: dict = {}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _save_plex_unmatched_seen({
                "movies": ["k3", "k1", "k1", "k2"],
                "shows":  ["s2", "s1", "s2"],
            })
            seen = _load_plex_unmatched_seen()
        self.assertEqual(seen["movies"], ["k1", "k2", "k3"])
        self.assertEqual(seen["shows"], ["s1", "s2"])

    def test_seen_returns_empty_lists_when_unset(self):
        from bot import _load_plex_unmatched_seen
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {}
            seen = _load_plex_unmatched_seen()
        self.assertEqual(seen, {"movies": [], "shows": []})

    def test_seen_save_does_not_clobber_unrelated_settings(self):
        """Verify _save_plex_unmatched_seen preserves other fields in settings."""
        from bot import _save_plex_unmatched_seen
        store = {"movie_subscriptions": {"100": {"x": 1}}, "movie_notify_last_run_at": "2026-05-15"}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _save_plex_unmatched_seen({"movies": ["k1"], "shows": []})
        # Other fields untouched
        self.assertEqual(store["movie_subscriptions"], {"100": {"x": 1}})
        self.assertEqual(store["movie_notify_last_run_at"], "2026-05-15")
        # New field added
        self.assertEqual(store["plex_unmatched_seen"], {"movies": ["k1"], "shows": []})


class PlexUnmatchedDetectionTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _check_plex_unmatched_against_seen — diff-based push logic."""

    def _make_movie(self, key: str, guid: str = "local://x") -> "object":
        from plex import PlexMovie
        return PlexMovie(title="M", year=2024, rating_key=key,
                         resolution="1080", added_at=0, file_paths=[f"/m/{key}.mkv"], guid=guid)

    def _make_show(self, key: str, guid: str = "local://s") -> "object":
        from plex import PlexShow
        return PlexShow(title="S", year=2024, rating_key=key, seasons={}, guid=guid)

    async def test_first_enable_sends_initial_summary(self):
        """Empty seen + non-empty unmatched + toggle ON → initial-kind push."""
        from bot import _check_plex_unmatched_against_seen
        store: dict = {"plex_unmatched_notify_enabled": True}
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {("m", 2024): self._make_movie("m1")}),
            patch.object(bot, "_plex_shows_library", {}),
            patch.object(bot, "ADMIN_CHAT_IDS", {500}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            await _check_plex_unmatched_against_seen(app=fake_app)
            # Spawned tasks run on the event loop; wait one tick
            await asyncio.sleep(0)

        fake_app.bot.send_message.assert_awaited()
        text = fake_app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Включены уведомления", text)

    async def test_no_push_when_toggle_off(self):
        from bot import _check_plex_unmatched_against_seen
        store: dict = {"plex_unmatched_notify_enabled": False}
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {("m", 2024): self._make_movie("m1")}),
            patch.object(bot, "_plex_shows_library", {}),
            patch.object(bot, "ADMIN_CHAT_IDS", {500}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            await _check_plex_unmatched_against_seen(app=fake_app)
            await asyncio.sleep(0)

        fake_app.bot.send_message.assert_not_called()
        # But the snapshot is still saved (so off→on later doesn't dump everything)
        self.assertEqual(store["plex_unmatched_seen"]["movies"], ["m1"])

    async def test_only_new_files_trigger_push(self):
        """Already-seen files in snapshot must NOT re-trigger push.
        Only the diff (current - seen) goes out."""
        from bot import _check_plex_unmatched_against_seen
        store: dict = {
            "plex_unmatched_notify_enabled": True,
            "plex_unmatched_seen": {"movies": ["m1", "m2"], "shows": []},
        }
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {
                ("a", 2024): self._make_movie("m1"),
                ("b", 2024): self._make_movie("m2"),
                ("c", 2024): self._make_movie("m3"),  # this one is new
            }),
            patch.object(bot, "_plex_shows_library", {}),
            patch.object(bot, "ADMIN_CHAT_IDS", {500}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            await _check_plex_unmatched_against_seen(app=fake_app)
            await asyncio.sleep(0)

        fake_app.bot.send_message.assert_awaited()
        text = fake_app.bot.send_message.call_args.kwargs["text"]
        # The message should reference '1' new file, not 3
        self.assertIn("новые несматченные файлы (1)", text)
        # Body contains the new file, not the previously-seen ones
        self.assertIn("m3.mkv", text)
        self.assertNotIn("m1.mkv", text)

    async def test_no_diff_means_no_push(self):
        """Current == seen → silent run, no message."""
        from bot import _check_plex_unmatched_against_seen
        store: dict = {
            "plex_unmatched_notify_enabled": True,
            "plex_unmatched_seen": {"movies": ["m1"], "shows": []},
        }
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {("a", 2024): self._make_movie("m1")}),
            patch.object(bot, "_plex_shows_library", {}),
            patch.object(bot, "ADMIN_CHAT_IDS", {500}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            await _check_plex_unmatched_against_seen(app=fake_app)
            await asyncio.sleep(0)

        fake_app.bot.send_message.assert_not_called()

    async def test_seen_snapshot_updated_even_when_toggle_off(self):
        """Off→on later must not flood the user — relies on snapshot accruing
        in the background regardless of toggle state."""
        from bot import _check_plex_unmatched_against_seen, _load_plex_unmatched_seen
        store: dict = {"plex_unmatched_notify_enabled": False}
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {
                ("a", 2024): self._make_movie("m1"),
                ("b", 2024): self._make_movie("m2"),
            }),
            patch.object(bot, "_plex_shows_library", {}),
            patch.object(bot, "ADMIN_CHAT_IDS", {500}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            await _check_plex_unmatched_against_seen(app=fake_app)

        self.assertEqual(set(store["plex_unmatched_seen"]["movies"]), {"m1", "m2"})

    async def test_no_app_means_silent_but_snapshot_still_saved(self):
        """When called without an app reference (early in startup), updates the
        snapshot but doesn't try to push (push would crash on None.bot)."""
        from bot import _check_plex_unmatched_against_seen
        store: dict = {"plex_unmatched_notify_enabled": True}
        with (
            patch.object(bot, "_plex_library", {("a", 2024): self._make_movie("m1")}),
            patch.object(bot, "_plex_shows_library", {}),
            patch.object(bot, "ADMIN_CHAT_IDS", {500}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            await _check_plex_unmatched_against_seen(app=None)
            await asyncio.sleep(0)

        # No crash + snapshot saved
        self.assertEqual(store["plex_unmatched_seen"]["movies"], ["m1"])


class PlexUnmatchedFormattingTests(unittest.TestCase):
    """Tests for _format_unmatched_push — push message formatting."""

    def _make_movie(self, filename: str) -> "object":
        from plex import PlexMovie
        return PlexMovie(title="", year=0, rating_key="r", resolution="",
                         added_at=0, file_paths=[f"/movies/{filename}"], guid="local://r")

    def test_initial_kind_has_specific_header(self):
        from bot import _format_unmatched_push
        text = _format_unmatched_push([self._make_movie("X.mkv")], [], kind="initial")
        self.assertIn("Включены уведомления", text)
        self.assertIn("1 файл", text)

    def test_new_kind_has_specific_header(self):
        from bot import _format_unmatched_push
        text = _format_unmatched_push([self._make_movie("X.mkv")], [], kind="new")
        self.assertIn("появились новые несматченные", text)

    def test_truncates_to_five_with_overflow_count(self):
        from bot import _format_unmatched_push
        movies = [self._make_movie(f"file{i}.mkv") for i in range(8)]
        text = _format_unmatched_push(movies, [], kind="new")
        # First 5 listed, then "…и ещё 3"
        for i in range(5):
            self.assertIn(f"file{i}.mkv", text)
        self.assertIn("ещё 3", text)
        # The 6th–8th should NOT appear inline
        self.assertNotIn("file5.mkv", text)

    def test_shows_section_only_when_shows_present(self):
        from bot import _format_unmatched_push
        text_movies_only = _format_unmatched_push([self._make_movie("X.mkv")], [], kind="new")
        self.assertNotIn("📺", text_movies_only)
        self.assertIn("🎬", text_movies_only)

    def test_push_links_file_to_plex_web_details(self):
        from bot import _format_unmatched_push
        with patch.object(bot, "_plex_machine_id", "machine-1"):
            text = _format_unmatched_push([self._make_movie("X.mkv")], [], kind="new")

        self.assertIn('<a href="https://app.plex.tv/desktop/#!/server/machine-1/details?key=%2Flibrary%2Fmetadata%2Fr">X.mkv</a>', text)


class FormatUnmatchedListTests(unittest.TestCase):
    """Tests for _format_unmatched_list (admin /admin → 📋 Несматчено screen)."""

    def _make_movie(self, filename: str) -> "object":
        from plex import PlexMovie
        return PlexMovie(title="", year=0, rating_key="r", resolution="",
                         added_at=0, file_paths=[f"/movies/{filename}"], guid="local://r")

    def test_empty_list_shows_clean_confirmation(self):
        from bot import _format_unmatched_list
        text = _format_unmatched_list([], [])
        self.assertIn("Все файлы Plex успешно сматчены", text)

    def test_truncates_to_25_with_overflow_count(self):
        from bot import _format_unmatched_list
        movies = [self._make_movie(f"file{i}.mkv") for i in range(30)]
        text = _format_unmatched_list(movies, [])
        # Last item that fits
        self.assertIn("file24.mkv", text)
        self.assertIn("ещё 5", text)
        # The 26th+ should not appear inline
        self.assertNotIn("file25.mkv", text)

    def test_list_links_file_to_plex_web_details(self):
        from bot import _format_unmatched_list
        with patch.object(bot, "_plex_machine_id", "machine-1"):
            text = _format_unmatched_list([self._make_movie("unmatched.mkv")], [])

        self.assertIn('<a href="https://app.plex.tv/desktop/#!/server/machine-1/details?key=%2Flibrary%2Fmetadata%2Fr">unmatched.mkv</a>', text)


class AdminPlexUnmatchedCallbackTests(unittest.IsolatedAsyncioTestCase):
    """Tests for admin:plex_unmatched and admin:plex_unmatched_toggle callbacks."""

    async def test_toggle_flips_state_and_renders_panel(self):
        from bot import admin_callback
        update = _make_callback_update(chat_id=300, callback_data="admin:plex_unmatched_toggle")
        context = _make_context()

        store: dict = {"plex_unmatched_notify_enabled": False}
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "ALLOWED_CHAT_IDS", {300}),
            patch.object(bot, "_build_admin_panel_text", AsyncMock(return_value="panel")),
            patch.object(bot, "_get_plex_unmatched_counts", return_value={"movies": 0, "shows": 0, "total": 0}),
            patch("bot.state_store") as st,
        ):
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            st.load_approved_chat_ids.return_value = set()
            await admin_callback(update, context)

        # State flipped
        self.assertTrue(store["plex_unmatched_notify_enabled"])
        # Pop-up text confirms the toggle; admin_callback should answer once.
        self.assertEqual(update.callback_query.answer.call_count, 1)
        confirmations = [
            call.args[0] for call in update.callback_query.answer.call_args_list
            if call.args and "Уведомления" in call.args[0]
        ]
        self.assertEqual(len(confirmations), 1, f"expected one toggle-confirmation answer; got {confirmations}")
        # Panel re-rendered
        update.callback_query.edit_message_text.assert_called()

    async def test_unmatched_list_screen_shows_files(self):
        from bot import admin_callback
        from plex import PlexMovie
        update = _make_callback_update(chat_id=300, callback_data="admin:plex_unmatched")
        context = _make_context()
        movie = PlexMovie(title="X", year=2024, rating_key="r1", resolution="",
                          added_at=0, file_paths=["/m/unmatched.mkv"], guid="local://r1")

        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "ADMIN_CHAT_IDS", {300}),
            patch.object(bot, "ALLOWED_CHAT_IDS", {300}),
            patch.object(bot, "_plex_library", {("x", 2024): movie}),
            patch.object(bot, "_plex_shows_library", {}),
            patch("bot.state_store") as st,
        ):
            st.load_approved_chat_ids.return_value = set()
            await admin_callback(update, context)

        update.callback_query.edit_message_text.assert_called()
        call_args = update.callback_query.edit_message_text.call_args
        # Could be positional or keyword arg — check both
        text = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("unmatched.mkv", text)


class MovieSeenByUserHelpersTests(unittest.TestCase):
    """Tests for the per-user dual-signal tracking (notified_at + shown_at)
    used by /new notifications and the 🆕 badge."""

    # --- _card_identifiers (unchanged) ---

    def test_card_identifiers_includes_both_kp_and_movie_key(self):
        from bot import _card_identifiers
        ids = _card_identifiers({"kp_id": 12345, "title": "Dune", "year": 2021})
        self.assertIn("kp:12345", ids)
        self.assertEqual(len(ids), 2)
        self.assertTrue(any(i.startswith("2021:") for i in ids))

    def test_card_identifiers_include_key_and_alt_title(self):
        from bot import _card_identifiers
        ids = _card_identifiers({
            "key": "2026:raw title",
            "kp_id": 12345,
            "title": "Canonical Title",
            "alt_title": "Raw Title",
            "year": 2026,
        })
        self.assertIn("2026:raw title", ids)
        self.assertIn("kp:12345", ids)
        self.assertIn("2026:canonical title", ids)
        self.assertEqual(len(ids), 3)

    def test_card_identifiers_only_movie_key_when_no_kp(self):
        from bot import _card_identifiers
        ids = _card_identifiers({"title": "Dune", "year": 2021})
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids[0].startswith("2021:"))

    def test_card_identifiers_empty_when_no_title_no_kp(self):
        from bot import _card_identifiers
        self.assertEqual(_card_identifiers({}), [])

    # --- legacy-aware entry inspectors ---

    def test_entry_is_notified_handles_dict_and_legacy_string(self):
        from bot import _entry_is_notified
        self.assertTrue(_entry_is_notified({"notified_at": "2026-05-17 10:00"}))
        self.assertFalse(_entry_is_notified({"notified_at": None, "shown_at": "ts"}))
        # Legacy plain-string entry counts as notified
        self.assertTrue(_entry_is_notified("2026-05-01 10:00"))
        self.assertFalse(_entry_is_notified(None))
        self.assertFalse(_entry_is_notified({}))

    def test_entry_is_shown_in_new_handles_dict_and_legacy_string(self):
        from bot import _entry_is_shown_in_new
        self.assertTrue(_entry_is_shown_in_new({"shown_at": "2026-05-17 10:00"}))
        self.assertFalse(_entry_is_shown_in_new({"notified_at": "ts", "shown_at": None}))
        # Legacy entries are treated as fully shown — no false 🆕 wave after migration
        self.assertTrue(_entry_is_shown_in_new("2026-05-01 10:00"))
        self.assertFalse(_entry_is_shown_in_new(None))
        self.assertFalse(_entry_is_shown_in_new({}))

    # --- _is_card_notified / _is_card_shown_in_new ---

    def test_is_card_notified_finds_any_identifier(self):
        from bot import _is_card_notified
        store = {"movie_seen_by_user": {"100": {"kp:777": {"notified_at": "ts"}}}}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = store
            self.assertTrue(_is_card_notified({"kp_id": 777, "title": "X", "year": 2024}, 100))
            # Different card → not notified
            self.assertFalse(_is_card_notified({"kp_id": 999, "title": "Y", "year": 2024}, 100))

    def test_is_card_notified_matches_via_title_when_kp_id_missing(self):
        """KP-flip: previously stored under movie_key, now card has kp_id."""
        from bot import _is_card_notified
        store = {"movie_seen_by_user": {"100": {"2024:x": {"notified_at": "ts"}}}}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = store
            self.assertTrue(_is_card_notified({"kp_id": 777, "title": "X", "year": 2024}, 100))

    def test_is_card_notified_matches_original_key_after_title_changes(self):
        from bot import _is_card_notified
        store = {"movie_seen_by_user": {"100": {"2026:raw title": {"notified_at": "ts"}}}}
        card = {
            "key": "2026:raw title",
            "kp_id": 777,
            "title": "Canonical Title",
            "year": 2026,
        }
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = store
            self.assertTrue(_is_card_notified(card, 100))

    def test_is_card_shown_in_new_distinguishes_from_notified(self):
        """Card with only notified_at must NOT count as shown."""
        from bot import _is_card_notified, _is_card_shown_in_new
        store = {"movie_seen_by_user": {"100": {"kp:777": {"notified_at": "ts"}}}}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = store
            card = {"kp_id": 777, "title": "X", "year": 2024}
            self.assertTrue(_is_card_notified(card, 100))
            self.assertFalse(_is_card_shown_in_new(card, 100))

    def test_legacy_string_entry_satisfies_both_checks(self):
        """Old single-timestamp entries from previous code version act as full seen."""
        from bot import _is_card_notified, _is_card_shown_in_new
        store = {"movie_seen_by_user": {"100": {"kp:777": "2026-05-01 10:00"}}}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = store
            card = {"kp_id": 777, "title": "X", "year": 2024}
            self.assertTrue(_is_card_notified(card, 100))
            self.assertTrue(_is_card_shown_in_new(card, 100))

    # --- _mark_user_notified / _mark_user_shown_in_new ---

    def test_mark_user_notified_sets_only_notified_at(self):
        from bot import _mark_user_notified
        store: dict = {}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _mark_user_notified(100, [{"kp_id": 777, "title": "X", "year": 2024}])
        entry = store["movie_seen_by_user"]["100"]["kp:777"]
        self.assertIsInstance(entry, dict)
        self.assertTrue(entry.get("notified_at"))
        # shown_at remains unset
        self.assertIsNone(entry.get("shown_at"))

    def test_mark_user_shown_in_new_sets_only_shown_at(self):
        from bot import _mark_user_shown_in_new
        store: dict = {}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _mark_user_shown_in_new(100, [{"kp_id": 777, "title": "X", "year": 2024}])
        entry = store["movie_seen_by_user"]["100"]["kp:777"]
        self.assertTrue(entry.get("shown_at"))
        self.assertIsNone(entry.get("notified_at"))

    def test_mark_user_signals_preserve_each_other(self):
        """Setting notified_at must not clobber existing shown_at and vice versa."""
        from bot import _mark_user_notified, _mark_user_shown_in_new
        store: dict = {}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _mark_user_notified(100, [{"kp_id": 777, "title": "X", "year": 2024}])
            _mark_user_shown_in_new(100, [{"kp_id": 777, "title": "X", "year": 2024}])
        entry = store["movie_seen_by_user"]["100"]["kp:777"]
        self.assertTrue(entry.get("notified_at"))
        self.assertTrue(entry.get("shown_at"))

    def test_mark_persists_all_card_identifiers(self):
        """Both 'kp:N' and 'year:title' identifiers are persisted so a later KP-flip
        still finds a match."""
        from bot import _mark_user_notified
        store: dict = {}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _mark_user_notified(100, [
                {"kp_id": 777, "title": "Dune", "year": 2021},
                {"title": "Foo", "year": 2024},
            ])
        seen = store["movie_seen_by_user"]["100"]
        # kp:777, 2021:dune, 2024:foo
        self.assertEqual(len(seen), 3)
        self.assertIn("kp:777", seen)
        self.assertIn("2021:dune", seen)
        self.assertIn("2024:foo", seen)

    def test_mark_preserves_other_users(self):
        from bot import _mark_user_notified
        store = {"movie_seen_by_user": {"999": {"kp:1": {"notified_at": "old"}}}}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _mark_user_notified(100, [{"kp_id": 5, "title": "X", "year": 2024}])
        self.assertIn("kp:1", store["movie_seen_by_user"]["999"])
        self.assertIn("kp:5", store["movie_seen_by_user"]["100"])

    def test_mark_upgrades_legacy_string_entry_to_dict(self):
        """A pre-existing plain-timestamp entry must be promoted to the dict format
        when the new helpers touch it. The other signal also gets backfilled from
        the legacy timestamp so previously-seen films aren't reset to 🆕."""
        from bot import _mark_user_shown_in_new
        store = {"movie_seen_by_user": {"100": {"kp:777": "2026-05-01 10:00"}}}
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.side_effect = lambda: dict(store)
            st.save_movie_discovery_settings.side_effect = store.update
            _mark_user_shown_in_new(100, [{"kp_id": 777, "title": "X", "year": 2024}])
        entry = store["movie_seen_by_user"]["100"]["kp:777"]
        self.assertIsInstance(entry, dict)
        # Legacy timestamp preserved as notified_at; new shown_at written
        self.assertEqual(entry.get("notified_at"), "2026-05-01 10:00")
        self.assertTrue(entry.get("shown_at"))

    def test_prune_movie_seen_keeps_previous_calendar_year_until_year_end(self):
        from bot import _prune_movie_seen_by_user
        settings = {
            "movie_seen_by_user": {
                "100": {
                    "kp:prev": {"shown_at": "2025-01-01 00:00"},
                    "kp:current": {"notified_at": "2026-06-01 12:00"},
                    "kp:old": {"handled_at": "2024-12-31 23:59"},
                }
            }
        }

        changed = _prune_movie_seen_by_user(settings, now=datetime(2026, 12, 31, 23, 59))

        self.assertTrue(changed)
        seen = settings["movie_seen_by_user"]["100"]
        self.assertIn("kp:prev", seen)
        self.assertIn("kp:current", seen)
        self.assertNotIn("kp:old", seen)

    def test_prune_movie_seen_preserves_unparseable_and_prunes_legacy_old(self):
        from bot import _prune_movie_seen_by_user
        settings = {
            "movie_seen_by_user": {
                "100": {
                    "kp:legacy_prev": "2025-01-01 00:00",
                    "kp:legacy_old": "2024-12-31 23:59",
                    "kp:unknown": "old-ts",
                }
            }
        }

        changed = _prune_movie_seen_by_user(settings, now=datetime(2026, 12, 31, 23, 59))

        self.assertTrue(changed)
        seen = settings["movie_seen_by_user"]["100"]
        self.assertIn("kp:legacy_prev", seen)
        self.assertIn("kp:unknown", seen)
        self.assertNotIn("kp:legacy_old", seen)


class FormatMovieDiscoveryCachePerUserBadgeTests(unittest.TestCase):
    """Tests for the per-user 🆕 badge in _format_movie_discovery_cache."""

    def _cache(self, *cards) -> dict:
        return {"updated_at": "2026-05-16 12:00", "cards": list(cards)}

    def _card(self, title: str, kp_id: int | None = None) -> dict:
        c = {"title": title, "year": 2026, "rating": 7.5}
        if kp_id is not None:
            c["kp_id"] = kp_id
        return c

    def test_badge_shown_for_unseen_film_with_chat_id(self):
        from bot import _format_movie_discovery_cache
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {"movie_seen_by_user": {}}
            text = _format_movie_discovery_cache(self._cache(self._card("Новый", kp_id=1)), chat_id=100)
        self.assertIn("🆕", text)
        self.assertIn("Новый", text)

    def test_badge_hidden_for_seen_film(self):
        from bot import _format_movie_discovery_cache
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {
                "movie_seen_by_user": {"100": {"kp:1": "old-ts"}}
            }
            text = _format_movie_discovery_cache(self._cache(self._card("Виденный", kp_id=1)), chat_id=100)
        self.assertNotIn("🆕", text)
        self.assertIn("Виденный", text)

    def test_per_user_independence(self):
        """User A has seen the film, user B hasn't — different badge state."""
        from bot import _format_movie_discovery_cache
        cache = self._cache(self._card("Фильм", kp_id=42))
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {
                "movie_seen_by_user": {"100": {"kp:42": "ts"}}
            }
            text_user_100 = _format_movie_discovery_cache(cache, chat_id=100)
            text_user_200 = _format_movie_discovery_cache(cache, chat_id=200)
        self.assertNotIn("🆕", text_user_100)
        self.assertIn("🆕", text_user_200)

    def test_no_badge_without_chat_id(self):
        """When called without chat_id (system render), no badge is added."""
        from bot import _format_movie_discovery_cache
        text = _format_movie_discovery_cache(self._cache(self._card("X", kp_id=1)))
        self.assertNotIn("🆕", text)

    def test_badge_remains_after_push_only(self):
        """Regression: a film that was pushed but not yet opened in /new must
        STILL show the 🆕 badge — so the user can locate it visually when
        they click 'Открыть /new' from the push."""
        from bot import _format_movie_discovery_cache
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {
                "movie_seen_by_user": {
                    "100": {"kp:1": {"notified_at": "ts", "shown_at": None}}
                }
            }
            text = _format_movie_discovery_cache(self._cache(self._card("Pushed", kp_id=1)), chat_id=100)
        # Badge stays because shown_at is empty
        self.assertIn("🆕", text)
        self.assertIn("Pushed", text)

    def test_badge_gone_after_shown_in_new(self):
        """After user opened /new and saw the film (shown_at set), the badge disappears."""
        from bot import _format_movie_discovery_cache
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {
                "movie_seen_by_user": {
                    "100": {"kp:1": {"notified_at": "ts1", "shown_at": "ts2"}}
                }
            }
            text = _format_movie_discovery_cache(self._cache(self._card("Seen", kp_id=1)), chat_id=100)
        self.assertNotIn("🆕", text)


    def test_badge_gone_after_handled_from_notification(self):
        from bot import _format_movie_discovery_cache
        with patch("bot.state_store") as st:
            st.load_movie_discovery_settings.return_value = {
                "movie_seen_by_user": {
                    "100": {"kp:1": {"notified_at": "ts1", "handled_at": "ts2"}}
                }
            }
            text = _format_movie_discovery_cache(self._cache(self._card("Handled", kp_id=1)), chat_id=100)
        self.assertNotIn("🆕", text)


# ---------------------------------------------------------------------------
# Movie /new notification callbacks
# ---------------------------------------------------------------------------


class MovieNotificationDownloadCallbackTests(unittest.IsolatedAsyncioTestCase):
    def _item(self, *, in_plex: bool = False) -> dict:
        return {
            "card": {
                "title": "Snapshot Movie",
                "year": 2026,
                "kp_id": 77,
                "in_plex": in_plex,
            },
            "result": {
                "title": "Snapshot Movie 1080p",
                "url": "https://rutracker.org/forum/viewtopic.php?t=123",
                "torrent_url": "https://jackett.local/dl/123",
                "tracker_name": "rutracker",
                "source": "jackett",
                "size": "2.5 GB",
                "seeders": 10,
            },
        }

    async def test_single_download_reuses_snapshot_result(self):
        update = _make_callback_update(chat_id=100, callback_data="new:dl:abc123def0:0")
        context = _make_context()
        item = self._item()
        download = AsyncMock(return_value=bot.SEARCH_RESULTS)

        with (
            patch.object(bot, "_is_allowed", return_value=True),
            patch.object(bot, "_load_movie_notification_snapshot", return_value={"items": [item]}),
            patch.object(bot, "_download_and_add", download),
        ):
            state = await bot.movie_new_notification_download(update, context)

        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(context.user_data["srch_results"], [item["result"]])
        self.assertEqual(context.user_data["srch_source"], "movie_discovery_notification")
        download.assert_awaited_once()
        self.assertEqual(download.await_args.kwargs["_movie_handled_cards"], [item["card"]])

    async def test_bulk_run_marks_successful_cards_handled(self):
        update = _make_callback_update(chat_id=100, callback_data="new:bulk_ok:abc123def0")
        context = _make_context()
        item = self._item()
        plex_item = self._item(in_plex=True)
        plex_item["card"]["kp_id"] = 78
        settings = {"movie_seen_by_user": {}}
        attempt = AsyncMock(return_value=("task1", "torrent-file"))

        with (
            patch.object(bot, "_is_allowed", return_value=True),
            patch.object(bot, "_load_movie_notification_snapshot", return_value={"items": [item, plex_item]}),
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", attempt),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_record_download_added_history"),
            patch.object(bot, "_load_movie_discovery_settings", side_effect=lambda: settings),
            patch.object(bot, "_save_movie_discovery_settings", side_effect=settings.update),
        ):
            await bot.movie_new_notification_bulk_run(update, context)

        attempt.assert_awaited_once()
        seen = settings["movie_seen_by_user"]["100"]
        self.assertTrue(seen["kp:77"].get("handled_at"))
        self.assertNotIn("kp:78", seen)


# ---------------------------------------------------------------------------
# Download fallback: Jackett → rutracker_client direct
# ---------------------------------------------------------------------------


class DownloadFallbackTests(unittest.IsolatedAsyncioTestCase):
    """When Jackett's proxy fails to deliver a Rutracker .torrent (HTTP 404 from
    /dl/<indexer>/?path=...), the bot must try fetching the file via
    rutracker_client.download_torrent before showing an error. Magnet is not a
    fallback for Rutracker — it's a private tracker with passkey-bearing
    announce URLs that aren't in the .torrent metadata.
    """

    async def test_jackett_404_falls_back_to_rutracker_client_for_rutracker_result(self):
        from jackett import JackettError
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Test Movie 1080p",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "torrent_url": "http://jackett:9117/dl/rutracker/?path=abc",
            "magnet_url": None,
            "tracker_name": "rutracker",
            "source": "jackett",
            "topic_id": "",
            "size": "3 GB",
            "seeders": 10,
            "quality": "1080p",
            "year": 2024,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Test"})

        mock_jackett = MagicMock()
        mock_jackett.download_torrent = MagicMock(side_effect=JackettError("HTTP 404 — Not Found"))
        mock_jackett._api_key = "key"

        mock_rt = MagicMock()
        mock_rt.download_torrent = MagicMock(return_value=b"d8:announce4:test")

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        mock_ds.create_torrent_file.return_value = "task1"

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_torrent_file_is_private", return_value=False),
            patch.object(bot, "_add_public_trackers_to_download_task", return_value=MagicMock(skipped_reason=None)),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_register_task_card_from_query"),
            patch.object(bot, "_start_task_card_refresh"),
            patch.object(bot, "_mark_tracker_processed_if_final"),
        ):
            await bot._download_and_add(update.callback_query, context, 0, subscribe=False, _skip_plex_check=True)

        # rutracker_client was called with the topic id extracted from the URL.
        mock_rt.download_torrent.assert_called_once_with("12345")
        # DS create_torrent_file was called with the bytes returned by rutracker_client.
        ds_call = mock_ds.create_torrent_file.call_args
        self.assertIsNotNone(ds_call)
        # The temp path bytes get written first; just verify the call shape.
        self.assertEqual(mock_ds.create_torrent_file.call_count, 1)

    async def test_jackett_404_no_rutracker_client_falls_through_to_existing_research(self):
        """If rutracker_client is None, behaviour is unchanged: re-search + magnet path runs."""
        from jackett import JackettError
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Test 1080p",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "torrent_url": "http://jackett/dl/rutracker/?path=abc",
            "magnet_url": "magnet:?xt=urn:btih:deadbeef",
            "tracker_name": "rutracker",
            "source": "jackett",
            "topic_id": "",
            "size": "3 GB",
            "seeders": 10,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Test"})

        mock_jackett = MagicMock()
        # Jackett 404 on first call; _refresh_jackett_torrent_url returns None;
        # fall back to magnet.
        mock_jackett.download_torrent = MagicMock(side_effect=JackettError("HTTP 404"))
        mock_jackett._api_key = "k"

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        mock_ds.create_magnet.return_value = "task1"

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_refresh_jackett_torrent_url", AsyncMock(return_value=None)),
            patch.object(bot, "_add_public_trackers_to_download_task", return_value=MagicMock(skipped_reason=None)),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_register_task_card_from_query"),
            patch.object(bot, "_start_task_card_refresh"),
            patch.object(bot, "_mark_tracker_processed_if_final"),
        ):
            await bot._download_and_add(update.callback_query, context, 0, subscribe=False, _skip_plex_check=True)

        # Magnet path was taken.
        mock_ds.create_magnet.assert_called_once_with("magnet:?xt=urn:btih:deadbeef")

    async def test_jackett_404_rutracker_also_fails_falls_through(self):
        """If both Jackett and rutracker_client fail, the existing re-search/magnet
        chain still runs (magnet works in this fake setup)."""
        from jackett import JackettError
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Test 1080p",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "torrent_url": "http://jackett/dl/rutracker/?path=abc",
            "magnet_url": "magnet:?xt=urn:btih:cafebabe",
            "tracker_name": "rutracker",
            "source": "jackett",
            "topic_id": "",
            "size": "3 GB",
            "seeders": 10,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Test"})

        mock_jackett = MagicMock()
        mock_jackett.download_torrent = MagicMock(side_effect=JackettError("HTTP 404"))
        mock_jackett._api_key = "k"

        mock_rt = MagicMock()
        mock_rt.download_torrent = MagicMock(side_effect=RutrackerError("Rutracker also down"))

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        mock_ds.create_magnet.return_value = "task1"

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_refresh_jackett_torrent_url", AsyncMock(return_value=None)),
            patch.object(bot, "_add_public_trackers_to_download_task", return_value=MagicMock(skipped_reason=None)),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_register_task_card_from_query"),
            patch.object(bot, "_start_task_card_refresh"),
            patch.object(bot, "_mark_tracker_processed_if_final"),
        ):
            await bot._download_and_add(update.callback_query, context, 0, subscribe=False, _skip_plex_check=True)

        # Both Jackett and rutracker_client were tried.
        mock_jackett.download_torrent.assert_called_once()
        mock_rt.download_torrent.assert_called_once_with("12345")
        # Fell through to magnet.
        mock_ds.create_magnet.assert_called_once_with("magnet:?xt=urn:btih:cafebabe")

    async def test_refreshed_jackett_url_magnet_redirect_uses_redirect_magnet(self):
        from jackett import JackettError, JackettMagnetRedirect
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Public Movie 1080p",
            "url": "https://example.org/topic/1",
            "torrent_url": "http://jackett/dl/public/?path=old",
            "magnet_url": None,
            "tracker_name": "public",
            "source": "jackett",
            "topic_id": "",
            "size": "3 GB",
            "seeders": 10,
            "quality": "1080p",
            "year": 2024,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Public Movie"})

        mock_jackett = MagicMock()
        mock_jackett.download_torrent = MagicMock(side_effect=[
            JackettError("HTTP 404"),
            JackettMagnetRedirect("magnet:?xt=urn:btih:fresh"),
        ])
        mock_jackett._api_key = "k"

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        mock_ds.create_magnet.return_value = "task1"

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_refresh_jackett_torrent_url", AsyncMock(return_value="http://jackett/dl/public/?path=fresh")),
            patch.object(bot, "_add_public_trackers_to_download_task", return_value=MagicMock(skipped_reason=None)),
            patch.object(bot, "_remember_task_owner"),
            patch.object(bot, "_remember_task_meta"),
            patch.object(bot, "_register_task_card_from_query"),
            patch.object(bot, "_start_task_card_refresh"),
            patch.object(bot, "_mark_tracker_processed_if_final"),
            patch.object(bot.asyncio, "sleep", AsyncMock()),
        ):
            await bot._download_and_add(update.callback_query, context, 0, subscribe=False, _skip_plex_check=True)

        self.assertEqual(mock_jackett.download_torrent.call_count, 2)
        mock_ds.create_magnet.assert_called_once_with("magnet:?xt=urn:btih:fresh")

    async def test_magnet_without_task_id_shows_download_list_without_tracking(self):
        """Magnet can be accepted by DS before a task id appears.

        That is not a normal tracked success: no owner/meta/tracker injection with
        an empty id, and the user gets the download-list button instead of a task
        card button.
        """
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Public Movie 1080p",
            "url": "https://example.org/t/1",
            "torrent_url": None,
            "magnet_url": "magnet:?xt=urn:btih:deadbeef&dn=Public+Movie",
            "tracker_name": "public",
            "source": "jackett",
            "topic_id": "",
            "size": "3 GB",
            "seeders": 10,
            "quality": "1080p",
            "year": 2024,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Public Movie"})

        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        mock_ds.create_magnet.return_value = ""
        add_trackers = MagicMock()
        remember_owner = MagicMock()
        remember_meta = MagicMock()

        with (
            patch.object(bot, "jackett_client", MagicMock()),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_wait_for_magnet_task_id", AsyncMock(return_value="")),
            patch.object(bot, "_add_public_trackers_to_download_task", add_trackers),
            patch.object(bot, "_remember_task_owner", remember_owner),
            patch.object(bot, "_remember_task_meta", remember_meta),
            patch.object(bot, "_start_task_card_refresh"),
            patch.object(bot, "_mark_tracker_processed_if_final"),
        ):
            await bot._download_and_add(
                update.callback_query,
                context,
                0,
                subscribe=False,
                _skip_plex_check=True,
        )

        add_trackers.assert_not_called()
        remember_owner.assert_not_called()
        remember_meta.assert_not_called()
        final_call = update.callback_query.edit_message_text.await_args
        final_text = final_call.args[0]
        self.assertIn("бот пока не видит созданную задачу", final_text)
        self.assertIn("через минуту откройте список загрузок", final_text)
        self.assertIn("трекеры не добавляю", final_text)
        markup = final_call.kwargs.get("reply_markup")
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertNotIn("📋 Показать задачу", labels)
        self.assertIn("📚 К списку загрузок", labels)

    async def test_wait_for_magnet_task_id_allows_missing_progress_message(self):
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []

        with patch.object(bot, "ds_client", mock_ds):
            task_id = await bot._wait_for_magnet_task_id(
                "magnet:?xt=urn:btih:deadbeef",
                set(),
                None,
                attempts=1,
                delay_seconds=0,
            )

        self.assertEqual(task_id, "")
        mock_ds.list_tasks.assert_called_once()

    async def test_nonretryable_download_station_failure_hides_queue_button(self):
        from download_station import DownloadStationError
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Auth Fail Movie 1080p",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "torrent_url": None,
            "magnet_url": None,
            "tracker_name": "rutracker",
            "source": "rutracker",
            "topic_id": "12345",
            "size": "3 GB",
            "seeders": 10,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Auth Fail Movie"})
        mock_rt = MagicMock()
        mock_rt.download_torrent.return_value = b"d8:announce4:test"
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        mock_ds.create_torrent_file.side_effect = DownloadStationError("Auth failed")

        with (
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_pending_downloads_enabled", return_value=True),
            patch.object(bot, "_record_download_history"),
        ):
            result_state = await bot._download_and_add(
                update.callback_query,
                context,
                0,
                subscribe=False,
                _skip_plex_check=True,
            )

        self.assertEqual(result_state, bot.SEARCH_RESULTS)
        final_call = update.callback_query.edit_message_text.await_args
        callbacks = [
            button.callback_data
            for row in final_call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("srch:retry_dl:0", callbacks)
        self.assertNotIn("srch:queue_dl:0", callbacks)

    async def test_retryable_jackett_failure_keeps_queue_button(self):
        from jackett import JackettError
        update = _make_callback_update(chat_id=100)
        result = {
            "title": "Retryable Movie 1080p",
            "url": "https://example.org/topic/1",
            "torrent_url": "http://jackett/dl/public/?path=old",
            "magnet_url": None,
            "tracker_name": "public",
            "source": "jackett",
            "topic_id": "",
            "size": "3 GB",
            "seeders": 10,
        }
        context = _make_context(user_data={"srch_results": [result], "srch_query": "Retryable Movie"})
        mock_jackett = MagicMock()
        mock_jackett.download_torrent.side_effect = JackettError("HTTP 404")
        mock_jackett._api_key = "k"
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "ds_client", mock_ds),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_refresh_jackett_torrent_url", AsyncMock(return_value=None)),
            patch.object(bot, "_pending_downloads_enabled", return_value=True),
            patch.object(bot, "_record_download_history"),
        ):
            result_state = await bot._download_and_add(
                update.callback_query,
                context,
                0,
                subscribe=False,
                _skip_plex_check=True,
            )

        self.assertEqual(result_state, bot.SEARCH_RESULTS)
        final_call = update.callback_query.edit_message_text.await_args
        callbacks = [
            button.callback_data
            for row in final_call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("srch:retry_dl:0", callbacks)
        self.assertIn("srch:queue_dl:0", callbacks)


class FormatDownloadErrorTests(unittest.TestCase):
    """_format_download_error: replaces raw long URLs with a compact summary."""

    def test_jackett_404_compact_text(self):
        from jackett import JackettError
        # The real error text contains a huge URL with base64 path; we shouldn't
        # leak any of that.
        exc = JackettError(
            "Не удалось скачать torrent через Jackett: HTTP 404 — 404 Client Error: "
            "Not Found for url: http://192.168.1.103:9117/dl/rutracker/?jackett_apikey="
            "***&path=Q2ZESjhQTTMtd2RZRZlVwUGlDOTF0SjVL... (огромный URL)"
        )
        text = bot._format_download_error(exc)
        self.assertIn("Jackett не отдал torrent-файл", text)
        self.assertNotIn("HTTP 404", text)
        self.assertNotIn("path=", text)
        self.assertNotIn("jackett_apikey", text)
        self.assertLess(len(text), 200)

    def test_jackett_5xx_text(self):
        from jackett import JackettError
        exc = JackettError("Не удалось скачать: HTTP 503 Service Unavailable")
        text = bot._format_download_error(exc)
        self.assertIn("временно недоступен", text)
        self.assertNotIn("HTTP 503", text)

    def test_jackett_timeout_text(self):
        from jackett import JackettError
        exc = JackettError("Read timed out after 45s")
        text = bot._format_download_error(exc)
        self.assertIn("ожидания", text)

    def test_rutracker_error_text(self):
        exc = RutrackerError("Капча на странице")
        text = bot._format_download_error(exc)
        self.assertIn("Rutracker", text)
        self.assertIn("капчу", text)

    def test_download_station_error_text(self):
        from download_station import DownloadStationError
        exc = DownloadStationError("Auth failed")
        text = bot._format_download_error(exc)
        self.assertIn("Download Station", text)
        self.assertNotIn("Auth failed", text)

    def test_download_failure_text_explains_queue_option(self):
        from jackett import JackettError
        text = bot._download_failure_text(JackettError("HTTP 404"), can_queue=True)
        self.assertIn("Не удалось добавить загрузку", text)
        self.assertIn("не получилось передать выбранную раздачу", text)
        self.assertIn("поставить в очередь", text)

    def test_download_failure_text_without_queue_mentions_download_service(self):
        from jackett import JackettError
        text = bot._download_failure_text(JackettError("HTTP 404"), can_queue=False)
        self.assertIn("попробовать снова сейчас", text)
        self.assertIn("сервиса загрузок", text)

    def test_unknown_error_truncated(self):
        exc = ValueError("X" * 500)
        text = bot._format_download_error(exc)
        self.assertLess(len(text), 250)
        self.assertNotIn("XXXXX", text)


class SearchRetryDlHandlerTests(unittest.IsolatedAsyncioTestCase):
    """search_retry_dl: re-runs _download_and_add with the given index."""

    async def test_retry_invokes_download_with_parsed_index(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:retry_dl:2")
        context = _make_context()
        with patch.object(bot, "_download_and_add", AsyncMock(return_value=bot.SEARCH_RESULTS)) as dl_mock:
            await bot.search_retry_dl(update, context)
        dl_mock.assert_awaited_once()
        args = dl_mock.call_args.args
        self.assertEqual(args[2], 2)   # index = 2
        # subscribe kwarg is False by default in retry
        self.assertEqual(dl_mock.call_args.kwargs.get("subscribe"), False)

    async def test_retry_with_malformed_callback_data_ends_conversation(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:retry_dl:not-a-number")
        context = _make_context()
        with patch.object(bot, "_download_and_add", AsyncMock()) as dl_mock:
            result = await bot.search_retry_dl(update, context)
        dl_mock.assert_not_awaited()
        from telegram.ext import ConversationHandler
        self.assertEqual(result, ConversationHandler.END)
        update.callback_query.edit_message_text.assert_called_once()


class SearchRetryHandlerTests(unittest.IsolatedAsyncioTestCase):
    """search_retry should re-run the unified search with the saved query."""

    async def test_retry_invokes_run_search_with_correct_argument_order(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:retry")
        context = _make_context(user_data={"srch_search_query": "Драйв 1080p"})

        with patch.object(bot, "_run_search", AsyncMock(return_value=bot.SEARCH_RESULTS)) as run_mock:
            result = await bot.search_retry(update, context)

        self.assertEqual(result, bot.SEARCH_RESULTS)
        run_mock.assert_awaited_once_with(
            update.callback_query.edit_message_text,
            context,
            "Драйв 1080p",
        )


class SearchJackettDoHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Tracker confirmation should not bypass the shared search pipeline."""

    async def test_results_mode_uses_unified_search_pipeline(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:jk_search")
        context = _make_context(user_data={
            "srch_picker_return_to": "results",
            "srch_jackett_selected": {"rutracker"},
            "srch_search_query": "Драйв 1080p",
        })

        with (
            patch.object(bot, "jackett_client", MagicMock()),
            patch.object(bot, "_execute_search", AsyncMock(return_value=bot.SEARCH_RESULTS)) as exec_mock,
        ):
            result = await bot.search_jackett_do(update, context)

        self.assertEqual(result, bot.SEARCH_RESULTS)
        update.callback_query.answer.assert_awaited_once()
        exec_mock.assert_awaited_once_with(
            update.callback_query,
            context,
            "Драйв 1080p",
        )


class SearchClusterPickerBackTests(unittest.IsolatedAsyncioTestCase):
    """Cluster-filtered results should let the user return to the chooser."""

    def _context_with_picker_state(self):
        full = [
            {"title": "Драйв 2011 1080p", "seeders": 10, "size": "5 GB"},
            {"title": "Ледяной драйв 2021 1080p", "seeders": 7, "size": "4 GB"},
        ]
        picker_clusters = [
            {"title": "Драйв", "year": 2011, "count": 1, "indices": [0]},
            {"title": "Ледяной драйв", "year": 2021, "count": 1, "indices": [1]},
        ]
        return _make_context(user_data={
            "srch_results_full": list(full),
            "srch_clusters": list(picker_clusters),
            "srch_picker_clusters": list(picker_clusters),
            "srch_search_query": "Драйв 1080p",
            "srch_source": "jackett",
            "srch_banner": "🎬 Показаны раздачи в 1080p.",
        })

    async def test_pick_cluster_keeps_back_to_variants_button(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:cluster:0")
        context = self._context_with_picker_state()

        with patch.object(bot, "_enrich_top_results_with_metadata", AsyncMock()):
            result = await bot.search_pick_cluster(update, context)

        self.assertEqual(result, bot.SEARCH_RESULTS)
        self.assertTrue(context.user_data.get("srch_cluster_picker_return"))
        self.assertIn("srch_results_full", context.user_data)

        kwargs = update.callback_query.edit_message_text.await_args.kwargs
        labels = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("⬅️ К вариантам", labels)

    async def test_cluster_back_rerenders_original_picker(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:cluster_back")
        context = self._context_with_picker_state()
        context.user_data["srch_cluster_picker_return"] = True

        result = await bot.search_cluster_back(update, context)

        self.assertEqual(result, bot.SEARCH_RESULTS)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("найдено несколько вариантов", text)
        self.assertIn("качество: 1080p", text)

        kwargs = update.callback_query.edit_message_text.await_args.kwargs
        buttons = {b.text: b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row}
        self.assertEqual(buttons["🎬 Драйв (2011) · 1 разд."], "srch:cluster:0")
        self.assertEqual(buttons["📋 Показать все 2 раздач"], "srch:cluster:all")


class SearchClusterKindTests(unittest.TestCase):
    """Cluster picker badges should distinguish films from series."""

    def test_build_search_clusters_marks_series_from_title_or_category(self):
        clusters = bot._build_search_clusters([
            {"title": "Драйв 2011 1080p", "category": "Фильмы"},
            {"title": "Клиника / Scrubs / Сезон 3 1080p", "category": ""},
            {"title": "Фарго 2014 1080p", "category": "Зарубежные сериалы"},
        ])

        by_title = {cluster["title"]: cluster for cluster in clusters}
        self.assertEqual(by_title["Драйв"]["kind"], "movie")
        self.assertEqual(by_title["Клиника"]["kind"], "series")
        self.assertEqual(by_title["Фарго"]["kind"], "series")


# ---------------------------------------------------------------------------
# Plex pre-download check helpers
# ---------------------------------------------------------------------------


class PlexPreDownloadCheckTests(unittest.TestCase):
    """Tests for _plex_is_series, _plex_pre_check, _plex_confirm_text."""

    def _make_movie(self, resolution: str = "1080", title: str = "Dune"):
        m = MagicMock()
        m.title = title
        m.year = 2021
        m.resolution = resolution
        return m

    # --- _plex_is_series ---

    def test_series_detected_by_s01e01(self):
        self.assertTrue(_plex_is_series("Loki S01E02 2021 1080p"))

    def test_series_detected_by_season_cyrillic(self):
        self.assertTrue(_plex_is_series("Игра в кальмара Сезон 2"))

    def test_movie_not_detected_as_series(self):
        self.assertFalse(_plex_is_series("Dune.Part.Two.2024.1080p"))

    # --- _plex_quality_from_title ---

    def test_quality_from_title_1080p(self):
        self.assertEqual(_plex_quality_from_title("Dune.2021.1080p.BluRay"), "1080")

    def test_quality_from_title_4k(self):
        self.assertEqual(_plex_quality_from_title("Avatar 2 4K HDR"), "4k")

    def test_quality_from_title_unknown(self):
        self.assertEqual(_plex_quality_from_title("some.title.without.quality"), "")

    # --- _plex_pre_check ---

    def test_pre_check_returns_none_when_plex_disabled(self):
        with patch.object(bot, "PLEX_ENABLED", False):
            result = _plex_pre_check("Dune", 2021, "1080")
        self.assertIsNone(result)

    def test_pre_check_returns_none_for_series(self):
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune s01", 2021): self._make_movie()}),
        ):
            result = _plex_pre_check("Dune S01E01", 2021, "1080")
        self.assertIsNone(result)

    def test_pre_check_returns_none_when_not_found(self):
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("other movie", 2020): self._make_movie(title="Other Movie")}),
        ):
            result = _plex_pre_check("Dune", 2021, "1080")
        self.assertIsNone(result)

    def test_pre_check_returns_result_when_found(self):
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune", 2021): self._make_movie("1080")}),
        ):
            result = _plex_pre_check("Dune", 2021, "1080")
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "warn_same")

    def test_pre_check_offer_upgrade_when_plex_has_lower_quality(self):
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune", 2021): self._make_movie("720")}),
        ):
            result = _plex_pre_check("Dune", 2021, "1080")
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "offer_upgrade")

    def test_pre_check_warn_better_when_plex_has_higher_quality(self):
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune", 2021): self._make_movie("4k")}),
        ):
            result = _plex_pre_check("Dune", 2021, "1080")
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "warn_better")

    # --- _plex_confirm_text ---

    def test_confirm_text_warn_same(self):
        movie = self._make_movie("1080")
        check = MagicMock()
        check.plex_movie = movie
        check.action = "warn_same"
        text = _plex_confirm_text(check, "Dune", "1080")
        self.assertIn("Dune", text)
        self.assertIn("уже есть в Plex", text)
        self.assertIn("1080", text)

    def test_confirm_text_offer_upgrade(self):
        movie = self._make_movie("720")
        check = MagicMock()
        check.plex_movie = movie
        check.action = "offer_upgrade"
        text = _plex_confirm_text(check, "Dune", "1080")
        self.assertIn("720", text)
        self.assertIn("1080", text)

    def test_confirm_text_has_download_hint(self):
        movie = self._make_movie("1080")
        check = MagicMock()
        check.plex_movie = movie
        check.action = "warn_same"
        text = _plex_confirm_text(check, "Dune", "1080")
        self.assertIn("Скачать всё равно?", text)


# ---------------------------------------------------------------------------
# Plex post-download polling helpers
# ---------------------------------------------------------------------------


class PlexPollingTests(unittest.TestCase):
    """Tests for _plex_find_by_ds_title and _plex_poll_after_finish."""

    def setUp(self):
        self._allowed_patch = patch.object(bot, "ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()

    def _labels(self, keyboard):
        return [button.text for row in keyboard.inline_keyboard for button in row]

    def _make_plex_movie(self, title: str, year: int, file_paths: list[str], resolution: str = "1080"):
        m = MagicMock()
        m.title = title
        m.year = year
        m.rating_key = "42"
        m.resolution = resolution
        m.file_paths = file_paths
        return m

    def test_find_by_ds_title_matches_file_path(self):
        movie = self._make_plex_movie(
            "Dune", 2021,
            ["/video/Movies/Dune.2021.1080p.BluRay/Dune.2021.1080p.BluRay.mkv"],
        )
        with patch.object(bot, "_plex_library", {("dune", 2021): movie}):
            result = _plex_find_by_ds_title("Dune.2021.1080p.BluRay")
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Dune")

    def test_find_by_ds_title_does_not_match_substring_inside_other_name(self):
        """Safe substring: 'Movie.2024' must NOT match '.../Movie.2024.backup/...'.
        Regression for #7 from the Plex audit plan."""
        other_movie = self._make_plex_movie(
            "Movie 2024 Backup Collection", 2024,
            ["/archive/Movie.2024.backup/some.file.mkv"],
        )
        with patch.object(bot, "_plex_library", {("movie 2024 backup collection", 2024): other_movie}):
            self.assertIsNone(_plex_find_by_ds_title("Movie.2024"))

    def test_plex_found_history_extra_marks_series(self):
        season = MagicMock()
        season.season_number = 5

        extra = bot._plex_found_history_extra(season, "3", "Сезон 5 «Clarkson's Farm»")

        self.assertEqual(extra, {
            "kind": "series",
            "canonical_title": "Clarkson's Farm",
            "series_query": "Clarkson's Farm",
            "season": 5,
        })

    def test_find_by_ds_title_matches_filename_without_extension(self):
        """Task title without extension should match Plex file `Title.mkv`."""
        movie = self._make_plex_movie(
            "Inception", 2010,
            ["/movies/Inception.2010.1080p.mkv"],
        )
        with patch.object(bot, "_plex_library", {("inception", 2010): movie}):
            result = _plex_find_by_ds_title("Inception.2010.1080p")
        self.assertIsNotNone(result)

    def test_find_by_ds_title_matches_windows_path_separator(self):
        """Should work for Windows-style backslash paths from Plex."""
        movie = self._make_plex_movie(
            "Tenet", 2020,
            ["C:\\Plex\\Movies\\Tenet.2020.4K\\Tenet.2020.4K.mkv"],
        )
        with patch.object(bot, "_plex_library", {("tenet", 2020): movie}):
            result = _plex_find_by_ds_title("Tenet.2020.4K")
        self.assertIsNotNone(result)

    def test_find_by_ds_title_returns_none_when_no_match(self):
        movie = self._make_plex_movie(
            "Avatar", 2009,
            ["/video/Movies/Avatar (2009)/Avatar.2009.mkv"],
        )
        with patch.object(bot, "_plex_library", {("avatar", 2009): movie}):
            result = _plex_find_by_ds_title("Dune.2021.1080p.BluRay")
        self.assertIsNone(result)

    def test_find_by_ds_title_empty_title_returns_none(self):
        with patch.object(bot, "_plex_library", {}):
            self.assertIsNone(_plex_find_by_ds_title(""))
            self.assertIsNone(_plex_find_by_ds_title("   "))

    def test_poll_after_finish_reports_unreachable_when_all_refreshes_failed(self):
        """When Plex was unreachable for the entire polling window (no refresh
        ever succeeded), the timeout message must say 'Plex недоступен',
        not 'не появился в Plex'. Regression for #6 from the audit plan."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        # _refresh_plex_library is a no-op AsyncMock — but we keep
        # _plex_consecutive_failures > 0 to simulate ongoing failures.
        with (
            patch.object(bot, "_plex_library", {}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_plex_library_find", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "_plex_consecutive_failures", 3),
            patch.object(bot, "_load_notified_tasks", return_value={}),
            patch.object(bot, "_mark_plex_poll_sent"),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Some.Movie.2024", [100], max_attempts=1, interval_seconds=0
            ))

        fake_app.bot.send_message.assert_awaited_once()
        text = fake_app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("сервер был недоступен", text)
        self.assertNotIn("не появился в Plex", text)
        labels = self._labels(fake_app.bot.send_message.call_args.kwargs["reply_markup"])
        self.assertIn("📋 Показать задачу", labels)
        self.assertIn("✖️ Закрыть", labels)

    def test_poll_after_finish_falls_back_to_title_year_when_substring_misses(self):
        """If _plex_find_by_ds_title returns None (e.g. Plex renamed the file or
        file_paths is empty), the poller must try _plex_library_find(title, year).
        Regression for #8 from the Plex audit plan."""
        movie = self._make_plex_movie("Dune", 2021, [])  # empty file_paths!
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {("dune", 2021): movie}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),  # substring miss
            patch.object(bot, "_plex_library_find", return_value=movie),     # fallback hit
            patch.object(bot, "_plex_machine_id", "abc123"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.Part.Two.2021.2160p", [100], max_attempts=1, interval_seconds=0
            ))

        # Found notification should still be sent thanks to the fallback
        fake_app.bot.send_message.assert_awaited_once()
        self.assertIn("✅", fake_app.bot.send_message.call_args.kwargs["text"])

    def test_poll_after_finish_sends_found_notification(self):
        """Polling should send a found-notification when the movie appears in Plex."""
        movie = self._make_plex_movie(
            "Dune", 2021,
            ["/video/Dune.2021.1080p.mkv"],
        )
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        store = MagicMock()

        with (
            patch.object(bot, "_plex_library", {("dune", 2021): movie}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=movie),
            patch.object(bot, "_plex_machine_id", "abc123"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "state_store", store),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.2021.1080p", [100], max_attempts=1, interval_seconds=0
            ))

        fake_app.bot.send_message.assert_awaited_once()
        call_kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], 100)
        self.assertIn("✅", call_kwargs["text"])
        # The notification now uses the canonical Plex title, not the raw torrent name.
        self.assertIn("Dune", call_kwargs["text"])
        labels = self._labels(call_kwargs["reply_markup"])
        history_entry = store.append_download_history.call_args.args[0]
        self.assertEqual(history_entry["event"], "plex_found")
        self.assertEqual(history_entry["chat_id"], 100)
        self.assertEqual(history_entry["task_id"], "task1")
        self.assertEqual(history_entry["plex_rating_key"], "42")
        self.assertIn("▶️ Смотреть в Plex", labels)
        self.assertIn("✖️ Закрыть", labels)
        self.assertNotIn("📋 Показать задачу", labels)

    def test_poll_after_finish_retries_transient_found_send_failure(self):
        movie = self._make_plex_movie("Dune", 2021, ["/video/Dune.2021.1080p.mkv"])
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock(side_effect=[bot.TimedOut("temporary"), None])
        tasks_dict = {"task1": object()}

        with (
            patch.object(bot, "_plex_library", {("dune", 2021): movie}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=movie),
            patch.object(bot, "_plex_machine_id", "abc123"),
            patch.object(bot, "_PLEX_POLLING_TASKS", tasks_dict),
            patch.object(bot, "_record_download_history") as history,
            patch.object(bot, "_mark_plex_poll_done") as mark_done,
            patch.object(bot.asyncio, "sleep", AsyncMock()) as sleep,
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.2021.1080p", [100], max_attempts=1, interval_seconds=0
            ))

        self.assertEqual(fake_app.bot.send_message.await_count, 2)
        sleep.assert_awaited_once_with(1.0)
        history.assert_called_once()
        mark_done.assert_called_once_with("task1")
        self.assertIn("task1", tasks_dict)
        self.assertIsNone(tasks_dict["task1"])

    def test_poll_after_finish_keeps_retry_open_after_persistent_transient_send_failure(self):
        movie = self._make_plex_movie("Dune", 2021, ["/video/Dune.2021.1080p.mkv"])
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock(side_effect=bot.TimedOut("temporary"))
        tasks_dict = {"task1": object()}

        with (
            patch.object(bot, "_plex_library", {("dune", 2021): movie}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=movie),
            patch.object(bot, "_plex_machine_id", "abc123"),
            patch.object(bot, "_PLEX_POLLING_TASKS", tasks_dict),
            patch.object(bot, "_record_download_history") as history,
            patch.object(bot, "_mark_plex_poll_done") as mark_done,
            patch.object(bot.asyncio, "sleep", AsyncMock()),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.2021.1080p", [100], max_attempts=1, interval_seconds=0
            ))

        self.assertEqual(fake_app.bot.send_message.await_count, 3)
        history.assert_not_called()
        mark_done.assert_not_called()
        self.assertNotIn("task1", tasks_dict)

    def test_poll_after_finish_sends_timeout_notification_when_not_found(self):
        """Polling should send a timeout-notification when exhausted without finding the movie."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "_load_notified_tasks", return_value={}),
            patch.object(bot, "_mark_plex_poll_sent"),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Some.Movie.2024", [100], max_attempts=1, interval_seconds=0
            ))

        fake_app.bot.send_message.assert_awaited_once()
        call_kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertIn("⚠️", call_kwargs["text"])
        self.assertIn("Some.Movie.2024", call_kwargs["text"])
        labels = self._labels(call_kwargs["reply_markup"])
        self.assertIn("📋 Показать задачу", labels)
        self.assertIn("✖️ Закрыть", labels)

    def test_poll_after_finish_marks_task_done_not_removed(self):
        """After completing, task_id stays in _PLEX_POLLING_TASKS with value None.
        This prevents the notification loop from re-launching a second poll."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        tasks_dict = {"task1": MagicMock()}

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", tasks_dict),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Movie", [100], max_attempts=1, interval_seconds=0
            ))

        # Key must remain so the guard `task_id not in _PLEX_POLLING_TASKS` stays False.
        self.assertIn("task1", tasks_dict)
        self.assertIsNone(tasks_dict["task1"])

    def test_poll_after_finish_deletes_hint_messages_when_found(self):
        """Hint messages must be deleted before the found-notification is sent."""
        movie = self._make_plex_movie("Dune", 2021, ["/video/Dune.2021.mkv"])
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        fake_app.bot.delete_message = AsyncMock()
        deleted_before_send: list[tuple[int, int]] = []

        async def _track_delete(chat_id, message_id):
            deleted_before_send.append((chat_id, message_id))

        fake_app.bot.delete_message.side_effect = _track_delete

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=movie),
            patch.object(bot, "_plex_machine_id", "abc123"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.2021.1080p", [100],
                hint_msg_ids={100: 999},
                max_attempts=1,
                interval_seconds=0,
            ))

        # Hint message should be deleted
        self.assertIn((100, 999), deleted_before_send)
        # Found notification should also be sent
        fake_app.bot.send_message.assert_awaited_once()
        self.assertIn("✅", fake_app.bot.send_message.call_args.kwargs["text"])

    def test_poll_after_finish_deletes_hint_messages_on_timeout(self):
        """Hint messages must be deleted even when Plex polling times out."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        fake_app.bot.delete_message = AsyncMock()

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Some.Movie.2024", [100],
                hint_msg_ids={100: 888},
                max_attempts=1,
                interval_seconds=0,
            ))

        fake_app.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=888)
        # Timeout notification should also be sent
        fake_app.bot.send_message.assert_awaited_once()
        kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertIn("⚠️", kwargs["text"])
        labels = self._labels(kwargs["reply_markup"])
        self.assertIn("📋 Показать задачу", labels)

    def test_poll_after_finish_uses_meta_canonical_lookup_for_movie(self):
        """When meta is provided for a movie, poll must use _plex_library_find first
        instead of _plex_find_by_ds_title."""
        movie = self._make_plex_movie("Dune: Part Two", 2024, [])
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        # Substring match would fail (file_paths empty), but meta-based lookup hits.
        meta = {"kind": "movie", "title": "Dune: Part Two", "year": 2024, "quality": "1080"}

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_library_find", return_value=movie),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_plex_machine_id", "abc"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.Part.Two.2024.1080p", [100],
                meta=meta, max_attempts=1, interval_seconds=0,
            ))

        fake_app.bot.send_message.assert_awaited_once()
        text = fake_app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Dune: Part Two", text)

    def test_poll_after_finish_uses_meta_for_series(self):
        """For meta.kind=='series' poll must look up the show and find the season."""
        from plex import PlexShow, PlexSeason
        season = PlexSeason(
            "season-key-77",
            3,
            episode_count=10,
            file_paths=["/volume1/video/Klinika S03/Klinika S03E01.mkv"],
            resolution="1080",
        )
        show = PlexShow("Клиника", 2001, "show-key-99", seasons={3: season})
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        meta = {"kind": "series", "title": "Klinika S03", "year": 0,
                "quality": "1080", "series_query": "Клиника", "season_num": 3}

        async def fake_ensure(show_arg):
            return show_arg.seasons

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_show_find", return_value=show),
            patch.object(bot, "_plex_ensure_show_seasons", AsyncMock(side_effect=fake_ensure)),
            patch.object(bot, "_plex_machine_id", "machine-1"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Klinika S03", [100],
                meta=meta, max_attempts=1, interval_seconds=0,
            ))

        fake_app.bot.send_message.assert_awaited_once()
        kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertIn("Сезон 3", kwargs["text"])
        self.assertIn("Клиника", kwargs["text"])
        # Plex deep-link uses the season's rating_key + machine_id.
        keyboard = kwargs["reply_markup"]
        btn = keyboard.inline_keyboard[0][0]
        self.assertEqual(btn.text, "▶️ Смотреть в Plex")
        self.assertTrue(btn.url.startswith("https://app.plex.tv"))
        self.assertIn("season-key-77", btn.url)
        self.assertIn("machine-1", btn.url)
        # ✖️ Закрыть button on a separate row so the user can dismiss
        # the notification once seen.
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("✖️ Закрыть", labels)

    def test_series_meta_match_requires_current_task_file_path(self):
        from plex import PlexShow, PlexSeason
        season = PlexSeason(
            "season-key-77",
            3,
            episode_count=10,
            file_paths=["/volume1/video/Old.Klinika.S03/Old.Klinika.S03E01.mkv"],
            resolution="1080",
        )
        show = PlexShow("Клиника", 2001, "show-key-99", seasons={3: season})
        meta = {"kind": "series", "title": "Klinika S03", "year": 0,
                "quality": "1080", "series_query": "Клиника", "season_num": 3}

        async def fake_ensure(show_arg):
            return show_arg.seasons

        with (
            patch.object(bot, "_plex_show_find", return_value=show),
            patch.object(bot, "_plex_ensure_show_seasons", AsyncMock(side_effect=fake_ensure)),
            patch.object(bot, "_plex_shows_library", {}),
        ):
            target, metadata_type, _found_title = asyncio.run(
                bot._plex_poll_lookup_target("New.Klinika.S03", meta)
            )

        self.assertIsNone(target)
        self.assertEqual(metadata_type, "1")

    def test_poll_after_finish_finds_series_by_episode_file_path(self):
        """Scene-style DS title should match Plex episode file paths for series."""
        from plex import PlexShow, PlexSeason
        task_title = "Clarksons.Farm.S05E01.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv"
        season = PlexSeason(
            "season-key-5",
            5,
            episode_count=4,
            file_paths=[f"/volume1/video/{task_title}"],
            resolution="1080",
        )
        show = PlexShow("Clarkson's Farm", 2021, "show-key-cf", seasons={5: season})
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        store = MagicMock()
        meta = {
            "kind": "series",
            "title": task_title,
            "year": 0,
            "quality": "1080",
            "series_query": task_title,
            "season_num": 5,
        }

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_show_find", return_value=None),
            patch.object(bot, "_plex_shows_library", {("clarkson s farm", 2021): show}),
            patch.object(bot, "_plex_machine_id", "machine-1"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "state_store", store),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", task_title, [100],
                meta=meta, max_attempts=1, interval_seconds=0,
            ))

        fake_app.bot.send_message.assert_awaited_once()
        kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertIn("Сезон 5", kwargs["text"])
        self.assertIn("Clarkson&#x27;s Farm", kwargs["text"])
        btn = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertIn("season-key-5", btn.url)
        history_entry = store.append_download_history.call_args.args[0]
        self.assertEqual(history_entry["event"], "plex_found")
        self.assertEqual(history_entry["kind"], "series")
        self.assertEqual(history_entry["series_query"], "Clarkson's Farm")
        self.assertEqual(history_entry["canonical_title"], "Clarkson's Farm")
        self.assertEqual(history_entry["season"], 5)
        self.assertEqual(history_entry["plex_rating_key"], "season-key-5")

    def test_poll_after_finish_series_without_meta_falls_back_via_legacy_path(self):
        """A legacy task (no meta) whose DS title looks like a series should still
        try the series path by reconstructing meta from the title."""
        from plex import PlexShow, PlexSeason
        season = PlexSeason("sk2", 4, 10, ["/volume1/video/Show.X.S04E01/Show.X.S04E01.mkv"], "720")
        show = PlexShow("Show X", 2018, "show2", seasons={4: season})
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        async def fake_ensure(show_arg):
            return show_arg.seasons

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_show_find", return_value=show),
            patch.object(bot, "_plex_ensure_show_seasons", AsyncMock(side_effect=fake_ensure)),
            patch.object(bot, "_plex_machine_id", "m1"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "legacy_task", "Show.X.S04E01", [100],
                meta=None, max_attempts=1, interval_seconds=0,
            ))

        fake_app.bot.send_message.assert_awaited_once()
        text = fake_app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Сезон 4", text)

    def test_poll_after_finish_persists_plex_done_marker(self):
        """After polling completes, plex_done must be saved so restart doesn't re-poll."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        saved: dict = {}

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "_load_notified_tasks", return_value={}),
            patch.object(bot, "_save_notified_tasks", side_effect=saved.update),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Movie", [100], max_attempts=1, interval_seconds=0
            ))

        # _save_notified_tasks must have been called with plex_done=True for task1
        self.assertIn("task1", saved)
        self.assertTrue(saved["task1"].get("plex_done"))

    def test_poll_after_finish_does_not_resend_already_delivered_found(self):
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        target = MagicMock()
        target.rating_key = "rk1"

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_poll_lookup_target", AsyncMock(return_value=(target, "1", "Movie"))),
            patch.object(bot, "_plex_machine_id", "machine1"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
            patch.object(bot, "_load_notified_tasks",
                         return_value={"task1": {"plex_poll": {"found": ["100"]}}}),
            patch.object(bot, "_mark_plex_poll_sent") as mark_sent,
            patch.object(bot, "_mark_plex_poll_done") as mark_done,
            patch.object(bot, "_record_download_history") as history,
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Movie", [100], max_attempts=1, interval_seconds=0
            ))

        fake_app.bot.send_message.assert_not_awaited()
        mark_sent.assert_not_called()
        history.assert_not_called()
        mark_done.assert_called_once_with("task1")

    def test_poll_after_finish_cancel_does_not_persist_plex_done_marker(self):
        """Cancelled polling must be retried after restart, not marked done."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        fake_app.bot.delete_message = AsyncMock()
        polling_tasks = {"task1": object()}

        with (
            patch.object(bot, "_refresh_plex_library", AsyncMock(side_effect=asyncio.CancelledError)),
            patch.object(bot, "_PLEX_POLLING_TASKS", polling_tasks),
            patch.object(bot, "_mark_plex_poll_done") as mark_done,
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(_plex_poll_after_finish(
                    fake_app,
                    "task1",
                    "Movie",
                    [100],
                    hint_msg_ids={100: 555},
                    max_attempts=1,
                    interval_seconds=0,
                ))

        mark_done.assert_not_called()
        self.assertNotIn("task1", polling_tasks)
        fake_app.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=555)

    def test_poll_after_finish_suppresses_unmatched_admin_radar_refresh(self):
        """Post-download polling should not alert admins before Plex matching settles."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()
        refresh = AsyncMock()

        with (
            patch.object(bot, "_refresh_plex_library", refresh),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app,
                "task1",
                "Movie",
                [100],
                max_attempts=1,
                interval_seconds=0,
            ))

        refresh.assert_awaited_once_with(fake_app, check_unmatched=False)

    def test_cleanup_plex_pending_removes_temp_file(self):
        """_cleanup_plex_pending must delete the temp .torrent if present."""
        import tempfile
        from bot import _cleanup_plex_pending

        with tempfile.NamedTemporaryFile(delete=False, suffix=".torrent") as f:
            f.write(b"d8:announce")
            tmp_path = f.name
        # Sanity: file exists
        self.assertTrue(Path(tmp_path).exists())

        _cleanup_plex_pending({"type": "torrent", "temp_path": tmp_path})
        self.assertFalse(Path(tmp_path).exists())

    def test_cleanup_plex_pending_handles_missing_file(self):
        """Must not raise if temp file already gone (e.g. consumed by confirm)."""
        from bot import _cleanup_plex_pending
        _cleanup_plex_pending({"type": "torrent", "temp_path": "/nonexistent/path/x.torrent"})
        # No exception = pass

    def test_cleanup_plex_pending_ignores_non_torrent_types(self):
        """For magnet/search type entries (no temp_path) it must be a no-op."""
        from bot import _cleanup_plex_pending
        _cleanup_plex_pending({"type": "magnet", "magnet_uri": "magnet:?xt=..."})
        _cleanup_plex_pending({"type": "search", "index": 0, "subscribe": False})
        _cleanup_plex_pending(None)
        # No exception = pass

    def test_plex_pre_check_skipped_when_quality_unknown(self):
        """If requested_quality is empty, pre-check must return None instead of
        showing a misleading 'same quality' warning."""
        movie = MagicMock()
        movie.resolution = "1080"
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_library", {("dune", 2021): movie}),
            patch.object(bot, "_plex_library_find", return_value=movie),
        ):
            result = bot._plex_pre_check("Dune", 2021, "")
        self.assertIsNone(result)

    def test_plex_library_find_year_zero_restricts_lookup(self):
        """When year=0 (unknown), do not spread the ±1 search into years -1/0/1
        to avoid false matches against movies with no year metadata."""
        movie_zero = MagicMock()
        movie_one = MagicMock()
        library = {("foo", 0): movie_zero, ("foo", 1): movie_one}
        with patch.object(bot, "_plex_library", library):
            # year=0 must only return year=0 entry, NOT year=1
            self.assertIs(bot._plex_library_find("foo", 0), movie_zero)
            # year=1 still uses ±1 tolerance
            self.assertIs(bot._plex_library_find("foo", 1), movie_one)

    def test_plex_library_find_handles_possessive_apostrophe(self):
        movie = MagicMock()
        movie.title = "Clarkson's Farm"
        library = {("clarkson s farm", 2021): movie}
        with patch.object(bot, "_plex_library", library):
            self.assertIs(bot._plex_library_find("Clarksons Farm", 2021), movie)

    def test_plex_poll_is_done_blocks_restart_after_reboot(self):
        """plex_poll_is_done() must return True when the persisted marker is present,
        preventing a second poll from starting after a bot restart."""
        from bot import _plex_poll_is_done
        notified_with_marker = {"task1": {"status": "done", "sent": ["100"], "plex_done": True}}
        notified_without_marker = {"task1": {"status": "done", "sent": ["100"]}}
        self.assertTrue(_plex_poll_is_done("task1", notified_with_marker))
        self.assertFalse(_plex_poll_is_done("task1", notified_without_marker))
        self.assertFalse(_plex_poll_is_done("unknown", notified_with_marker))


# ---------------------------------------------------------------------------
# Plex refresh single-flight / error classification tests (Phase 1)
# ---------------------------------------------------------------------------


class PlexRefreshSingleFlightTests(unittest.IsolatedAsyncioTestCase):
    """Verify _refresh_plex_library serialises concurrent callers and coalesces
    rapid successive calls. Without these, polling loops + the 30-min cache
    loop could fire 6+ concurrent get_all_movies() calls at Plex."""

    async def test_concurrent_refreshes_coalesce_into_one_api_call(self):
        # Fake plex_client whose get_all_movies just counts invocations.
        fake_plex = MagicMock()
        fake_plex.get_all_movies = MagicMock(return_value=[])
        fake_plex.get_machine_id = MagicMock(return_value="abc123")

        with (
            patch.object(bot, "plex_client", fake_plex),
            patch.object(bot, "_plex_library", {}),
            patch.object(bot, "_plex_library_updated_at", 0.0),
            patch.object(bot, "_plex_refresh_lock", None),
            patch.object(bot, "_plex_machine_id", ""),
        ):
            # Fire 5 concurrent refreshes
            await asyncio.gather(*[bot._refresh_plex_library() for _ in range(5)])

        # First call does the work, subsequent ones see fresh cache and skip via coalesce.
        # We allow up to 2 calls — first does the work, the next four enter the lock
        # one-by-one and see the fresh _plex_library_updated_at, so 1 real call total.
        self.assertEqual(fake_plex.get_all_movies.call_count, 1,
                         "concurrent refreshes must coalesce to a single Plex API call")

    async def test_classify_plex_exception_routes_by_type(self):
        from plex import PlexAuthError, PlexTimeoutError, PlexConnectionError, PlexParseError
        from bot import _classify_plex_exception
        self.assertEqual(_classify_plex_exception(PlexAuthError("bad token"))[0], "auth")
        self.assertEqual(_classify_plex_exception(PlexTimeoutError("slow"))[0], "timeout")
        self.assertEqual(_classify_plex_exception(PlexConnectionError("refused"))[0], "network")
        self.assertEqual(_classify_plex_exception(PlexParseError("bad xml"))[0], "xml")
        self.assertEqual(_classify_plex_exception(RuntimeError("???"))[0], "other")

    async def test_refresh_records_failure_state_on_auth_error(self):
        from plex import PlexAuthError
        fake_plex = MagicMock()
        fake_plex.get_all_movies = MagicMock(side_effect=PlexAuthError("Invalid token"))

        with (
            patch.object(bot, "plex_client", fake_plex),
            patch.object(bot, "_plex_refresh_lock", None),
            patch.object(bot, "_plex_library_updated_at", 0.0),
            patch.object(bot, "_plex_consecutive_failures", 0),
            patch.object(bot, "_plex_last_error_kind", ""),
        ):
            await bot._refresh_plex_library()
            info = bot._plex_cache_info()

        self.assertEqual(info["last_error_kind"], "auth")
        self.assertGreaterEqual(info["consecutive_failures"], 1)


# ---------------------------------------------------------------------------
# Series quality / season-discovery helpers (formatters.py)
# ---------------------------------------------------------------------------


class SeriesHelpersTests(unittest.TestCase):
    """Tests for _quality_to_query_suffix and _seasons_available_in_results."""

    def test_quality_to_suffix_maps_known_qualities(self):
        from formatters import _quality_to_query_suffix
        self.assertEqual(_quality_to_query_suffix("1080"), " 1080p")
        self.assertEqual(_quality_to_query_suffix("4k"), " 2160p")
        self.assertEqual(_quality_to_query_suffix("720"), " 720p")
        self.assertEqual(_quality_to_query_suffix("480"), " 480p")

    def test_quality_to_suffix_empty_for_unknown_quality(self):
        """Empty/unknown quality must return "" so the search is unfiltered."""
        from formatters import _quality_to_query_suffix
        self.assertEqual(_quality_to_query_suffix(""), "")
        self.assertEqual(_quality_to_query_suffix("sd"), "")  # not in the map
        self.assertEqual(_quality_to_query_suffix("garbage"), "")

    def test_seasons_available_extracts_unique_sorted_numbers(self):
        from formatters import _seasons_available_in_results
        results = [
            {"title": "Клиника / Scrubs / Сезон: 3 / Серии 1-22 [BDRip]"},
            {"title": "Клиника Сезон 1 1080p WEB-DL"},
            {"title": "Клиника · Сезон:5 (полный)"},
            {"title": "Драйв / Drive / 2-й сезон [WEB-DL]"},
            {"title": "Клиника Сезон 3 4K"},  # duplicate season 3
        ]
        self.assertEqual(_seasons_available_in_results(results), [1, 2, 3, 5])

    def test_seasons_available_returns_empty_for_no_season_marker(self):
        from formatters import _seasons_available_in_results
        results = [
            {"title": "Some Movie 2024 1080p"},
            {"title": "Another.Film.2023.BluRay"},
        ]
        self.assertEqual(_seasons_available_in_results(results), [])

    def test_seasons_available_handles_empty_input(self):
        from formatters import _seasons_available_in_results
        self.assertEqual(_seasons_available_in_results([]), [])

    def test_seasons_available_tolerates_missing_title_field(self):
        from formatters import _seasons_available_in_results
        results = [{"title": None}, {"other": "x"}]
        self.assertEqual(_seasons_available_in_results(results), [])

    def test_seasons_available_is_case_insensitive(self):
        """СЕЗОН / сезон / Сезон — must all be matched."""
        from formatters import _seasons_available_in_results
        results = [
            {"title": "Шоу / СЕЗОН: 1"},
            {"title": "Show / сезон: 2"},
            {"title": "Шоу / Сезон: 3"},
        ]
        self.assertEqual(_seasons_available_in_results(results), [1, 2, 3])

    def test_magnet_wait_text_uses_user_friendly_attempt_counter(self):
        from formatters import _magnet_wait_text
        text = _magnet_wait_text(2, 8)
        self.assertIn("Добавляю magnet-ссылку", text)
        self.assertIn("Ищу созданную задачу", text)
        self.assertIn("10-15 секунд", text)
        self.assertIn("Попытка 3 из 8", text)


class SeasonRegexCaseInsensitiveTests(unittest.TestCase):
    """Regression: regexps that detect/extract season numbers must all agree on
    case-insensitive matching. Without this guarantee, an upper-case title like
    'СЕЗОН: 1' could pass _plex_is_series (re.I) but fail extraction in
    _extract_season_from_query / _filter_by_season, leaving meta inconsistent."""

    def test_plex_is_series_handles_all_cyrillic_cases(self):
        from bot import _plex_is_series
        for variant in ("Сезон: 1", "сезон 1", "СЕЗОН: 1", "сЕзОн:1", "1-й сезон", "S01E02", "1x05"):
            self.assertTrue(_plex_is_series(f"Show / {variant}"),
                            f"variant {variant!r} not detected")

    def test_extract_season_handles_all_cyrillic_cases(self):
        from formatters import _extract_season_from_query
        self.assertEqual(_extract_season_from_query("Show СЕЗОН: 5 1080p"), 5)
        self.assertEqual(_extract_season_from_query("Show сезон 7"), 7)
        self.assertEqual(_extract_season_from_query("Show Сезон: 3"), 3)
        self.assertEqual(_extract_season_from_query("Show сЕзОн:9"), 9)
        self.assertEqual(_extract_season_from_query("Драйв / Drive / 1-й сезон [WEB-DL]"), 1)
        self.assertIsNone(_extract_season_from_query("Just a Movie"))

    def test_filter_by_season_handles_all_cyrillic_cases(self):
        from formatters import _extract_season_from_query, _filter_by_season
        results = [
            {"title": "Show / СЕЗОН: 3 / 1080p"},
            {"title": "Show / сезон: 3 / 720p"},
            {"title": "Show / Сезон: 3 / 4K"},
            {"title": "Show / 3-й сезон / WEB-DL"},
            {"title": "Show / Сезон: 4 / 1080p"},
            {"title": "Show / 13-й сезон / WEB-DL"},
        ]
        # All three case-variants of season 3 must match, season 4 must not.
        filtered = _filter_by_season(results, 3)
        self.assertEqual(len(filtered), 4)
        for r in filtered:
            self.assertEqual(_extract_season_from_query(r["title"]), 3)

    def test_parse_episode_info_handles_uppercase_serii(self):
        """СЕРИИ: 1-8 из 10 must be parsed identically to 'Серии: 1-8 из 10'."""
        from formatters import _parse_episode_info
        self.assertEqual(_parse_episode_info("Show СЕРИИ: 1-8 из 10"), (8, 10))
        self.assertEqual(_parse_episode_info("Show серия: 5-9 из 10"), (9, 10))
        self.assertEqual(_parse_episode_info("Show СЕРИЯ: 2-4 из 8"), (4, 8))

    def test_extract_series_base_query_handles_uppercase_sezon(self):
        """Series detection must work for СЕЗОН as well as Сезон/сезон."""
        from formatters import _extract_series_base_query
        self.assertEqual(_extract_series_base_query("Шоу / СЕЗОН: 3 / blah"), "Шоу")
        self.assertEqual(_extract_series_base_query("Шоу / сезон 1"), "Шоу")
        self.assertEqual(_extract_series_base_query("Драйв / Drive / 1-й сезон"), "Драйв")
        self.assertIsNone(_extract_series_base_query("Movie 2024 1080p"))

    def test_format_sub_title_handles_uppercase_sezon(self):
        """Sub-title formatting must extract the season number regardless of case."""
        from formatters import _format_sub_title
        result = _format_sub_title("Шоу / Show / СЕЗОН: 5 / Серии: 1-3")
        self.assertIn("Сезон 5", result)
        self.assertEqual(
            _format_sub_title("Драйв / Drive / 1-й сезон [WEB-DL]"),
            "Драйв / Сезон 1",
        )

    def test_jackett_normalize_title_strips_uppercase_sezon_parts(self):
        """jackett_subscriptions._normalize_title must drop 'СЕЗОН' / 'СЕРИИ' parts."""
        from jackett_subscriptions import _normalize_title
        # 'СЕЗОН: 5' and 'СЕРИИ: 1-3' parts must be stripped — same as lowercase.
        out = _normalize_title("Шоу / Show / СЕЗОН: 5 / СЕРИИ: 1-3 [1080p]")
        # Lowercase keywords proves the function returns lowercased title; key thing
        # is that 'сезон' / 'серии' parts are gone from the result.
        self.assertNotIn("сезон", out)
        self.assertNotIn("серии", out)


class EnglishSeriesFormatTests(unittest.TestCase):
    """Regression: regexps that detect/extract season+episode markers must
    recognise the English ``SxxExx`` form used by Jackett-fed foreign trackers
    (e.g. 'Аркейн / Arcane / S2E1-9 of 9 [...]'). Without this, a query like
    'Аркейн сезон 1' returned 0 hits because all results came back with
    English-form titles that the filter rejected.
    """

    def test_extract_season_from_query_recognises_english(self):
        from formatters import _extract_season_from_query
        self.assertEqual(_extract_season_from_query("Аркейн сезон 1"), 1)
        self.assertEqual(_extract_season_from_query("Arcane S01"), 1)
        self.assertEqual(_extract_season_from_query("Show S1E3"), 1)
        self.assertEqual(_extract_season_from_query("Show s02"), 2)
        self.assertIsNone(_extract_season_from_query("Movie 2024"))

    def test_filter_by_season_keeps_english_form(self):
        from formatters import _filter_by_season
        results = [
            {"title": "Аркейн / Arcane / S01E1-9 of 9 [WEB-DL]"},
            {"title": "Show / S1 / 1080p"},
            {"title": "Show / Сезон: 1 / 1080p"},
            {"title": "Show / Сезон: 01 / 1080p"},
            {"title": "Show / S01E03 / 1080p"},
        ]
        filtered = _filter_by_season(results, 1)
        self.assertEqual(len(filtered), 5)

    def test_filter_by_season_rejects_other_seasons(self):
        from formatters import _filter_by_season
        results = [
            {"title": "Show / S02E03 / 1080p"},
            {"title": "Show / Сезон: 2 / 1080p"},
            {"title": "Show / S10E01 / 1080p"},  # must NOT match season 1
            {"title": "Show / S01E03 / 1080p"},  # must match season 1
        ]
        filtered = _filter_by_season(results, 1)
        self.assertEqual(len(filtered), 1)
        self.assertIn("S01E03", filtered[0]["title"])

    def test_seasons_available_in_results_recognises_english(self):
        from formatters import _seasons_available_in_results
        results = [
            {"title": "Show / S01E1-9 of 9 / [WEB-DL]"},
            {"title": "Show / Сезон: 2 / 1080p"},
            {"title": "Show / S03E05 / 720p"},
        ]
        self.assertEqual(_seasons_available_in_results(results), [1, 2, 3])

    def test_extract_series_base_query_recognises_english(self):
        from formatters import _extract_series_base_query
        self.assertEqual(
            _extract_series_base_query("Аркейн / Arcane: League of Legends / S2E1-9 of 9 [...]"),
            "Аркейн",
        )
        self.assertEqual(
            _extract_series_base_query("Шоу / Show / S01E03 / 1080p"),
            "Шоу",
        )
        self.assertEqual(
            _extract_series_base_query("Clarksons.Farm.S05E01.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv"),
            "Clarksons Farm",
        )
        self.assertIsNone(_extract_series_base_query("Movie 2024 1080p"))

    def test_parse_episode_info_recognises_english_with_of(self):
        from formatters import _parse_episode_info
        self.assertEqual(_parse_episode_info("Аркейн / Arcane / S2E1-9 of 9 [...]"), (9, 9))
        self.assertEqual(_parse_episode_info("Show / S1E3-7 of 10"), (7, 10))

    def test_parse_episode_info_recognises_english_without_of(self):
        """When 'of N' is absent, total falls back to last_episode_end (treat as complete)."""
        from formatters import _parse_episode_info
        self.assertEqual(_parse_episode_info("Show / S2E1-9 [WEB-DL]"), (9, 9))
        self.assertEqual(_parse_episode_info("Show / S1E3-7"), (7, 7))

    def test_parse_episode_info_prefers_russian_when_both_present(self):
        """If both Russian and English forms are in the title, Russian wins
        (mixed-language Rutracker titles always include Russian form authoritatively).
        """
        from formatters import _parse_episode_info
        # Russian says (8, 10), English says (9, 9) — Russian must win.
        self.assertEqual(
            _parse_episode_info("Show / S1E1-9 of 9 / Серии: 1-8 из 10"),
            (8, 10),
        )


class BuildTaskMetaTests(unittest.TestCase):
    """Tests for _build_task_meta_from_result and _build_task_meta_from_title."""

    def test_movie_result_produces_movie_meta(self):
        from bot import _build_task_meta_from_result
        result = {
            "movie_title": "Dune: Part Two",
            "title": "Dune.Part.Two.2024.2160p.WEB-DL",
            "year": 2024,
            "quality": "2160p",
        }
        meta = _build_task_meta_from_result(result, source="search")
        self.assertEqual(meta["kind"], "movie")
        self.assertEqual(meta["title"], "Dune: Part Two")
        self.assertEqual(meta["year"], 2024)
        self.assertEqual(meta["quality"], "4k")
        self.assertEqual(meta["source"], "search")
        self.assertNotIn("series_query", meta)

    def test_series_result_produces_series_meta(self):
        from bot import _build_task_meta_from_result
        result = {
            "movie_title": "Клиника / Scrubs / Сезон: 3 / Серии 1-22",
            "title": "Клиника / Scrubs / Сезон: 3 / Серии 1-22 [BDRip 1080p]",
            "year": 2003,
            "quality": "1080p",
        }
        meta = _build_task_meta_from_result(result, source="search")
        self.assertEqual(meta["kind"], "series")
        self.assertEqual(meta["series_query"], "Клиника")
        self.assertEqual(meta["season_num"], 3)
        self.assertEqual(meta["quality"], "1080")
        self.assertEqual(meta["source"], "search")

    def test_series_result_detects_ordinal_russian_season_marker(self):
        from bot import _build_task_meta_from_result
        result = {
            "title": "Драйв / Drive / 1-й сезон [WEB-DL 1080p]",
            "year": 2020,
            "quality": "1080p",
        }
        meta = _build_task_meta_from_result(result, source="search")
        self.assertEqual(meta["kind"], "series")
        self.assertEqual(meta["series_query"], "Драйв")
        self.assertEqual(meta["season_num"], 1)
        self.assertEqual(meta["quality"], "1080")

    def test_from_title_detects_movie_when_no_season_marker(self):
        from bot import _build_task_meta_from_title
        meta = _build_task_meta_from_title("Dune.Part.Two.2024.1080p", source="torrent_file")
        self.assertEqual(meta["kind"], "movie")
        self.assertEqual(meta["year"], 2024)
        self.assertEqual(meta["quality"], "1080")
        self.assertEqual(meta["source"], "torrent_file")

    def test_from_title_detects_series_via_S01E01(self):
        from bot import _build_task_meta_from_title
        meta = _build_task_meta_from_title("Schitts.Creek.S03E05.1080p", source="magnet")
        self.assertEqual(meta["kind"], "series")
        self.assertEqual(meta["series_query"], "Schitts Creek")
        self.assertEqual(meta["quality"], "1080")
        self.assertEqual(meta["source"], "magnet")

    def test_from_title_handles_missing_year_quality_gracefully(self):
        from bot import _build_task_meta_from_title
        meta = _build_task_meta_from_title("RandomFile", source="torrent_file")
        self.assertEqual(meta["kind"], "movie")
        self.assertEqual(meta["year"], 0)
        self.assertEqual(meta["quality"], "")

    def test_from_title_with_gpt_adds_release_metadata(self):
        parsed = {
            "quality": "2160p",
            "source": "WEB-DL",
            "hdr": "HDR10",
            "audio": "EAC3 5.1",
            "langs": ["RUS", "ENG"],
            "release_group": "NTb",
            "edition": None,
        }
        store = MagicMock()
        store.load_torrent_titles_cache.return_value = {}

        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "state_store", store),
            patch.object(bot, "gpt_features_parse_torrent_title",
                         return_value=(parsed, None)) as parse,
            patch.object(bot, "_gpt_record_usage") as usage,
        ):
            meta = asyncio.run(bot._build_task_meta_from_title_with_gpt(
                "Dune.Part.Two.2024.2160p.WEB-DL.HDR10.EAC3.NTb",
                source="magnet",
            ))

        self.assertEqual(meta["quality"], "4k")
        self.assertEqual(meta["release"]["quality"], "2160p")
        self.assertEqual(meta["release"]["hdr"], "HDR10")
        self.assertEqual(meta["release"]["audio"], "EAC3 5.1")
        parse.assert_called_once()
        usage.assert_called_once()
        store.save_torrent_titles_cache.assert_called_once()


class DownloadHistoryTests(unittest.TestCase):
    def test_added_history_sanitizes_magnet_and_jackett_proxy_urls(self):
        result = {
            "title": "Show S01 1080p",
            "movie_title": "Show / Season: 1",
            "url": "http://jackett:9117/dl/rutracker/?apikey=secret",
            "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "magnet_url": "magnet:?xt=urn:btih:secret",
            "torrent_url": "http://jackett:9117/dl/rutracker/?apikey=secret",
            "tracker_name": "RuTracker.org",
            "source": "jackett",
            "parsed_meta": {
                "quality": "1080p",
                "source": "WEB-DL",
                "audio": "AC3 5.1",
                "langs": ["Rus", "Eng"],
            },
        }
        store = MagicMock()

        with patch.object(bot, "state_store", store):
            bot._record_download_added_history(
                "dbid_1",
                100,
                result,
                method="torrent-file",
                meta_source="search",
            )

        entry = store.append_download_history.call_args.args[0]
        self.assertEqual(entry["event"], "download_added")
        self.assertEqual(entry["chat_id"], 100)
        self.assertEqual(entry["task_id"], "dbid_1")
        self.assertEqual(entry["topic_id"], "12345")
        self.assertEqual(entry["topic_url"], "https://rutracker.org/forum/viewtopic.php?t=12345")
        self.assertNotIn("magnet_url", entry)
        self.assertNotIn("torrent_url", entry)
        self.assertNotIn("apikey", str(entry).lower())
        self.assertNotIn("magnet:", str(entry).lower())
        self.assertEqual(entry["release"]["langs"], ["Rus", "Eng"])

    def test_added_history_from_title_keeps_gpt_release_meta(self):
        store = MagicMock()
        meta = {
            "kind": "movie",
            "title": "Dune",
            "year": 2024,
            "quality": "4k",
            "source": "magnet",
            "release": {"hdr": "HDR10", "audio": "EAC3 5.1"},
        }

        with patch.object(bot, "state_store", store):
            bot._record_download_added_from_title_history(
                "dbid_2",
                100,
                "Dune.2024.2160p",
                method="magnet-ссылка",
                meta_source="magnet",
                meta=meta,
            )

        entry = store.append_download_history.call_args.args[0]
        self.assertEqual(entry["release"], {"hdr": "HDR10", "audio": "EAC3 5.1"})


class TaskMetaWrapperTests(unittest.TestCase):
    """Tests for the bot.py wrappers around state_store task_meta methods."""

    def test_get_task_meta_returns_none_for_unknown_id(self):
        with patch("bot.state_store") as st:
            st.load_task_meta.return_value = {}
            from bot import _get_task_meta
            self.assertIsNone(_get_task_meta("missing"))

    def test_get_task_meta_returns_entry_when_present(self):
        sample = {"tid1": {"kind": "movie", "title": "X"}}
        with patch("bot.state_store") as st:
            st.load_task_meta.return_value = sample
            from bot import _get_task_meta
            self.assertEqual(_get_task_meta("tid1"), {"kind": "movie", "title": "X"})

    def test_remember_task_meta_skips_empty_inputs(self):
        with patch("bot.state_store") as st:
            from bot import _remember_task_meta
            _remember_task_meta("", {"kind": "movie"})
            _remember_task_meta("tid", None)
        st.remember_task_meta.assert_not_called()


class PlexShowFindTests(unittest.TestCase):
    """Tests for _plex_show_find — TV show lookup in the in-memory Plex cache."""

    def _make_show(self, title: str, year: int) -> "object":
        from plex import PlexShow
        return PlexShow(title=title, year=year, rating_key=str(year * 100), seasons={})

    def test_finds_show_by_exact_title_and_year(self):
        from bot import _plex_show_find
        show = self._make_show("Schitt's Creek", 2015)
        with patch.object(bot, "_plex_shows_library", {("schitt s creek", 2015): show}):
            self.assertIs(_plex_show_find("Schitt's Creek", 2015), show)

    def test_finds_show_with_possessive_apostrophe_variant(self):
        from bot import _plex_show_find
        show = self._make_show("Clarkson's Farm", 2021)
        with patch.object(bot, "_plex_shows_library", {("clarkson s farm", 2021): show}):
            self.assertIs(_plex_show_find("Clarksons Farm", 2021), show)

    def test_year_tolerance_plus_minus_one(self):
        from bot import _plex_show_find
        show = self._make_show("X", 2020)
        with patch.object(bot, "_plex_shows_library", {("x", 2020): show}):
            # ±1 tolerance: 2019, 2020, 2021 — все находят show.
            self.assertIs(_plex_show_find("X", 2021), show)
            self.assertIs(_plex_show_find("X", 2019), show)

    def test_zero_year_scans_by_title_across_all_years(self):
        from bot import _plex_show_find
        show = self._make_show("Test", 2010)
        with patch.object(bot, "_plex_shows_library", {("test", 2010): show}):
            self.assertIs(_plex_show_find("Test", 0), show)

    def test_returns_none_when_no_match(self):
        from bot import _plex_show_find
        with patch.object(bot, "_plex_shows_library", {}):
            self.assertIsNone(_plex_show_find("Unknown Show", 0))

    def test_lookup_logs_diagnostic_when_series_show_not_found(self):
        """When _plex_poll_lookup_target can't find a series, it must log an
        INFO line with the query / year / cache size — so operators can
        diagnose «не появился в Plex» without reading code."""
        from bot import _plex_poll_lookup_target
        meta = {"kind": "series", "series_query": "Nonexistent", "season_num": 1, "year": 2026}
        with (
            patch.object(bot, "_plex_shows_library", {}),
            self.assertLogs("tg_torrent_drop", level="INFO") as captured,
        ):
            target, mt, ft = asyncio.run(_plex_poll_lookup_target("task title", meta))
        self.assertIsNone(target)
        joined = "\n".join(captured.output)
        self.assertIn("Plex lookup: series show not found", joined)
        self.assertIn("Nonexistent", joined)

    def test_lookup_logs_diagnostic_when_movie_not_found(self):
        """Same for movie path — log diagnostic when nothing matches."""
        from bot import _plex_poll_lookup_target
        meta = {"kind": "movie", "title": "Nonexistent Film", "year": 2026}
        with (
            patch.object(bot, "_plex_library", {}),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_plex_library_find", return_value=None),
            self.assertLogs("tg_torrent_drop", level="INFO") as captured,
        ):
            target, mt, ft = asyncio.run(_plex_poll_lookup_target("task.title.2026.mkv", meta))
        self.assertIsNone(target)
        joined = "\n".join(captured.output)
        self.assertIn("Plex lookup: movie not found", joined)

    def test_year_mismatch_for_series_falls_back_to_title_only(self):
        """Regression: for TV series, meta.year often reflects the season/episode
        year (e.g. 2026 for Good Omens S3E1) while Plex caches the show under
        its PREMIERE year (2019). Without title-only fallback, the lookup
        returns None and the «✅ добавлен в Plex» notification is never sent.
        """
        from bot import _plex_show_find
        show = self._make_show("Good Omens", 2019)
        with patch.object(bot, "_plex_shows_library", {("good omens", 2019): show}):
            # meta.year=2026 (episode year), Plex.year=2019 (premiere) — gap > 1.
            self.assertIs(_plex_show_find("Good Omens", 2026), show)
            # Also works for wider gaps.
            self.assertIs(_plex_show_find("Good Omens", 2100), show)

    def test_year_match_still_takes_priority_over_title_only(self):
        """When two shows share a normalised title under different years and the
        requested year matches one exactly, that one wins over the fallback."""
        from bot import _plex_show_find
        old_show = self._make_show("Same Title", 2010)
        new_show = self._make_show("Same Title", 2026)
        with patch.object(
            bot,
            "_plex_shows_library",
            {("same title", 2010): old_show, ("same title", 2026): new_show},
        ):
            # year=2026 exact → new_show; ±1 tolerance is checked before title-only.
            self.assertIs(_plex_show_find("Same Title", 2026), new_show)
            # year=2010 exact → old_show.
            self.assertIs(_plex_show_find("Same Title", 2010), old_show)
            # year=2027 (±1 of 2026) → new_show.
            self.assertIs(_plex_show_find("Same Title", 2027), new_show)


class PlexEnsureShowSeasonsTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _plex_ensure_show_seasons — lazy season loading."""

    async def test_returns_cached_seasons_without_api_call(self):
        from bot import _plex_ensure_show_seasons
        from plex import PlexShow, PlexSeason
        cached = {1: PlexSeason("k1", 1, 10, [], "1080")}
        show = PlexShow(title="X", year=2020, rating_key="100", seasons=cached)
        # plex_client.get_show_seasons must NOT be called when seasons are cached.
        fake_client = MagicMock()
        with patch.object(bot, "plex_client", fake_client):
            result = await _plex_ensure_show_seasons(show)
        self.assertIs(result, cached)
        fake_client.get_show_seasons.assert_not_called()

    async def test_fetches_seasons_when_show_seasons_empty(self):
        from bot import _plex_ensure_show_seasons
        from plex import PlexShow, PlexSeason
        new_seasons = {1: PlexSeason("k1", 1, 8, [], "720")}
        show = PlexShow(title="X", year=2020, rating_key="100", seasons={})
        fake_client = MagicMock()
        fake_client.get_show_seasons = MagicMock(return_value=new_seasons)
        with patch.object(bot, "plex_client", fake_client):
            result = await _plex_ensure_show_seasons(show)
        self.assertEqual(result, new_seasons)
        # And the result was cached on the show
        self.assertEqual(show.seasons, new_seasons)

    async def test_returns_empty_dict_on_api_failure(self):
        from bot import _plex_ensure_show_seasons
        from plex import PlexShow
        show = PlexShow(title="X", year=2020, rating_key="100", seasons={})
        fake_client = MagicMock()
        fake_client.get_show_seasons = MagicMock(side_effect=Exception("boom"))
        with patch.object(bot, "plex_client", fake_client):
            result = await _plex_ensure_show_seasons(show)
        self.assertEqual(result, {})


class PlexCacheInfoIncludesShowsTests(unittest.TestCase):
    def test_show_count_and_shows_updated_at_present(self):
        from plex import PlexShow
        from bot import _plex_cache_info
        with (
            patch.object(bot, "_plex_shows_library", {("x", 2020): PlexShow("X", 2020, "1", {})}),
            patch.object(bot, "_plex_shows_updated_at", 1700000000.0),
        ):
            info = _plex_cache_info()
        self.assertEqual(info["show_count"], 1)
        self.assertTrue(info["shows_updated_at"])  # non-empty formatted timestamp


class PlexPreCheckSeriesTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _plex_pre_check_series — TV-season variant of pre-download check."""

    def _make_show_with_seasons(self, *, season_resolution: str = "1080"):
        from plex import PlexShow, PlexSeason
        season = PlexSeason("seasonkey", 3, episode_count=10, file_paths=[],
                            resolution=season_resolution)
        show = PlexShow("Клиника", 2001, "showkey", seasons={3: season})
        return show, season

    async def test_returns_warn_same_when_quality_matches(self):
        from bot import _plex_pre_check_series
        show, _ = self._make_show_with_seasons(season_resolution="1080")
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("клиника", 2001): show}),
        ):
            result = await _plex_pre_check_series("Клиника", 3, "1080")
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "warn_same")
        self.assertEqual(result.season.season_number, 3)

    async def test_returns_offer_upgrade_when_plex_has_worse_quality(self):
        from bot import _plex_pre_check_series
        show, _ = self._make_show_with_seasons(season_resolution="720")
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("клиника", 2001): show}),
        ):
            result = await _plex_pre_check_series("Клиника", 3, "1080")
        self.assertEqual(result.action, "offer_upgrade")

    async def test_returns_warn_better_when_plex_has_better_quality(self):
        from bot import _plex_pre_check_series
        show, _ = self._make_show_with_seasons(season_resolution="4k")
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("клиника", 2001): show}),
        ):
            result = await _plex_pre_check_series("Клиника", 3, "1080")
        self.assertEqual(result.action, "warn_better")

    async def test_returns_none_when_quality_unknown(self):
        """Without a known requested_quality we can't decide → no warning."""
        from bot import _plex_pre_check_series
        show, _ = self._make_show_with_seasons()
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("клиника", 2001): show}),
        ):
            self.assertIsNone(await _plex_pre_check_series("Клиника", 3, ""))

    async def test_returns_none_when_show_not_in_plex(self):
        from bot import _plex_pre_check_series
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {}),
        ):
            self.assertIsNone(await _plex_pre_check_series("Unknown Show", 3, "1080"))

    async def test_returns_none_when_season_not_in_show(self):
        """Show is in Plex but the specific season isn't."""
        from bot import _plex_pre_check_series
        show, _ = self._make_show_with_seasons()  # only has season 3
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("клиника", 2001): show}),
        ):
            self.assertIsNone(await _plex_pre_check_series("Клиника", 5, "1080"))

    async def test_returns_none_when_disabled(self):
        from bot import _plex_pre_check_series
        with patch.object(bot, "PLEX_ENABLED", False):
            self.assertIsNone(await _plex_pre_check_series("Клиника", 3, "1080"))

    async def test_returns_none_for_invalid_season_num(self):
        from bot import _plex_pre_check_series
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("x", 2020): MagicMock()}),
        ):
            self.assertIsNone(await _plex_pre_check_series("X", 0, "1080"))
            self.assertIsNone(await _plex_pre_check_series("X", -1, "1080"))
            self.assertIsNone(await _plex_pre_check_series("X", None, "1080"))


class PlexSeriesConfirmTextTests(unittest.TestCase):
    def _check(self, action: str, season_resolution: str = "1080"):
        from plex import PlexShow, PlexSeason, PlexSeriesCheckResult
        show = PlexShow("Клиника", 2001, "showkey", seasons={})
        season = PlexSeason("sk", 3, 10, [], season_resolution)
        return PlexSeriesCheckResult(show=show, season=season, action=action)

    def test_warn_same_text_mentions_show_and_season(self):
        from bot import _plex_series_confirm_text
        text = _plex_series_confirm_text(self._check("warn_same"), "Клиника / Сезон 3", "1080")
        self.assertIn("Сезон 3", text)
        self.assertIn("Клиника", text)
        self.assertIn("уже есть в Plex", text)
        self.assertIn("1080", text)

    def test_offer_upgrade_mentions_requested_quality(self):
        from bot import _plex_series_confirm_text
        text = _plex_series_confirm_text(
            self._check("offer_upgrade", season_resolution="720"), "Клиника", "1080"
        )
        self.assertIn("1080", text)
        self.assertIn("720", text)


class SeriesPlexSeasonsLineTests(unittest.TestCase):
    def test_empty_when_no_plex_seasons(self):
        from bot import _series_plex_seasons_line
        self.assertEqual(_series_plex_seasons_line(None, 5), "")
        self.assertEqual(_series_plex_seasons_line(set(), 5), "")

    def test_lists_sorted_seasons(self):
        from bot import _series_plex_seasons_line
        text = _series_plex_seasons_line({3, 1, 2}, 5)
        self.assertEqual(text, "В Plex: 1, 2, 3\n")

    def test_says_all_seasons_when_complete(self):
        from bot import _series_plex_seasons_line
        text = _series_plex_seasons_line({1, 2, 3}, 3)
        self.assertIn("Все сезоны", text)

    def test_no_total_seasons_falls_back_to_list(self):
        from bot import _series_plex_seasons_line
        text = _series_plex_seasons_line({1, 2}, None)
        self.assertEqual(text, "В Plex: 1, 2\n")


class GetPlexSeasonsForSeriesTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_empty_when_plex_disabled(self):
        from bot import _get_plex_seasons_for_series
        with patch.object(bot, "PLEX_ENABLED", False):
            self.assertEqual(await _get_plex_seasons_for_series("X"), set())

    async def test_returns_empty_when_show_not_found(self):
        from bot import _get_plex_seasons_for_series
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {}),
        ):
            self.assertEqual(await _get_plex_seasons_for_series("X"), set())

    async def test_returns_season_numbers_from_plex(self):
        from bot import _get_plex_seasons_for_series
        from plex import PlexShow, PlexSeason
        show = PlexShow(
            "X", 2020, "1",
            seasons={1: PlexSeason("a", 1, 10, [], "1080"), 2: PlexSeason("b", 2, 8, [], "1080")},
        )
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_plex_shows_library", {("x", 2020): show}),
        ):
            self.assertEqual(await _get_plex_seasons_for_series("X"), {1, 2})


class SearchSeriesEntryHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Tests for the 'Другой сезон' entry point."""

    async def test_without_kinopoisk_still_offers_manual_season_picker(self):
        from bot import search_series_entry, SEARCH_SEASON_SELECT
        update = _make_callback_update(callback_data="srch:series_base")
        context = _make_context(user_data={
            "srch_series_query": "Клиника",
            "srch_picked_quality": "1080",
        })

        with (
            patch.object(bot, "kinopoisk_client", None),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())),
            patch.object(bot, "_execute_search", AsyncMock()) as exec_mock,
        ):
            result = await search_series_entry(update, context)

        self.assertEqual(result, SEARCH_SEASON_SELECT)
        exec_mock.assert_not_awaited()
        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        text = edit.call_args.args[0]
        self.assertIn("«Клиника»", text)
        self.assertIn("1080p", text)
        keyboard = edit.call_args.kwargs["reply_markup"]
        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["✏️ Свой номер"], "srch:season_input")
        self.assertEqual(buttons["🔎 Без сезона"], "srch:season_skip")
        self.assertEqual(context.user_data["srch_total_seasons"], None)
        self.assertEqual(context.user_data["srch_base_title"], "Клиника")


class SearchSeasonBackHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Tests for search_season_back — the '⬅️ Назад' button in the season picker."""

    async def test_back_restores_success_message_and_reopens_offer(self):
        """Tapping back must restore the success_text + 'Другой сезон' keyboard
        AND re-arm srch_series_query so the user can open the picker again."""
        from bot import search_season_back, SEARCH_RESULTS
        update = _make_callback_update(callback_data="srch:season_back")
        context = _make_context(user_data={
            "srch_series_success_text": "✅ Клиника Сезон 2 добавлен",
            "srch_series_success_task_id": "dbid_777",
            "srch_base_title": "Клиника",
        })
        result = await search_season_back(update, context)

        self.assertEqual(result, SEARCH_RESULTS)
        # The success text was restored verbatim
        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        self.assertEqual(edit.call_args.args[0], "✅ Клиника Сезон 2 добавлен")
        # And the keyboard is the after-add one (has '🔎 Другой сезон' button)
        keyboard = edit.call_args.kwargs["reply_markup"]
        button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🔎 Другой сезон", button_texts)
        # srch_series_query re-armed for the next 'Другой сезон' tap
        self.assertEqual(context.user_data.get("srch_series_query"), "Клиника")

    async def test_back_handles_lost_state_gracefully(self):
        """If success_text is missing (state expired), close politely instead of crashing."""
        from bot import search_season_back
        from telegram.ext import ConversationHandler
        update = _make_callback_update(callback_data="srch:season_back")
        context = _make_context(user_data={})  # no series state
        result = await search_season_back(update, context)
        self.assertEqual(result, ConversationHandler.END)
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Запрос потерян", text)


class SearchSeasonManualInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_input_prompt_has_back_cancel_and_tracks_message(self):
        from bot import search_season_input_ask, SEARCH_SEASON_SELECT

        update = _make_callback_update(callback_data="srch:season_input")
        context = _make_context(user_data={"srch_base_title": "Клиника"})

        result = await search_season_input_ask(update, context)

        self.assertEqual(result, SEARCH_SEASON_SELECT)
        self.assertEqual(context.user_data["srch_season_input_msg_id"], 42)
        self.assertEqual(context.user_data["srch_season_input_chat_id"], 100)
        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        self.assertIn("Клиника", edit.call_args.args[0])
        buttons = {
            button.text: button.callback_data
            for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["⬅️ К выбору сезона"], "srch:season_back_to_picker")
        self.assertEqual(buttons["❌ Отмена"], "srch:cancel")

    async def test_invalid_manual_input_edits_prompt_without_new_message(self):
        from bot import search_season_got_input, SEARCH_SEASON_SELECT

        update = _make_message_update()
        update.message.text = "0"
        context = _make_context(user_data={
            "srch_base_title": "Клиника",
            "srch_season_input_msg_id": 77,
            "srch_season_input_chat_id": 100,
        })

        result = await search_season_got_input(update, context)

        self.assertEqual(result, SEARCH_SEASON_SELECT)
        context.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)
        context.bot.edit_message_text.assert_awaited_once()
        kwargs = context.bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 100)
        self.assertEqual(kwargs["message_id"], 77)
        self.assertIn("положительный номер сезона", kwargs["text"])
        update.message.reply_text.assert_not_awaited()

    async def test_valid_manual_input_deletes_prompt_and_runs_search(self):
        from bot import search_season_got_input, SEARCH_RESULTS

        update = _make_message_update()
        update.message.text = "7"
        context = _make_context(user_data={
            "srch_base_title": "Клиника",
            "srch_picked_quality": "1080",
            "srch_season_input_msg_id": 77,
            "srch_season_input_chat_id": 100,
        })

        with patch.object(bot, "_run_search", AsyncMock(return_value=SEARCH_RESULTS)) as run_search:
            result = await search_season_got_input(update, context)

        self.assertEqual(result, SEARCH_RESULTS)
        context.bot.delete_message.assert_any_await(chat_id=100, message_id=42)
        context.bot.delete_message.assert_any_await(chat_id=100, message_id=77)
        self.assertNotIn("srch_season_input_msg_id", context.user_data)
        self.assertNotIn("srch_season_input_chat_id", context.user_data)
        self.assertEqual(run_search.await_args.args[2], "Клиника Сезон: 7 1080p")


class SearchDownloadModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_click_toggles_to_series_and_rerenders_options(self):
        update = _make_callback_update(callback_data="srch:mode:options")
        context = _make_context(user_data={"srch_query": "Клиника"})

        result = await bot.search_choose_mode(update, context)

        self.assertEqual(result, bot.SEARCH_OPTIONS)
        self.assertEqual(context.user_data["srch_intent"], bot.SEARCH_INTENT_SERIES_MASTER)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Скачать сериал целиком", text)
        buttons = {
            button.text: button.callback_data
            for row in update.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["🎚 Что скачать: сериал целиком"], "srch:mode:options")

    async def test_mode_click_toggles_to_single_without_losing_filters(self):
        update = _make_callback_update(callback_data="srch:mode:advanced")
        context = _make_context(user_data={
            "srch_query": "Клиника",
            "srch_intent": bot.SEARCH_INTENT_SERIES_MASTER,
            "srch_settings": {"quality": "720p", "audio": True, "subs": False},
        })

        result = await bot.search_choose_mode(update, context)

        self.assertEqual(result, bot.SEARCH_ADVANCED)
        self.assertNotIn("srch_intent", context.user_data)
        self.assertEqual(context.user_data["srch_settings"]["quality"], "720p")
        buttons = {
            button.text: button.callback_data
            for row in update.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["🎚 Что скачать: одна раздача"], "srch:mode:advanced")

    async def test_new_text_query_resets_series_mode(self):
        update = _make_message_update(chat_id=100)
        update.message.text = "Дюна"
        reply = MagicMock()
        reply.message_id = 77
        update.message.reply_text.return_value = reply
        context = _make_context(user_data={"srch_intent": bot.SEARCH_INTENT_SERIES_MASTER})

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "rutracker_client", MagicMock()),
            patch.object(bot, "jackett_client", None),
        ):
            result = await bot.text_message_entry(update, context)

        self.assertEqual(result, bot.SEARCH_OPTIONS)
        self.assertNotIn("srch_intent", context.user_data)
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Что скачать: одна раздача", text)

    async def test_youtube_link_disabled_does_not_start_search(self):
        update = _make_message_update(chat_id=100)
        update.message.text = "https://youtu.be/abcdefghijk"
        context = _make_context()
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", store),
            patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", False),
            patch.object(bot, "search_got_query", AsyncMock()) as search,
        ):
            result = await bot.text_message_entry(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        search.assert_not_awaited()
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("YouTube-download сейчас отключён", text)

    async def test_youtube_link_enabled_shows_preview(self):
        update = _make_message_update(chat_id=100)
        update.message.text = "https://youtu.be/abcdefghijk"
        status = MagicMock()
        status.message_id = 77
        status.edit_text = AsyncMock()
        update.message.reply_text.return_value = status
        context = _make_context()
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        info = {
            "id": "abcdefghijk",
            "title": "Test clip",
            "channel": "Channel",
            "duration": 120,
            "formats": [
                {
                    "format_id": "22",
                    "ext": "mp4",
                    "height": 720,
                    "vcodec": "avc1.64001F",
                    "acodec": "mp4a.40.2",
                }
            ],
        }

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", store),
            patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
            patch.object(bot, "_youtube_extract_metadata", MagicMock(return_value=info)),
        ):
            result = await bot.text_message_entry(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        text = status.edit_text.await_args.args[0]
        self.assertIn("Test clip", text)
        keyboard = status.edit_text.await_args.kwargs["reply_markup"]
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertTrue(any(callback.startswith("yt:dl:") for callback in callbacks))

    async def test_youtube_callback_creates_queue_job(self):
        update = _make_callback_update(chat_id=100, callback_data="yt:dl:tok123:720")
        context = _make_context()
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            bot.YOUTUBE_PREVIEWS["tok123"] = {
                "url": "https://www.youtube.com/watch?v=abcdefghijk",
                "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "video_id": "abcdefghijk",
                "title": "Test clip",
                "channel": "Channel",
                "duration_seconds": 120,
                "qualities": [
                    {
                        "height": 720,
                        "label": "720p",
                        "format_id": "22",
                        "filesize": 1000,
                    }
                ],
            }
            try:
                with (
                    patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                    patch.object(bot, "ADMIN_CHAT_IDS", set()),
                    patch.object(bot, "state_store", store),
                    patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                ):
                    await bot.youtube_callback(update, context)

                jobs = store.load_youtube_downloads()
                self.assertEqual(len(jobs), 1)
                job = next(iter(jobs.values()))
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["video_id"], "abcdefghijk")
                self.assertEqual(job["target_height"], 720)
                history = store.load_download_history(chat_id=100)
                self.assertEqual(history[-1]["event"], "youtube_download_queued")
                self.assertEqual(history[-1]["source"], "youtube")
            finally:
                bot.YOUTUBE_PREVIEWS.pop("tok123", None)
                bot.YOUTUBE_JOB_MESSAGES.clear()

    async def test_youtube_callback_still_queues_when_answer_times_out(self):
        update = _make_callback_update(chat_id=100, callback_data="yt:dl:tok123:720")
        update.callback_query.answer.side_effect = bot.TimedOut("Timed out")
        context = _make_context()
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            bot.YOUTUBE_PREVIEWS["tok123"] = {
                "url": "https://www.youtube.com/watch?v=abcdefghijk",
                "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "video_id": "abcdefghijk",
                "title": "Test clip",
                "channel": "Channel",
                "duration_seconds": 120,
                "qualities": [
                    {
                        "height": 720,
                        "label": "720p",
                        "format_id": "22",
                        "filesize": 1000,
                    }
                ],
            }
            try:
                with (
                    patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                    patch.object(bot, "ADMIN_CHAT_IDS", set()),
                    patch.object(bot, "state_store", store),
                    patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                ):
                    await bot.youtube_callback(update, context)

                jobs = store.load_youtube_downloads()
                self.assertEqual(len(jobs), 1)
                self.assertEqual(next(iter(jobs.values()))["status"], "queued")
            finally:
                bot.YOUTUBE_PREVIEWS.pop("tok123", None)
                bot.YOUTUBE_JOB_MESSAGES.clear()

    def test_youtube_job_card_shows_separate_audio_checklist(self):
        text = bot._youtube_job_card_text({
            "id": "yt_1",
            "status": "downloading",
            "title": "Test clip",
            "quality": "720p",
            "format_id": "137+140",
            "media_step": "audio",
            "video_done": True,
            "downloaded_bytes": 10,
            "total_bytes": 100,
            "speed_bytes": 1024 * 1024,
            "eta_seconds": 4,
        })

        self.assertIn("✅ Скачивание видео", text)
        self.assertIn("⬇️ Скачивание аудио", text)
        self.assertIn("Текущий файл: 10% · 1.0 MB/с · осталось 0:04", text)
        self.assertIn("☐ Сборка MP4", text)
        self.assertIn("☐ Обложка и metadata", text)

    def test_youtube_job_card_hides_audio_step_for_progressive_format(self):
        text = bot._youtube_job_card_text({
            "id": "yt_1",
            "status": "downloading",
            "title": "Test clip",
            "quality": "720p",
            "format_id": "22",
            "media_step": "video",
        })

        self.assertIn("⬇️ Скачивание видео", text)
        self.assertNotIn("Скачивание аудио", text)
        self.assertNotIn("Сборка MP4", text)

    def test_youtube_job_card_does_not_show_video_id_as_title(self):
        text = bot._youtube_job_card_text({
            "id": "yt_1",
            "video_id": "abcdefghijk",
            "status": "queued",
            "quality": "720p",
            "format_id": "22",
        })

        self.assertNotIn("abcdefghijk", text)

    def test_youtube_job_card_shows_retry_status(self):
        text = bot._youtube_job_card_text({
            "id": "yt_1",
            "status": "downloading",
            "title": "Test clip",
            "quality": "720p",
            "format_id": "22",
            "media_step": "video",
            "retry_attempt": 2,
            "retry_max_attempts": 3,
            "retry_reason": "сетевой таймаут YouTube",
        })

        self.assertIn("Повтор 2/3: сетевой таймаут YouTube", text)

    def test_youtube_failure_message_keeps_download_error_readable(self):
        text = bot._youtube_failure_message(
            bot.YouTubeDownloadError("Не удалось скачать видео: сетевой таймаут YouTube. Повторите позже.")
        )

        self.assertEqual(text, "Не удалось скачать видео: сетевой таймаут YouTube. Повторите позже.")

    def test_youtube_progress_hook_marks_second_stream_as_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "status": "queued",
                    "format_id": "137+140",
                    "title": "Test clip",
                }
            })
            with patch.object(bot, "state_store", store):
                hook = bot._youtube_progress_hook("yt_1", separate_streams=True)
                hook({
                    "status": "downloading",
                    "filename": "video.mp4",
                    "downloaded_bytes": 100,
                    "total_bytes": 200,
                })
                hook({"status": "finished", "filename": "video.mp4"})
                hook({
                    "status": "downloading",
                    "filename": "audio.m4a",
                    "downloaded_bytes": 10,
                    "total_bytes": 50,
                })

            job = store.load_youtube_downloads()["yt_1"]
            self.assertEqual(job["status"], "downloading")
            self.assertTrue(job["video_done"])
            self.assertEqual(job["media_step"], "audio")
            self.assertEqual(job["downloaded_bytes"], 10)
            self.assertEqual(job["total_bytes"], 50)
            self.assertEqual(job["eta_seconds"], 0)

    def test_youtube_progress_hook_records_retry_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "status": "queued",
                    "format_id": "22",
                    "title": "Test clip",
                }
            })
            with patch.object(bot, "state_store", store):
                hook = bot._youtube_progress_hook("yt_1", separate_streams=False)
                hook({
                    "status": "retrying",
                    "attempt": 2,
                    "max_attempts": 3,
                    "reason": "сетевой таймаут YouTube",
                })

            job = store.load_youtube_downloads()["yt_1"]
            self.assertEqual(job["status"], "downloading")
            self.assertEqual(job["media_step"], "video")
            self.assertEqual(job["retry_attempt"], 2)
            self.assertEqual(job["retry_max_attempts"], 3)
            self.assertEqual(job["retry_reason"], "сетевой таймаут YouTube")

    async def test_youtube_set_collection_poster_uploads_channel_poster(self):
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "Clip"
            item_dir.mkdir()
            poster_path = Path(tmp) / "channel-poster.png"
            poster_path.write_bytes(b"poster")
            plex = MagicMock()
            plex.find_section_collection.return_value = MagicMock(rating_key="collection-1")
            plex.upload_poster.return_value = True
            plex.lock_collection_poster.return_value = True

            with patch.object(bot, "plex_client", plex):
                uploaded = await bot._youtube_set_collection_poster(
                    "9",
                    {
                        "id": "yt_1",
                        "channel": "AcademeG",
                        "item_dir": str(item_dir),
                        "channel_poster_path": str(poster_path),
                    },
                )

            self.assertTrue(uploaded)
            plex.find_section_collection.assert_called_once_with("9", "AcademeG")
            plex.upload_poster.assert_called_once_with("collection-1", poster_path)
            plex.lock_collection_poster.assert_called_once_with("9", "collection-1")

    async def test_youtube_set_collection_poster_fails_when_lock_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "Clip"
            item_dir.mkdir()
            poster_path = Path(tmp) / "channel-poster.png"
            poster_path.write_bytes(b"poster")
            plex = MagicMock()
            plex.find_section_collection.return_value = MagicMock(rating_key="collection-1")
            plex.upload_poster.return_value = True
            plex.lock_collection_poster.return_value = False

            with patch.object(bot, "plex_client", plex):
                uploaded = await bot._youtube_set_collection_poster(
                    "9",
                    {
                        "id": "yt_1",
                        "channel": "AcademeG",
                        "item_dir": str(item_dir),
                        "channel_poster_path": str(poster_path),
                    },
                )

        self.assertFalse(uploaded)
        plex.upload_poster.assert_called_once_with("collection-1", poster_path)
        plex.lock_collection_poster.assert_called_once_with("9", "collection-1")

    async def test_youtube_set_collection_poster_skips_missing_poster(self):
        plex = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bot, "plex_client", plex):
                uploaded = await bot._youtube_set_collection_poster(
                    "9",
                    {"id": "yt_1", "channel": "AcademeG", "item_dir": tmp},
                )

        self.assertFalse(uploaded)
        plex.find_section_collection.assert_not_called()
        plex.upload_poster.assert_not_called()

    async def test_youtube_worker_completes_queued_job(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            job = {
                "id": "yt_1",
                "chat_id": 100,
                "status": "queued",
                "created_at": "2026-06-16T10:00:00+03:00",
                "updated_at": "2026-06-16T10:00:00+03:00",
                "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "video_id": "abcdefghijk",
                "title": "Test clip",
                "target_height": 720,
                "quality": "720p",
            }
            store.save_youtube_downloads({"yt_1": job})
            download = MagicMock(return_value={
                "file_path": str(Path(tmp) / "Test clip.mp4"),
                "file_size": 1234,
                "item_dir": str(Path(tmp) / "Test clip"),
                "format_id": "22",
                "quality": "720p",
                "title": "Test clip",
                "channel": "Channel",
                "duration_seconds": 120,
            })

            with (
                patch.object(bot, "state_store", store),
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", Path(tmp)),
                patch.object(bot, "YOUTUBE_AUDIO_LANGUAGE", "rus"),
                patch.object(bot, "YOUTUBE_MIN_FREE_GB", 0),
                patch.object(bot, "_youtube_download_video", download),
                patch.object(bot, "_youtube_delete_job_cards", AsyncMock()) as delete_cards,
                patch.object(bot, "_youtube_start_plex_poll_if_needed", AsyncMock()) as plex_poll,
            ):
                await bot._youtube_worker_once(app)

            jobs = store.load_youtube_downloads()
            self.assertEqual(jobs["yt_1"]["status"], "completed")
            self.assertEqual(jobs["yt_1"]["file_size"], 1234)
            events = [item["event"] for item in store.load_download_history(chat_id=100)]
            self.assertIn("youtube_download_started", events)
            self.assertIn("youtube_download_completed", events)
            self.assertEqual(download.call_args.kwargs["audio_language"], "rus")
            delete_cards.assert_awaited_once()
            app.bot.send_message.assert_awaited_once()
            plex_poll.assert_awaited()

    async def test_youtube_worker_records_download_failure(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "queued",
                    "created_at": "2026-06-16T10:00:00+03:00",
                    "updated_at": "2026-06-16T10:00:00+03:00",
                    "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "video_id": "abcdefghijk",
                    "title": "Test clip",
                    "target_height": 720,
                    "quality": "720p",
                }
            })

            with (
                patch.object(bot, "state_store", store),
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", Path(tmp)),
                patch.object(bot, "YOUTUBE_MIN_FREE_GB", 0),
                patch.object(bot, "_youtube_download_video", MagicMock(side_effect=bot.YouTubeDownloadError("boom"))),
                patch.object(bot, "_youtube_delete_job_cards", AsyncMock()) as delete_cards,
            ):
                await bot._youtube_worker_once(app)

            jobs = store.load_youtube_downloads()
            self.assertEqual(jobs["yt_1"]["status"], "failed")
            self.assertIn("boom", jobs["yt_1"]["error"])
            history = store.load_download_history(chat_id=100)
            self.assertEqual(history[-1]["event"], "youtube_download_failed")
            delete_cards.assert_awaited_once()
            app.bot.send_message.assert_awaited_once()

    async def test_youtube_status_card_stays_registered_after_edit_timeout(self):
        app = MagicMock()
        app.bot.edit_message_text = AsyncMock(side_effect=bot.TimedOut("Timed out"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "downloading",
                    "title": "Test clip",
                    "downloaded_bytes": 10,
                    "total_bytes": 100,
                }
            })
            bot.YOUTUBE_JOB_MESSAGES["yt_1"] = {(100, 77)}
            try:
                with patch.object(bot, "state_store", store):
                    await bot._youtube_update_registered_cards(app)

                self.assertEqual(bot.YOUTUBE_JOB_MESSAGES["yt_1"], {(100, 77)})
            finally:
                bot.YOUTUBE_JOB_MESSAGES.clear()

    async def test_youtube_delete_job_cards_removes_registered_messages(self):
        app = MagicMock()
        app.bot.delete_message = AsyncMock()
        bot.YOUTUBE_JOB_MESSAGES["yt_1"] = {(100, 77)}
        try:
            await bot._youtube_delete_job_cards(app, "yt_1")

            app.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=77)
            self.assertNotIn("yt_1", bot.YOUTUBE_JOB_MESSAGES)
        finally:
            bot.YOUTUBE_JOB_MESSAGES.clear()

    async def test_new_text_query_prefills_personal_defaults(self):
        update = _make_message_update(chat_id=100)
        update.message.text = "Дюна"
        reply = MagicMock()
        reply.message_id = 77
        update.message.reply_text.return_value = reply
        context = _make_context()
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        store.load_user_search_defaults.return_value = {
            "quality": "4K",
            "audio": True,
            "subs": False,
            "preferred_voices": ["LostFilm"],
        }

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", store),
            patch.object(bot, "rutracker_client", MagicMock()),
            patch.object(bot, "jackett_client", None),
        ):
            result = await bot.text_message_entry(update, context)

        self.assertEqual(result, bot.SEARCH_OPTIONS)
        self.assertEqual(context.user_data["srch_settings"]["quality"], "4K")
        self.assertTrue(context.user_data["srch_settings"]["audio"])
        self.assertEqual(context.user_data["srch_voice_hints"], ["LostFilm"])
        self.assertEqual(context.user_data["srch_voice_source"], "default")
        self.assertEqual(context.user_data["srch_setting_sources"]["audio"], "default")
        self.assertEqual(context.user_data["srch_setting_sources"]["subs"], "default")

    async def test_explicit_query_overrides_personal_defaults_and_autostarts(self):
        update = _make_message_update(chat_id=100)
        update.message.text = "Дюна 720р"
        context = _make_context()
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        store.load_user_search_defaults.return_value = {
            "quality": "4K",
            "audio": False,
            "subs": False,
            "preferred_voices": [],
        }

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", store),
            patch.object(bot, "rutracker_client", MagicMock()),
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "_run_search", AsyncMock(return_value=bot.SEARCH_RESULTS)) as run_search,
        ):
            result = await bot.text_message_entry(update, context)

        self.assertEqual(result, bot.SEARCH_RESULTS)
        self.assertEqual(run_search.await_args.args[2], "Дюна 720p")

    async def test_didmean_preserves_series_mode_and_strips_season_from_query(self):
        update = _make_callback_update(callback_data="srch:didmean:0")
        context = _make_context(user_data={
            "srch_intent": bot.SEARCH_INTENT_SERIES_MASTER,
            "srch_didmean_suggestions": ["Клиника сезон 3"],
            "srch_settings": {"quality": "1080p", "audio": False, "subs": False},
        })

        with patch.object(bot, "_run_search", AsyncMock(return_value=bot.SEARCH_RESULTS)) as run_search:
            result = await bot.search_didmean(update, context)

        self.assertEqual(result, bot.SEARCH_RESULTS)
        self.assertEqual(context.user_data["srch_intent"], bot.SEARCH_INTENT_SERIES_MASTER)
        self.assertEqual(run_search.await_args.args[2], "Клиника 1080p")


class SearchSettingsCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_command_renders_personal_defaults(self):
        update = _make_message_update(chat_id=100)
        reply = MagicMock()
        reply.message_id = 77
        update.message.reply_text.return_value = reply
        context = _make_context()
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        store.load_user_search_defaults.return_value = {
            "quality": "4K",
            "audio": True,
            "subs": True,
            "preferred_voices": ["LostFilm"],
        }

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", store),
        ):
            await bot.settings_command(update, context)

        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Предпочтения поиска", text)
        self.assertIn("Качество: предпочитаю 4K", text)
        self.assertIn("Переводы: предпочитаю LostFilm", text)

    async def test_settings_callback_toggles_quality_and_saves(self):
        update = _make_callback_update(chat_id=100, callback_data="settings:quality")
        context = _make_context()
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        store.load_user_search_defaults.return_value = {
            "quality": "1080p",
            "audio": False,
            "subs": False,
            "preferred_voices": [],
        }

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", store),
        ):
            await bot.settings_callback(update, context)

        saved = store.save_user_search_defaults.call_args.args[1]
        self.assertEqual(saved["quality"], "720p")
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Качество: предпочитаю 720p", text)


class SearchVoicePreferenceTests(unittest.TestCase):
    def test_explicit_voice_boosts_when_found_without_hiding_alternatives(self):
        context = _make_context(user_data={
            "srch_voice_hints": ["LostFilm"],
            "srch_voice_source": "explicit",
        })
        results = [
            {"title": "Клиника / Scrubs / Сезон: 1 / LostFilm", "size": "1 GB"},
            {"title": "Клиника / Scrubs / Сезон: 1 / NewStudio", "size": "1 GB"},
        ]

        filtered, banner = bot._apply_voice_preferences(results, context)

        self.assertEqual(len(filtered), 2)
        self.assertIn("LostFilm", filtered[0]["title"])
        self.assertIn("Сначала варианты с LostFilm", banner)

    def test_default_voice_only_boosts_and_keeps_alternatives(self):
        context = _make_context(user_data={
            "srch_voice_hints": ["LostFilm"],
            "srch_voice_source": "default",
        })
        results = [
            {"title": "Клиника / Scrubs / Сезон: 1 / NewStudio", "size": "1 GB"},
            {"title": "Клиника / Scrubs / Сезон: 1 / LostFilm", "size": "1 GB"},
        ]

        filtered, banner = bot._apply_voice_preferences(results, context)

        self.assertEqual(len(filtered), 2)
        self.assertIn("LostFilm", filtered[0]["title"])
        self.assertEqual(banner, "")

    def test_explicit_voice_without_matches_keeps_alternatives_with_banner(self):
        context = _make_context(user_data={
            "srch_voice_hints": ["LostFilm"],
            "srch_voice_source": "explicit",
        })
        results = [{"title": "Клиника / Scrubs / Сезон: 1 / NewStudio", "size": "1 GB"}]

        filtered, banner = bot._apply_voice_preferences(results, context)

        self.assertEqual(filtered, results)
        self.assertIn("не нашёл", banner)

    def test_default_voice_without_matches_keeps_alternatives_with_banner(self):
        context = _make_context(user_data={
            "srch_voice_hints": ["LostFilm"],
            "srch_voice_source": "default",
        })
        results = [{"title": "Клиника / Scrubs / Сезон: 1 / NewStudio", "size": "1 GB"}]

        filtered, banner = bot._apply_voice_preferences(results, context)

        self.assertEqual(filtered, results)
        self.assertIn("Предпочитаемый перевод LostFilm не нашёл", banner)


class SearchPlexHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_cluster_hint_marks_existing_series_season(self):
        show = bot.PlexShow(
            title="Острые козырьки",
            year=2013,
            rating_key="show1",
            seasons={
                6: bot.PlexSeason(
                    rating_key="season6",
                    season_number=6,
                    episode_count=6,
                    resolution="1080",
                )
            },
        )
        old_shows = bot._plex_shows_library
        try:
            bot._plex_shows_library = {bot._plex_cache_key("Острые козырьки", 2013): show}
            with patch.object(bot, "PLEX_ENABLED", True):
                hint = await bot._cluster_plex_hint({
                    "kind": "series",
                    "title": "Острые козырьки",
                    "seasons": [6],
                }, "1080")
        finally:
            bot._plex_shows_library = old_shows

        self.assertEqual(hint["action"], "warn_same")
        self.assertEqual(hint["season"], 6)

    async def test_cluster_hint_does_not_mark_movie_from_series_match(self):
        show = bot.PlexShow(title="Острые козырьки", year=2013, rating_key="show1")
        old_shows = bot._plex_shows_library
        old_movies = bot._plex_library
        try:
            bot._plex_shows_library = {bot._plex_cache_key("Острые козырьки", 2013): show}
            bot._plex_library = {}
            with patch.object(bot, "PLEX_ENABLED", True):
                hint = await bot._cluster_plex_hint({
                    "kind": "movie",
                    "title": "Острые козырьки Бессмертный человек",
                    "year": 2026,
                }, "1080")
        finally:
            bot._plex_shows_library = old_shows
            bot._plex_library = old_movies

        self.assertIsNone(hint)


class SearchSeasonBackToPickerHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Tests for search_season_back_to_picker — '⬅️ К выбору сезона' on 0-results screen."""

    async def test_returns_to_picker_with_saved_base_title_and_total(self):
        from bot import search_season_back_to_picker, SEARCH_SEASON_SELECT
        update = _make_callback_update(callback_data="srch:season_back_to_picker")
        context = _make_context(user_data={
            "srch_base_title": "Клиника",
            "srch_total_seasons": 10,
            "srch_picked_quality": "1080",
        })
        result = await search_season_back_to_picker(update, context)
        self.assertEqual(result, SEARCH_SEASON_SELECT)
        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        text = edit.call_args.args[0]
        self.assertIn("«Клиника»", text)
        self.assertIn("(10 сез.)", text)
        # Quality hint included because srch_picked_quality is set
        self.assertIn("1080p", text)

    async def test_handles_lost_base_title_gracefully(self):
        from bot import search_season_back_to_picker
        from telegram.ext import ConversationHandler
        update = _make_callback_update(callback_data="srch:season_back_to_picker")
        context = _make_context(user_data={})
        result = await search_season_back_to_picker(update, context)
        self.assertEqual(result, ConversationHandler.END)


# ---------------------------------------------------------------------------
# _movie_trackers_panel tests
# ---------------------------------------------------------------------------


class MovieTrackersPanelTests(unittest.TestCase):
    """Tests for _movie_trackers_panel() in bot.py."""

    def _make_store(self, tmp_dir: str) -> JsonStateStore:
        d = Path(tmp_dir)
        return JsonStateStore(
            approved_chat_ids_file=d / "approved.json",
            tracker_processed_file=d / "tracker.json",
            task_owners_file=d / "owners.json",
            notified_tasks_file=d / "notified.json",
            auto_delete_tasks_file=d / "auto_delete.json",
            movie_discovery_settings_file=d / "md_settings.json",
        )

    def test_fresh_jackett_trackers_saved_to_known(self) -> None:
        """When Jackett returns fresh trackers, jackett_trackers_known must be persisted."""
        from bot import _movie_trackers_panel

        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            fake_jackett = MagicMock()
            fake_jackett.get_indexers.return_value = [
                {"id": "kinozal", "name": "Kinozal"},
                {"id": "rutracker", "name": "RuTracker"},
            ]
            with (
                patch.object(bot, "jackett_client", fake_jackett),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_load_movie_discovery_settings",
                             lambda: store.load_movie_discovery_settings()),
                patch.object(bot, "_save_movie_discovery_settings",
                             lambda s: store.save_movie_discovery_settings(s)),
            ):
                asyncio.run(_movie_trackers_panel())

            saved = store.load_movie_discovery_settings()
            self.assertEqual(sorted(saved.get("jackett_trackers_known", [])),
                             ["kinozal", "rutracker"])

    def test_known_not_overwritten_when_jackett_unavailable(self) -> None:
        """When Jackett fails, existing jackett_trackers_known must not be erased."""
        from bot import _movie_trackers_panel

        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            store.save_movie_discovery_settings({"jackett_trackers_known": ["kinozal", "rutracker"]})

            fake_jackett = MagicMock()
            fake_jackett.get_indexers.side_effect = Exception("timeout")
            with (
                patch.object(bot, "jackett_client", fake_jackett),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_load_movie_discovery_settings",
                             lambda: store.load_movie_discovery_settings()),
                patch.object(bot, "_save_movie_discovery_settings",
                             lambda s: store.save_movie_discovery_settings(s)),
            ):
                asyncio.run(_movie_trackers_panel())

            saved = store.load_movie_discovery_settings()
            self.assertEqual(sorted(saved.get("jackett_trackers_known", [])),
                             ["kinozal", "rutracker"])


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

    def _urls(self, keyboard):
        return {btn.text: btn.url for row in keyboard.inline_keyboard for btn in row if btn.url}

    def test_movie_notification_keyboard_has_close_button(self):
        """Push about new films in /new must offer ✖️ Закрыть so the user can
        dismiss the notification once seen."""
        from bot import _movie_notification_keyboard
        keyboard = _movie_notification_keyboard()
        labels = self._labels(keyboard)
        self.assertIn("✖️ Закрыть", labels)
        # Existing buttons preserved
        self.assertIn("🎬 Открыть /new", labels)
        self.assertIn("🔕 Отписаться", labels)

    def test_movie_notification_keyboard_has_download_actions(self):
        from bot import _movie_notification_keyboard
        keyboard = _movie_notification_keyboard("abc123def0", 3, [0, 2])
        buttons = {btn.text: btn.callback_data for row in keyboard.inline_keyboard for btn in row}

        self.assertEqual(buttons["⬇️ 1"], "new:dl:abc123def0:0")
        self.assertEqual(buttons["⬇️ 3"], "new:dl:abc123def0:2")
        self.assertEqual(buttons["⬇️ Скачать все 2"], "new:bulk:abc123def0")
        self.assertEqual(buttons["🎬 Открыть /new"], "new:open:abc123def0")
        self.assertNotIn("⬇️ 2", buttons)

    def test_movie_notification_text_links_kinopoisk_title(self):
        text = bot._format_movie_notification_text([{
            "title": "Test Movie",
            "alt_title": "Original Test",
            "year": 2026,
            "kp_url": "https://www.kinopoisk.ru/film/123/",
            "rating": 8.1,
        }])

        self.assertIn(
            '<a href="https://www.kinopoisk.ru/film/123/">Test Movie / Original Test</a>',
            text,
        )

    def test_movie_notification_pick_release_uses_soft_defaults(self):
        card = {"releases": [
            {
                "title": "Test Movie 1080p",
                "tracker": "rutracker",
                "url": "https://example.test/1080",
                "size": "2 GB",
                "seeders": 50,
            },
            {
                "title": "Test Movie 2160p Original Sub",
                "tracker": "rutracker",
                "url": "https://example.test/2160",
                "size": "8 GB",
                "seeders": 1,
            },
        ]}

        with (
            patch.object(bot, "_score_result", return_value=0),
            patch.object(bot, "_search_defaults_for_chat", return_value={
                "quality": "4K",
                "audio": True,
                "subs": True,
                "preferred_voices": [],
            }),
        ):
            selected, notes = bot._movie_notification_pick_release(card, 100)

        self.assertEqual(selected["title"], "Test Movie 2160p Original Sub")
        self.assertEqual(notes, [])

    def test_movie_notification_snapshot_tiebreak_can_use_gpt(self):
        card = {"title": "Test Movie", "year": 2026, "releases": [
            {
                "title": "Test Movie 1080p A",
                "tracker": "rutracker",
                "url": "https://example.test/a",
                "size": "2 GB",
                "seeders": 50,
            },
            {
                "title": "Test Movie 1080p Original B",
                "tracker": "rutracker",
                "url": "https://example.test/b",
                "size": "3 GB",
                "seeders": 45,
            },
        ]}

        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "_score_result", return_value=0),
            patch.object(bot, "_search_defaults_for_chat", return_value={
                "quality": "any",
                "audio": False,
                "subs": False,
                "preferred_voices": [],
            }),
            patch.object(bot, "gpt_choose_movie_notification_release",
                         return_value=(1, "лучше по Original", None)) as choose,
            patch.object(bot, "_gpt_record_usage"),
        ):
            selected, notes = asyncio.run(
                bot._movie_notification_pick_release_for_snapshot(card, 100)
            )

        self.assertEqual(selected["title"], "Test Movie 1080p Original B")
        self.assertIn("GPT выбрал: лучше по Original", notes)
        choose.assert_called_once()

    def test_movie_notification_snapshot_skips_gpt_when_score_gap_is_large(self):
        card = {"title": "Test Movie", "releases": [
            {"title": "Test Movie 1080p", "url": "https://example.test/a"},
            {"title": "Test Movie 720p", "url": "https://example.test/b"},
        ]}

        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "_movie_notification_release_score", side_effect=[500, 0]),
            patch.object(bot, "_search_defaults_for_chat", return_value={}),
            patch.object(bot, "gpt_choose_movie_notification_release") as choose,
            patch.object(bot, "_gpt_record_usage"),
        ):
            selected, _notes = asyncio.run(
                bot._movie_notification_pick_release_for_snapshot(card, 100)
            )

        self.assertEqual(selected["title"], "Test Movie 1080p")
        choose.assert_not_called()

    def test_final_download_statuses_show_task_button_not_generic_plex(self):
        with patch.object(bot, "PLEX_ENABLED", True):
            finished_labels = self._labels(_notification_keyboard("tid1", "finished", "bt"))
            seeding_labels = self._labels(_notification_keyboard("tid1", "seeding", "bt"))
            error_labels = self._labels(_notification_keyboard("tid1", "error", "bt"))

        self.assertIn("📋 Показать задачу", finished_labels)
        self.assertIn("📋 Показать задачу", seeding_labels)
        self.assertNotIn("▶️ Открыть Plex", finished_labels)
        self.assertNotIn("▶️ Открыть Plex", seeding_labels)
        self.assertNotIn("▶️ Смотреть в Plex", finished_labels)
        self.assertNotIn("▶️ Смотреть в Plex", seeding_labels)
        self.assertNotIn("📋 Показать задачу", error_labels)


class PlexDeepLinkHelperTests(unittest.TestCase):
    """_plex_deep_link: builds Plex URLs for inline buttons, with optional
    user-hosted redirect base for native-app launching on iOS."""

    def test_no_args_default_returns_plex_web_root(self):
        with patch.object(bot, "PLEX_DEEPLINK_BASE_URL", ""):
            self.assertEqual(bot._plex_deep_link(), "https://app.plex.tv/desktop")

    def test_with_rating_key_and_machine_id_default_returns_plex_web_deeplink(self):
        with patch.object(bot, "PLEX_DEEPLINK_BASE_URL", ""):
            url = bot._plex_deep_link("12345", "abc-machine-id")
        self.assertEqual(
            url,
            "https://app.plex.tv/desktop/#!/server/abc-machine-id/details?key=%2Flibrary%2Fmetadata%2F12345",
        )

    def test_custom_base_url_no_args_returns_base_as_is(self):
        with patch.object(bot, "PLEX_DEEPLINK_BASE_URL", "https://nas.example.com/plex.html"):
            self.assertEqual(bot._plex_deep_link(), "https://nas.example.com/plex.html")

    def test_custom_base_url_with_args_appends_query_params(self):
        """Redirect page reads key+server and does location.href='plex://...'."""
        with patch.object(bot, "PLEX_DEEPLINK_BASE_URL", "https://nas.example.com/plex.html"):
            url = bot._plex_deep_link("12345", "abc-machine-id")
        self.assertIn("https://nas.example.com/plex.html?", url)
        self.assertIn("key=%2Flibrary%2Fmetadata%2F12345", url)
        self.assertIn("server=abc-machine-id", url)

    def test_custom_base_with_existing_query_uses_ampersand(self):
        """If the base URL already has ?something, append using &."""
        with patch.object(bot, "PLEX_DEEPLINK_BASE_URL", "https://nas.example.com/plex.html?theme=dark"):
            url = bot._plex_deep_link("12345", "abc-machine-id")
        self.assertIn("theme=dark&key=", url)
        self.assertNotIn("?key=", url)

    def test_empty_rating_key_with_custom_base_returns_base(self):
        with patch.object(bot, "PLEX_DEEPLINK_BASE_URL", "https://nas.example.com/plex.html"):
            self.assertEqual(
                bot._plex_deep_link(rating_key="", machine_id="m1"),
                "https://nas.example.com/plex.html",
            )


class SubscriptionAdvancedKeyboardTests(unittest.TestCase):
    def _buttons(self, keyboard) -> dict[str, str]:
        return {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }

    def test_notify_step_has_cancel(self):
        buttons = self._buttons(bot._advanced_notify_keyboard(3))
        self.assertEqual(buttons["❌ Отмена"], "srch:cancel")

    def test_download_step_has_cancel(self):
        buttons = self._buttons(bot._advanced_download_keyboard(3))
        self.assertEqual(buttons["❌ Отмена"], "srch:cancel")


# ---------------------------------------------------------------------------
# /seasons command tests
# ---------------------------------------------------------------------------


class SeriesContinueCommandTests(unittest.TestCase):
    def _candidate(self, index: int = 0, *, topic_id: str = "12345", source: str = "history"):
        from series_continue import PlexSeriesIdentity, SeriesCatchUpCandidate

        title = "The Rookie" if index == 0 else f"The Rookie {index}"
        return SeriesCatchUpCandidate(
            identity=PlexSeriesIdentity(
                plex_rating_key=str(1000 + index),
                plex_guid=f"plex://show/{index}",
                title=title,
                original_title=title,
                year=2024,
            ),
            season_number=8,
            present_count=8,
            known_total=18,
            quality="1080p",
            source=source,
            topic_id=topic_id,
            tracker="RuTracker",
            history_chat_ids=(100,),
            history_last_episode_end=8,
        )

    def _missing_candidate(
        self,
        *,
        season: int = 7,
        index: int = 0,
        metadata_confidence: str = "confirmed",
        metadata_sources: tuple[str, ...] = (),
        metadata_source_counts: tuple[tuple[str, int], ...] = (),
        metadata_unavailable_sources: tuple[str, ...] = (),
        quality: str = "1080",
    ):
        from series_continue import PlexSeriesIdentity, SeriesMissingSeasonCandidate

        title = "The Rookie" if index == 0 else f"The Rookie {index}"
        return SeriesMissingSeasonCandidate(
            identity=PlexSeriesIdentity(
                plex_rating_key=str(1000 + index),
                plex_guid=f"plex://show/{index}",
                title=title,
                original_title=title,
                year=2024,
            ),
            season_number=season,
            episode_count=18,
            present_seasons=(1, 2, 3, 4, 5, 6, 8),
            quality=quality,
            history_chat_ids=(100,),
            metadata_confidence=metadata_confidence,
            metadata_sources=metadata_sources,
            metadata_source_counts=metadata_source_counts,
            metadata_unavailable_sources=metadata_unavailable_sources,
        )

    def _callbacks(self, keyboard) -> list[str]:
        return [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]

    def _button_texts(self, keyboard) -> list[str]:
        return [
            button.text
            for row in keyboard.inline_keyboard
            for button in row
        ]

    def _rt_result(self, topic_id: str, title: str):
        from rutracker import RutrackerResult

        return RutrackerResult(
            topic_id=topic_id,
            title=title,
            category="TV",
            size="10 GB",
            seeders=10,
        )

    def test_continue_active_task_matches_possessive_apostrophe_title(self):
        from series_continue import PlexSeriesIdentity, SeriesCatchUpCandidate

        candidate = SeriesCatchUpCandidate(
            identity=PlexSeriesIdentity(
                plex_rating_key="show-cf",
                plex_guid="plex://show/cf",
                title="Clarkson's Farm",
                year=2021,
            ),
            season_number=5,
        )
        task = {
            "status": "downloading",
            "title": "Clarksons.Farm.S05E01.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv",
        }

        self.assertTrue(bot._series_continue_task_matches_candidate(task, candidate))

    def test_series_bulk_downloading_seasons_matches_possessive_apostrophe_title(self):
        ds = MagicMock()
        ds.list_tasks.return_value = [{
            "status": "downloading",
            "title": "Clarksons.Farm.S05E01.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv",
        }]

        with patch.object(bot, "ds_client", ds):
            seasons = asyncio.run(bot._series_bulk_downloading_seasons("Clarkson's Farm"))

        self.assertEqual(seasons, {5})

    def test_continue_known_totals_uses_tmdb_external_id(self):
        from plex import PlexShow, PlexSeason

        season = PlexSeason("season-key-5", 5, episode_count=4, file_paths=[], resolution="1080")
        show = PlexShow(
            "Clarkson's Farm",
            2021,
            "show-key-cf",
            seasons={5: season},
            guid="plex://show/cf",
            external_guids=["tmdb://119550"],
        )
        tmdb = MagicMock()
        tmdb.season_aired_episode_count.return_value = 8
        tvmaze = MagicMock()
        state = MagicMock()
        state.load_series_continue_totals.return_value = {}

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_known_totals_by_show([show], []))

        self.assertEqual(totals, {"show-key-cf": {5: 8}})
        tmdb.season_aired_episode_count.assert_called_once_with(
            season_number=5,
            tmdb_id="119550",
            imdb_id="",
            tvdb_id="",
        )
        tvmaze.season_aired_episode_count.assert_not_called()
        state.save_series_continue_totals.assert_called_once()
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertEqual(saved["tmdb:119550"]["5"], 8)
        self.assertEqual(saved["plex_rating:show-key-cf"]["5"], 8)
        self.assertEqual(saved["tmdb:119550"][bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY], 2)

    def test_continue_known_totals_fetches_plex_external_guids(self):
        from plex import PlexShow, PlexSeason

        season = PlexSeason("season-key-5", 5, episode_count=4, file_paths=[], resolution="1080")
        show = PlexShow(
            "Clarkson's Farm",
            2021,
            "show-key-cf",
            seasons={5: season},
            guid="plex://show/cf",
            external_guids=[],
        )
        detailed = PlexShow(
            "Clarkson's Farm",
            2021,
            "show-key-cf",
            seasons={},
            guid="plex://show/cf",
            external_guids=["tvdb://402960"],
        )
        plex = MagicMock()
        plex.get_show_details.return_value = detailed
        tmdb = MagicMock()
        tmdb.season_aired_episode_count.return_value = 8
        tvmaze = MagicMock()
        tvmaze.season_aired_episode_count.return_value = 8
        state = MagicMock()
        state.load_series_continue_totals.return_value = {}

        with (
            patch.object(bot, "plex_client", plex),
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_known_totals_by_show([show], []))

        self.assertEqual(totals, {"show-key-cf": {5: 8}})
        plex.get_show_details.assert_called_once_with("show-key-cf")
        tmdb.season_aired_episode_count.assert_called_once_with(
            season_number=5,
            tmdb_id="",
            imdb_id="",
            tvdb_id="402960",
        )
        tvmaze.season_aired_episode_count.assert_called_once_with(
            season_number=5,
            imdb_id="",
            tvdb_id="402960",
        )
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertEqual(saved["tvmaze:tvdb:402960"]["5"], 8)

    def test_continue_known_totals_skips_tmdb_total_when_tvmaze_conflicts(self):
        from plex import PlexShow, PlexSeason

        season = PlexSeason("season-key-1", 1, episode_count=5, file_paths=[], resolution="1080")
        show = PlexShow(
            "Lupin",
            2021,
            "show-key-lupin",
            seasons={1: season},
            guid="plex://show/lupin",
            external_guids=["tmdb://96677", "tvdb://367178"],
        )
        tmdb = MagicMock()
        tmdb.season_aired_episode_count.return_value = 10
        tvmaze = MagicMock()
        tvmaze.season_aired_episode_count.return_value = 5
        state = MagicMock()
        state.load_series_continue_totals.return_value = {}

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_known_totals_by_show([show], []))

        self.assertEqual(totals, {})
        tmdb.season_aired_episode_count.assert_called_once_with(
            season_number=1,
            tmdb_id="96677",
            imdb_id="",
            tvdb_id="367178",
        )
        tvmaze.season_aired_episode_count.assert_called_once_with(
            season_number=1,
            imdb_id="",
            tvdb_id="367178",
        )
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertEqual(saved["tmdb:96677"]["1"], 10)
        self.assertEqual(saved["tvmaze:tvdb:367178"]["1"], 5)

    def test_continue_known_totals_uses_local_cache_before_tmdb(self):
        from plex import PlexShow, PlexSeason

        season = PlexSeason("season-key-5", 5, episode_count=4, file_paths=[], resolution="1080")
        show = PlexShow(
            "Clarkson's Farm",
            2021,
            "show-key-cf",
            seasons={5: season},
            guid="plex://show/cf",
            external_guids=["tmdb://119550"],
        )
        tmdb = MagicMock()
        tvmaze = MagicMock()
        state = MagicMock()
        state.load_series_continue_totals.return_value = {
            "tmdb:119550": {
                "5": 8,
                bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY: 2,
            }
        }

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_known_totals_by_show([show], []))

        self.assertEqual(totals, {"show-key-cf": {5: 8}})
        tmdb.season_aired_episode_count.assert_not_called()
        tvmaze.season_aired_episode_count.assert_not_called()
        state.save_series_continue_totals.assert_not_called()

    def test_continue_known_totals_ignores_unversioned_local_cache(self):
        from plex import PlexShow, PlexSeason

        season = PlexSeason("season-key-5", 5, episode_count=4, file_paths=[], resolution="1080")
        show = PlexShow(
            "Clarkson's Farm",
            2021,
            "show-key-cf",
            seasons={5: season},
            guid="plex://show/cf",
            external_guids=["tmdb://119550"],
        )
        tmdb = MagicMock()
        tmdb.season_aired_episode_count.return_value = 4
        tvmaze = MagicMock()
        state = MagicMock()
        state.load_series_continue_totals.return_value = {"tmdb:119550": {"5": 8}}

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_known_totals_by_show([show], []))

        self.assertEqual(totals, {"show-key-cf": {5: 4}})
        tmdb.season_aired_episode_count.assert_called_once()
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertEqual(saved["tmdb:119550"]["5"], 4)
        self.assertEqual(saved["tmdb:119550"][bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY], 2)

    def test_continue_metadata_totals_uses_complete_cache_without_api(self):
        from plex import PlexShow

        show = PlexShow(
            "The Rookie",
            2018,
            "show-key-rookie",
            seasons={},
            guid="plex://show/rookie",
            external_guids=["tmdb://12345"],
        )
        tmdb = MagicMock()
        tvmaze = MagicMock()
        state = MagicMock()
        state.load_series_continue_totals.return_value = {
            "tmdb:12345": {
                "1": 20,
                "2": 18,
                bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY: True,
                bot._SERIES_CONTINUE_TOTALS_FETCHED_AT_KEY: int(bot.time.time()),
                bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY: 2,
            },
        }

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_metadata_totals_by_show([show]))

        self.assertEqual(
            {season: item.episode_count for season, item in totals["show-key-rookie"].items()},
            {1: 20, 2: 18},
        )
        tmdb.season_released_episode_counts.assert_not_called()
        tvmaze.season_released_episode_counts.assert_not_called()
        state.save_series_continue_totals.assert_not_called()

    def test_continue_metadata_totals_ignores_unversioned_complete_cache(self):
        from plex import PlexShow

        show = PlexShow(
            "The Gentlemen",
            2024,
            "show-key-gentlemen",
            seasons={},
            guid="plex://show/gentlemen",
            external_guids=["tmdb://242446"],
        )
        tmdb = MagicMock()
        tmdb.season_released_episode_counts.return_value = {1: 8}
        tvmaze = MagicMock()
        tvmaze.season_released_episode_counts.return_value = {}
        state = MagicMock()
        state.load_series_continue_totals.return_value = {
            "tmdb:242446": {
                "1": 8,
                "2": 8,
                bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY: True,
                bot._SERIES_CONTINUE_TOTALS_FETCHED_AT_KEY: int(bot.time.time()),
            },
        }

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_metadata_totals_by_show([show]))

        self.assertEqual(sorted(totals["show-key-gentlemen"]), [1])
        tmdb.season_released_episode_counts.assert_called_once()
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertEqual(saved["tmdb:242446"]["1"], 8)
        self.assertNotIn("2", saved["tmdb:242446"])
        self.assertEqual(saved["tmdb:242446"][bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY], 2)

    def test_continue_metadata_totals_refreshes_tmdb_when_only_tvmaze_cache_is_complete(self):
        from plex import PlexShow

        show = PlexShow(
            "Tainy sledstviya",
            2000,
            "5984",
            seasons={},
            guid="plex://show/5d9c08f22df347001e3ba83b",
            external_guids=["imdb://tt0442730", "tmdb://76713", "tvdb://113651"],
        )
        tmdb = MagicMock()
        tmdb.season_released_episode_counts.return_value = {
            season: 16
            for season in range(1, 26)
        } | {2: 12}
        tvmaze = MagicMock()
        tvmaze.season_released_episode_counts.return_value = {1: 16, 2: 12, 19: 28, 22: 16, 23: 16, 24: 16, 25: 16}
        state = MagicMock()
        state.load_series_continue_totals.return_value = {
            "tmdb:76713": {"1": 16, "2": 12},
            "tvdb:113651": {"1": 16, "2": 12},
            "imdb:tt0442730": {"1": 16, "2": 12},
            "plex_rating:5984": {"1": 16, "2": 12},
            "tvmaze:tvdb:113651": {
                "1": 16,
                "2": 12,
                "22": 16,
                "23": 16,
                "24": 16,
                "25": 16,
                bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY: True,
            },
        }

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_metadata_totals_by_show([show]))

        self.assertEqual(sorted(totals["5984"]), list(range(1, 26)))
        self.assertEqual(totals["5984"][25].episode_count, 16)
        self.assertEqual(totals["5984"][19].confidence, "conflict")
        self.assertEqual(totals["5984"][19].episode_count, 0)
        tmdb.season_released_episode_counts.assert_called_once_with(
            tmdb_id="76713",
            imdb_id="tt0442730",
            tvdb_id="113651",
        )
        tvmaze.season_released_episode_counts.assert_called_once_with(
            imdb_id="tt0442730",
            tvdb_id="113651",
        )
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertTrue(saved["tmdb:76713"][bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY])
        self.assertEqual(saved["tmdb:76713"]["25"], 16)

    def test_continue_metadata_totals_refreshes_stale_complete_tmdb_cache(self):
        from plex import PlexShow

        show = PlexShow(
            "The Rookie",
            2018,
            "show-key-rookie",
            seasons={},
            guid="plex://show/rookie",
            external_guids=["tmdb://12345"],
        )
        tmdb = MagicMock()
        tmdb.season_released_episode_counts.return_value = {1: 20, 2: 18, 3: 18}
        tvmaze = MagicMock()
        tvmaze.season_released_episode_counts.return_value = {}
        state = MagicMock()
        state.load_series_continue_totals.return_value = {
            "tmdb:12345": {
                "1": 20,
                "2": 18,
                bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY: True,
                bot._SERIES_CONTINUE_TOTALS_FETCHED_AT_KEY: int(
                    bot.time.time() - bot._SERIES_CONTINUE_TOTALS_COMPLETE_TTL_SECONDS - 1
                ),
                bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY: 2,
            },
        }

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_metadata_totals_by_show([show]))

        self.assertEqual(sorted(totals["show-key-rookie"]), [1, 2, 3])
        self.assertEqual(totals["show-key-rookie"][3].episode_count, 18)
        tmdb.season_released_episode_counts.assert_called_once()

    def test_continue_metadata_totals_marks_cached_conflicts(self):
        from plex import PlexShow

        show = PlexShow(
            "Lupin",
            2021,
            "show-key-lupin",
            seasons={},
            guid="plex://show/lupin",
            external_guids=["tmdb://96677", "tvdb://367178"],
        )
        tmdb = MagicMock()
        tvmaze = MagicMock()
        state = MagicMock()
        state.load_series_continue_totals.return_value = {
            "tmdb:96677": {
                "1": 10,
                "2": 8,
                bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY: True,
                bot._SERIES_CONTINUE_TOTALS_FETCHED_AT_KEY: int(bot.time.time()),
                bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY: 2,
            },
            "tvmaze:tvdb:367178": {
                "1": 5,
                "2": 8,
                bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY: True,
                bot._SERIES_CONTINUE_TOTALS_FETCHED_AT_KEY: int(bot.time.time()),
                bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY: 2,
            },
        }

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_metadata_totals_by_show([show]))

        self.assertEqual(sorted(totals["show-key-lupin"]), [1, 2])
        self.assertEqual(totals["show-key-lupin"][1].confidence, "conflict")
        self.assertEqual(totals["show-key-lupin"][1].source_episode_counts, (("tmdb", 10), ("tvmaze", 5)))
        self.assertEqual(totals["show-key-lupin"][2].confidence, "confirmed")
        tmdb.season_released_episode_counts.assert_not_called()
        tvmaze.season_released_episode_counts.assert_not_called()
        state.save_series_continue_totals.assert_not_called()

    def test_continue_metadata_totals_marks_live_conflicts_and_stores_complete_snapshots(self):
        from plex import PlexShow

        show = PlexShow(
            "Lupin",
            2021,
            "show-key-lupin",
            seasons={},
            guid="plex://show/lupin",
            external_guids=["tmdb://96677", "tvdb://367178"],
        )
        tmdb = MagicMock()
        tmdb.season_released_episode_counts.return_value = {1: 10, 2: 8}
        tvmaze = MagicMock()
        tvmaze.season_released_episode_counts.return_value = {1: 5, 2: 8}
        state = MagicMock()
        state.load_series_continue_totals.return_value = {}

        with (
            patch.object(bot, "tmdb_client", tmdb),
            patch.object(bot, "tvmaze_client", tvmaze),
            patch.object(bot, "state_store", state),
        ):
            totals = asyncio.run(bot._series_continue_metadata_totals_by_show([show]))

        self.assertEqual(sorted(totals["show-key-lupin"]), [1, 2])
        self.assertEqual(totals["show-key-lupin"][1].confidence, "conflict")
        self.assertEqual(totals["show-key-lupin"][1].source_episode_counts, (("tmdb", 10), ("tvmaze", 5)))
        self.assertEqual(totals["show-key-lupin"][2].episode_count, 8)
        tmdb.season_released_episode_counts.assert_called_once_with(
            tmdb_id="96677",
            imdb_id="",
            tvdb_id="367178",
        )
        tvmaze.season_released_episode_counts.assert_called_once_with(
            imdb_id="",
            tvdb_id="367178",
        )
        saved = state.save_series_continue_totals.call_args.args[0]
        self.assertTrue(saved["tmdb:96677"][bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY])
        self.assertEqual(saved["tmdb:96677"]["1"], 10)
        self.assertGreater(saved["tmdb:96677"][bot._SERIES_CONTINUE_TOTALS_FETCHED_AT_KEY], 0)
        self.assertEqual(saved["tmdb:96677"][bot._SERIES_CONTINUE_TOTALS_CACHE_VERSION_KEY], 2)
        self.assertTrue(saved["tvmaze:tvdb:367178"][bot._SERIES_CONTINUE_TOTALS_COMPLETE_KEY])
        self.assertEqual(saved["tvmaze:tvdb:367178"]["1"], 5)

    @contextmanager
    def _allowed_context(self):
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
        ):
            yield

    def test_continue_progress_text_explains_sources_and_confidence(self):
        text = bot._series_continue_progress_text()
        self.assertIn("Проверяю, что докачать", text)
        self.assertIn("Plex", text)
        self.assertIn("историей загрузок", text)
        self.assertIn("каталогом сезонов", text)
        self.assertIn("можно докачать", text)

    def test_continue_command_renders_mine_list(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        progress = MagicMock()
        progress.edit_text = AsyncMock()
        update.message.reply_text.return_value = progress
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "mine", "page": 0}
        build_state = AsyncMock(return_value=state)

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "_series_continue_build_state", build_state),
            ):
                await bot.series_continue_command(update, context)

        asyncio.run(run())

        build_state.assert_awaited_once_with(context, 100, include_missing=True)
        text = progress.edit_text.await_args.args[0]
        keyboard = progress.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("The Rookie", text)
        self.assertIn("S08 · Plex 8/18", text)
        self.assertIn("прошлая тема есть", text)
        self.assertIn("cont:open:mine:0", self._callbacks(keyboard))
        self.assertIn("cont:list:missing:0", self._callbacks(keyboard))
        self.assertIn("task:close:", self._callbacks(keyboard))

    def test_continue_command_keeps_incomplete_list_first_when_missing_exists(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        progress = MagicMock()
        progress.edit_text = AsyncMock()
        update.message.reply_text.return_value = progress
        missing = self._missing_candidate(season=7)
        state = {
            "mine": [],
            "all": [],
            "missing": [missing],
            "hidden_missing": [],
            "scope": "mine",
            "page": 0,
        }
        build_state = AsyncMock(return_value=state)

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "_series_continue_build_state", build_state),
            ):
                await bot.series_continue_command(update, context)

        asyncio.run(run())

        build_state.assert_awaited_once_with(context, 100, include_missing=True)
        text = progress.edit_text.await_args.args[0]
        keyboard = progress.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("Пока не нашёл серии или сезоны", text)
        self.assertNotIn("Нет в Plex: S07", text)
        self.assertIn("cont:list:missing:0", self._callbacks(keyboard))

    def test_continue_command_retries_final_render_network_error(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        progress = MagicMock()
        progress.edit_text = AsyncMock(side_effect=[bot.NetworkError("temporary"), None])
        update.message.reply_text.return_value = progress
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "mine", "page": 0}
        build_state = AsyncMock(return_value=state)
        sleep = AsyncMock()

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "_series_continue_build_state", build_state),
                patch.object(bot.asyncio, "sleep", sleep),
            ):
                await bot.series_continue_command(update, context)

        asyncio.run(run())

        self.assertEqual(progress.edit_text.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

    def test_continue_command_does_not_crash_when_final_render_keeps_failing(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        progress = MagicMock()
        progress.edit_text = AsyncMock(side_effect=bot.NetworkError("telegram down"))
        update.message.reply_text.return_value = progress
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "mine", "page": 0}
        build_state = AsyncMock(return_value=state)
        sleep = AsyncMock()

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "_series_continue_build_state", build_state),
                patch.object(bot.asyncio, "sleep", sleep),
            ):
                await bot.series_continue_command(update, context)

        asyncio.run(run())

        self.assertEqual(progress.edit_text.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    def test_continue_list_marks_own_exact_topic_subscription(self):
        candidate = self._candidate()
        sub = {
            "chat_id": 100,
            "title": "The Rookie S8E1-8 of 18 WEB-DL 1080p",
            "notify_policy": bot.NOTIFY_FINAL_ONLY,
            "download_policy": bot.DOWNLOAD_ONLY_WHEN_COMPLETE,
        }
        state = {
            "mine": [candidate],
            "all": [candidate],
            "topic_subscriptions": {"12345": sub},
            "scope": "mine",
            "page": 0,
        }

        text = bot._series_continue_list_text(state, "mine", 0)
        detail = bot._series_continue_detail_text(candidate, state)

        self.assertIn("Подписка: " + bot.policies_summary_ru(sub), text)
        self.assertIn("Подписка: " + bot.policies_summary_ru(sub), detail)

    def test_continue_subscription_map_ignores_other_users_and_jackett(self):
        store = MagicMock()
        store.load_topic_subscriptions.return_value = {
            "12345": {"chat_id": 200, "title": "Other user"},
            "67890": {"chat_id": 100, "title": "Mine"},
            "jackett:1": {"chat_id": 100, "type": "jackett", "query": "The Rookie"},
        }

        with patch.object(bot, "state_store", store):
            subs = bot._series_continue_subscription_map_for_chat(100)

        self.assertEqual(subs, {"67890": {"chat_id": 100, "title": "Mine"}})

    def test_continue_empty_mine_can_switch_to_all(self):
        state = {"mine": [], "all": [self._candidate()], "scope": "mine", "page": 0}

        text = bot._series_continue_list_text(state, "mine", 0)
        keyboard = bot._series_continue_list_keyboard(state, "mine", 0)

        self.assertIn("общей медиатеке", text)
        self.assertIn("cont:list:all:0", self._callbacks(keyboard))
        self.assertIn("task:close:", self._callbacks(keyboard))

    def test_continue_empty_all_explains_confidence_boundary(self):
        state = {"mine": [], "all": [], "scope": "mine", "page": 0}

        text = bot._series_continue_list_text(state, "mine", 0)
        keyboard = bot._series_continue_list_keyboard(state, "mine", 0)

        self.assertIn("Пока не нашёл серии или сезоны", text)
        self.assertIn("Что проверяю", text)
        self.assertIn("Почему может быть пусто", text)
        self.assertIn("только уверенные варианты", text)
        self.assertIn("cont:refresh:mine", self._callbacks(keyboard))
        self.assertIn("task:close:", self._callbacks(keyboard))

    def test_continue_list_paginates_by_ten(self):
        candidates = [self._candidate(i) for i in range(11)]
        state = {"mine": [], "all": candidates, "scope": "all", "page": 0}

        first_keyboard = bot._series_continue_list_keyboard(state, "all", 0)
        second_keyboard = bot._series_continue_list_keyboard(state, "all", 1)

        first_callbacks = self._callbacks(first_keyboard)
        second_callbacks = self._callbacks(second_keyboard)
        self.assertEqual(len([cb for cb in first_callbacks if cb.startswith("cont:open:all:")]), 10)
        self.assertIn("cont:list:all:1", first_callbacks)
        self.assertIn("cont:open:all:10", second_callbacks)
        self.assertIn("cont:list:all:0", second_callbacks)

    def test_continue_list_shows_hidden_count_and_toggle(self):
        candidate = self._candidate()
        state = {"raw_mine": [], "raw_all": [candidate]}
        bot._series_continue_refresh_hidden_views(state, {bot._series_continue_candidate_key(candidate)})

        text = bot._series_continue_list_text(state, "all", 0)
        keyboard = bot._series_continue_list_keyboard(state, "all", 0)

        self.assertIn("Скрыто: 1", text)
        self.assertIn("cont:list:hidden_all:0", self._callbacks(keyboard))

    def test_continue_missing_list_shows_hidden_toggle_and_regular_list_button(self):
        visible = self._missing_candidate(season=7)
        hidden = self._missing_candidate(season=9)
        state = {"raw_mine": [], "raw_all": [], "raw_missing": [visible, hidden]}
        bot._series_continue_refresh_hidden_views(state, {bot._series_continue_candidate_key(hidden)})

        text = bot._series_continue_list_text(state, "missing", 0)
        keyboard = bot._series_continue_list_keyboard(state, "missing", 0)
        hidden_keyboard = bot._series_continue_list_keyboard(state, "hidden_missing", 0)

        self.assertIn("Нет в Plex: S07", text)
        self.assertNotIn("Режим", text)
        self.assertIn("cont:list:hidden_missing:0", self._callbacks(keyboard))
        self.assertIn("🙈 Показать скрытые (1)", self._button_texts(keyboard))
        self.assertIn("👁️ К обычному списку", self._button_texts(hidden_keyboard))
        self.assertIn("cont:list:mine:0", self._callbacks(keyboard))

    def test_continue_missing_detail_offers_bulk_plan_and_single_seasons(self):
        state = {
            "raw_mine": [],
            "raw_all": [],
            "raw_missing": [
                self._missing_candidate(season=7),
                self._missing_candidate(season=9),
            ],
        }
        bot._series_continue_refresh_hidden_views(state, set())
        group = bot._series_continue_missing_groups(state, "missing")[0]

        text = bot._series_continue_missing_detail_text(group)
        keyboard = bot._series_continue_missing_detail_keyboard("missing", 0, 0, group)
        callbacks = self._callbacks(keyboard)

        self.assertIn("Нет в Plex: S07, S09", text)
        self.assertIn("cont:missing_bulk:missing:0:all", callbacks)
        self.assertIn("cont:missing_bulk:missing:0:7", callbacks)
        self.assertIn("cont:missing_bulk:missing:0:9", callbacks)
        self.assertNotIn("🔔 Следить за сериалом", self._button_texts(keyboard))

    def test_continue_missing_detail_shows_conflicting_episode_counts(self):
        candidate = self._missing_candidate(
            season=7,
            metadata_confidence="conflict",
            metadata_sources=("tmdb", "tvmaze"),
            metadata_source_counts=(("tmdb", 10), ("tvmaze", 8)),
        )
        state = {"raw_mine": [], "raw_all": [], "raw_missing": [candidate]}
        bot._series_continue_refresh_hidden_views(state, set())
        group = bot._series_continue_missing_groups(state, "missing")[0]

        list_text = bot._series_continue_missing_list_text(state, "missing", 0)
        detail_text = bot._series_continue_missing_detail_text(group)

        self.assertIn("S07", list_text)
        self.assertIn("Каталог: спорное число серий S07", list_text)
        self.assertIn("S07: TMDB 10, TVMaze 8", detail_text)

    def test_continue_missing_detail_names_single_source_and_unavailable_catalog(self):
        candidate = self._missing_candidate(
            season=7,
            metadata_confidence="single_source",
            metadata_sources=("tvmaze",),
            metadata_source_counts=(("tvmaze", 8),),
            metadata_unavailable_sources=("tmdb",),
        )
        state = {"raw_mine": [], "raw_all": [], "raw_missing": [candidate]}
        bot._series_continue_refresh_hidden_views(state, set())
        group = bot._series_continue_missing_groups(state, "missing")[0]

        list_text = bot._series_continue_missing_list_text(state, "missing", 0)
        detail_text = bot._series_continue_missing_detail_text(group)

        self.assertIn("Каталог: TMDB недоступен, подтверждено TVMaze S07", list_text)
        self.assertIn("TMDB недоступен, подтверждено TVMaze: S07", detail_text)

    def test_continue_select_metadata_marks_unavailable_single_source(self):
        selected = bot._series_continue_select_metadata_totals(
            {},
            {1: 8},
            show_title="Wednesday",
            unavailable_sources=("tmdb",),
        )

        self.assertEqual(selected[1].confidence, "single_source")
        self.assertEqual(selected[1].sources, ("tvmaze",))
        self.assertEqual(selected[1].unavailable_sources, ("tmdb",))

    def test_continue_missing_bulk_all_uses_only_missing_seasons(self):
        state = {
            "raw_mine": [],
            "raw_all": [],
            "raw_missing": [
                self._missing_candidate(season=7),
                self._missing_candidate(season=9),
            ],
        }
        bot._series_continue_refresh_hidden_views(state, set())
        update = _make_callback_update(chat_id=100, callback_data="cont:missing_bulk:missing:0:all")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        build_plan = AsyncMock(return_value=bot.SEARCH_RESULTS)

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "_series_bulk_build_plan_from_context", build_plan),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        build_plan.assert_awaited_once()
        self.assertEqual(context.user_data["srch_series_bulk_target_seasons"], [7, 9])
        self.assertEqual(context.user_data["srch_series_bulk_origin"], "continue_missing")

    def test_continue_missing_bulk_uses_default_quality_when_show_quality_unknown(self):
        state = {
            "raw_mine": [],
            "raw_all": [],
            "raw_missing": [
                self._missing_candidate(season=7, quality=""),
            ],
        }
        bot._series_continue_refresh_hidden_views(state, set())
        update = _make_callback_update(chat_id=100, callback_data="cont:missing_bulk:missing:0:7")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        build_plan = AsyncMock(return_value=bot.SEARCH_RESULTS)

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "_series_bulk_build_plan_from_context", build_plan),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        build_plan.assert_awaited_once()
        self.assertEqual(context.user_data["srch_series_bulk_profile_draft"].quality, "1080p")
        self.assertEqual(context.user_data["srch_series_bulk_base_quality"], "1080p")
        self.assertEqual(context.user_data["srch_results"][0]["quality"], "1080p")
        self.assertIn("1080", context.user_data["srch_results"][0]["title"])

    def test_continue_missing_bulk_respects_user_any_quality_default(self):
        state = {
            "raw_mine": [],
            "raw_all": [],
            "raw_missing": [
                self._missing_candidate(season=7, quality=""),
            ],
        }
        bot._series_continue_refresh_hidden_views(state, set())
        update = _make_callback_update(chat_id=100, callback_data="cont:missing_bulk:missing:0:7")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        build_plan = AsyncMock(return_value=bot.SEARCH_RESULTS)
        store = MagicMock(
            load_approved_chat_ids=MagicMock(return_value=set()),
            load_user_search_defaults=MagicMock(return_value={"quality": "any"}),
        )

        async def run():
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "_series_bulk_build_plan_from_context", build_plan),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        build_plan.assert_awaited_once()
        self.assertEqual(context.user_data["srch_series_bulk_profile_draft"].quality, "any")
        self.assertEqual(context.user_data["srch_series_bulk_base_quality"], "1080p")
        self.assertEqual(context.user_data["srch_results"][0]["quality"], "")
        self.assertNotIn("1080", context.user_data["srch_results"][0]["title"])

    def test_continue_missing_bulk_single_uses_only_selected_season(self):
        state = {
            "raw_mine": [],
            "raw_all": [],
            "raw_missing": [
                self._missing_candidate(season=7),
                self._missing_candidate(season=9),
            ],
        }
        bot._series_continue_refresh_hidden_views(state, set())
        update = _make_callback_update(chat_id=100, callback_data="cont:missing_bulk:missing:0:7")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        build_plan = AsyncMock(return_value=bot.SEARCH_RESULTS)

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "_series_bulk_build_plan_from_context", build_plan),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        build_plan.assert_awaited_once()
        self.assertEqual(context.user_data["srch_series_bulk_target_seasons"], [7])

    def test_continue_hidden_missing_key_does_not_hide_incomplete_candidate(self):
        from series_continue import PlexSeriesIdentity, SeriesCatchUpCandidate, SeriesMissingSeasonCandidate

        identity = PlexSeriesIdentity(
            plex_rating_key="show-1",
            plex_guid="plex://show/1",
            title="The Rookie",
            original_title="The Rookie",
            year=2024,
        )
        incomplete = SeriesCatchUpCandidate(identity=identity, season_number=7, present_count=4, known_total=18)
        missing = SeriesMissingSeasonCandidate(identity=identity, season_number=7, present_seasons=(1, 2, 3))
        state = {"raw_mine": [], "raw_all": [incomplete], "raw_missing": [missing]}

        bot._series_continue_refresh_hidden_views(state, {bot._series_continue_candidate_key(missing)})

        self.assertEqual(state["all"], [incomplete])
        self.assertEqual(state["hidden_missing"], [missing])
        self.assertEqual(bot._series_continue_candidate_key(incomplete), "incomplete:show-1:S07")
        self.assertEqual(bot._series_continue_candidate_key(missing), "missing:show-1:S07")

    def test_continue_callback_hides_candidate_for_current_chat(self):
        candidate = self._candidate()
        state = {"raw_mine": [], "raw_all": [candidate], "scope": "all", "page": 0}
        bot._series_continue_refresh_hidden_views(state, set())
        update = _make_callback_update(chat_id=100, callback_data="cont:hide:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        store.load_series_continue_hidden.return_value = {}

        async def run():
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        store.save_series_continue_hidden.assert_called_once_with({
            "100": [bot._series_continue_candidate_key(candidate)]
        })
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Скрыто: 1", text)
        self.assertIn("cont:list:hidden_all:0", self._callbacks(update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]))

    def test_continue_callback_unhides_candidate_for_current_chat(self):
        candidate = self._candidate()
        key = bot._series_continue_candidate_key(candidate)
        state = {"raw_mine": [], "raw_all": [candidate], "scope": "hidden_all", "page": 0}
        bot._series_continue_refresh_hidden_views(state, {key})
        update = _make_callback_update(chat_id=100, callback_data="cont:unhide:hidden_all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        store = MagicMock()
        store.load_approved_chat_ids.return_value = set()
        store.load_series_continue_hidden.return_value = {"100": [key]}

        async def run():
            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        store.save_series_continue_hidden.assert_called_once_with({})
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("В скрытых сезонах пока пусто", text)

    def test_continue_callback_opens_candidate_detail(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:open:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})

        async def run():
            with self._allowed_context():
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        update.callback_query.answer.assert_awaited_once()
        text = update.callback_query.edit_message_text.await_args.args[0]
        keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = self._callbacks(keyboard)
        self.assertIn("The Rookie", text)
        self.assertIn("Сезон: 8", text)
        self.assertIn("cont:hide:all:0", callbacks)
        self.assertIn("cont:list:all:0", callbacks)
        self.assertIn("task:close:", callbacks)

    def test_continue_callback_opens_plex_only_candidate_search(self):
        candidate = self._candidate(topic_id="", source="plex")
        state = {"mine": [], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:open:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})

        async def run():
            with self._allowed_context():
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = self._callbacks(keyboard)
        self.assertIn("cont:search_alt:all:0", callbacks)
        self.assertIn("cont:hide:all:0", callbacks)
        self.assertNotIn("cont:update_topic:all:0", callbacks)

    def test_continue_same_topic_downloads_and_subscribes_when_updated(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:update_topic:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.get_topic_title.return_value = "The Rookie S8E1-12 of 18 WEB-DL 1080p"
        attempt_download = AsyncMock(return_value=("dbid_1", "torrent-файл"))

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
                patch.object(bot, "_series_continue_active_task", AsyncMock(return_value=None)),
                patch.object(bot, "_attempt_pending_download", attempt_download),
                patch.object(bot, "_save_subscription_for_result", MagicMock(return_value=("12345", {}))) as save_sub,
                patch.object(bot, "_remember_task_owner") as remember_owner,
                patch.object(bot, "_remember_task_meta") as remember_meta,
                patch.object(bot, "_record_download_added_history") as record_history,
                patch.object(bot, "_register_task_card_from_query"),
                patch.object(bot, "_start_task_card_refresh"),
            ):
                await bot.series_continue_callback(update, context)
                return save_sub, remember_owner, remember_meta, record_history

        save_sub, remember_owner, remember_meta, record_history = asyncio.run(run())

        entry = attempt_download.await_args.args[0]
        self.assertEqual(entry["topic_id"], "12345")
        self.assertTrue(entry["subscribe"])
        save_sub.assert_called_once()
        remember_owner.assert_called_once_with("dbid_1", 100)
        remember_meta.assert_called_once()
        record_history.assert_called_once()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Задача добавлена", text)
        self.assertIn("Буду следить", text)

    def test_continue_same_topic_complete_update_does_not_subscribe(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:update_topic:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.get_topic_title.return_value = "The Rookie S8E1-18 of 18 WEB-DL 1080p"
        attempt_download = AsyncMock(return_value=("dbid_1", "torrent-файл"))

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
                patch.object(bot, "_series_continue_active_task", AsyncMock(return_value=None)),
                patch.object(bot, "_attempt_pending_download", attempt_download),
                patch.object(bot, "_save_subscription_for_result") as save_sub,
                patch.object(bot, "_remember_task_owner"),
                patch.object(bot, "_remember_task_meta"),
                patch.object(bot, "_record_download_added_history"),
                patch.object(bot, "_register_task_card_from_query"),
                patch.object(bot, "_start_task_card_refresh"),
            ):
                await bot.series_continue_callback(update, context)
                return save_sub

        save_sub = asyncio.run(run())

        entry = attempt_download.await_args.args[0]
        self.assertFalse(entry["subscribe"])
        save_sub.assert_not_called()

    def test_continue_same_topic_no_update_does_not_download(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:update_topic:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.get_topic_title.return_value = "The Rookie S8E1-8 of 18 WEB-DL 1080p"
        attempt_download = AsyncMock()

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
                patch.object(bot, "_series_continue_active_task", AsyncMock(return_value=None)),
                patch.object(bot, "_attempt_pending_download", attempt_download),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        attempt_download.assert_not_awaited()
        text = update.callback_query.edit_message_text.await_args.args[0]
        keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("пока нет новых серий", text)
        self.assertIn("cont:subscribe_topic:all:0", self._callbacks(keyboard))
        self.assertIn("cont:search_alt:all:0", self._callbacks(keyboard))

    def test_continue_same_topic_skips_existing_active_task(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:update_topic:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        attempt_download = AsyncMock()

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", MagicMock()),
                patch.object(bot, "_series_continue_active_task", AsyncMock(return_value={"id": "dbid_1"})),
                patch.object(bot, "_attempt_pending_download", attempt_download),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        attempt_download.assert_not_awaited()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("уже есть активная задача", text)

    def test_continue_subscribe_topic_saves_subscription_without_download(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:subscribe_topic:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.get_topic_title.return_value = "The Rookie S8E1-8 of 18 WEB-DL 1080p"
        save_sub = MagicMock(return_value=("12345", {}))

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
                patch.object(bot, "_save_subscription_for_result", save_sub),
                patch.object(bot, "_attempt_pending_download", AsyncMock()) as attempt_download,
            ):
                await bot.series_continue_callback(update, context)
                return attempt_download

        attempt_download = asyncio.run(run())

        save_sub.assert_called_once()
        attempt_download.assert_not_awaited()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Подписка сохранена", text)

    def test_continue_search_alternatives_renders_other_topics(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:search_alt:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.search.return_value = [
            self._rt_result("12345", "The Rookie S8E1-12 of 18 WEB-DL 1080p"),
            self._rt_result("999", "The Rookie S8E1-12 of 18 WEB-DL 1080p"),
        ]

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        alternatives = context.user_data[bot.CONTINUE_STATE_KEY]["continue_state:alt:all:0"]
        self.assertEqual([item["topic_id"] for item in alternatives], ["999"])
        text = update.callback_query.edit_message_text.await_args.args[0]
        keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("Похожие раздачи", text)
        self.assertIn("cont:alt_dl:all:0:0", self._callbacks(keyboard))

    def test_continue_plex_only_empty_alternatives_retry_search_without_subscribe(self):
        candidate = self._candidate(topic_id="", source="plex")
        state = {"mine": [], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:search_alt:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.search.return_value = []

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        callbacks = self._callbacks(update.callback_query.edit_message_text.await_args.kwargs["reply_markup"])
        self.assertIn("cont:search_alt:all:0", callbacks)
        self.assertNotIn("cont:update_topic:all:0", callbacks)
        self.assertNotIn("cont:subscribe_topic:all:0", callbacks)

    def test_continue_unavailable_same_topic_offers_alternative_search(self):
        candidate = self._candidate()
        state = {"mine": [candidate], "all": [candidate], "scope": "all", "page": 0}
        update = _make_callback_update(chat_id=100, callback_data="cont:update_topic:all:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        rt_client = MagicMock()
        rt_client.get_topic_title.side_effect = bot.RutrackerTopicUnavailable("deleted")

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "rutracker_client", rt_client),
                patch.object(bot, "_series_continue_active_task", AsyncMock(return_value=None)),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        callbacks = self._callbacks(update.callback_query.edit_message_text.await_args.kwargs["reply_markup"])
        self.assertIn("cont:search_alt:all:0", callbacks)
        self.assertNotIn("cont:update_topic:all:0", callbacks)

    def test_continue_download_alternative_uses_updated_release_wording_and_subscription(self):
        candidate = self._candidate()
        alt = {
            "source": "rutracker",
            "topic_id": "999",
            "title": "The Rookie S8E1-12 of 18 WEB-DL 1080p",
            "url": "https://rutracker.org/forum/viewtopic.php?t=999",
            "tracker_name": "rutracker",
            "quality": "1080p",
        }
        state = {
            "mine": [candidate],
            "all": [candidate],
            "scope": "all",
            "page": 0,
            "continue_state:alt:all:0": [alt],
        }
        update = _make_callback_update(chat_id=100, callback_data="cont:alt_dl:all:0:0")
        context = _make_context(user_data={bot.CONTINUE_STATE_KEY: state})
        attempt_download = AsyncMock(return_value=("dbid_9", "torrent-файл"))
        save_sub = MagicMock(return_value=("999", {}))

        async def run():
            with (
                self._allowed_context(),
                patch.object(bot, "_series_continue_active_task", AsyncMock(return_value=None)),
                patch.object(bot, "_attempt_pending_download", attempt_download),
                patch.object(bot, "_save_subscription_for_result", save_sub),
                patch.object(bot, "_remember_task_owner"),
                patch.object(bot, "_remember_task_meta"),
                patch.object(bot, "_record_download_added_history"),
                patch.object(bot, "_register_task_card_from_query"),
                patch.object(bot, "_start_task_card_refresh"),
            ):
                await bot.series_continue_callback(update, context)

        asyncio.run(run())

        entry = attempt_download.await_args.args[0]
        self.assertEqual(entry["topic_id"], "999")
        self.assertTrue(entry["subscribe"])
        save_sub.assert_called_once()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Буду следить", text)


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
        context.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)
        self.assertIn("Film", progress_message.edit_text.call_args.args[0])
        self.assertEqual(bot.DOWNLOAD_PANEL_MESSAGES[100], 77)
        bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)

    def test_status_includes_youtube_download_jobs(self):
        update = _make_message_update(chat_id=100)
        context = _make_context()
        progress_message = MagicMock()
        progress_message.message_id = 77
        progress_message.edit_text = AsyncMock()
        context.bot.send_message.return_value = progress_message

        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = []
        bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "file_size": 1024,
                    "updated_at": "2026-06-16T22:57:00+03:00",
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "ds_client", fake_ds),
            ):
                asyncio.run(status(update, context))

        text = progress_message.edit_text.await_args.args[0]
        self.assertIn("YouTube Clip", text)
        self.assertIn("1. ✅ YouTube Clip", text)
        self.assertEqual(bot.DOWNLOAD_PANEL_MESSAGES[100], 77)
        bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
        bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)

    def test_task_list_callback_includes_youtube_download_jobs(self):
        update = _make_callback_update(chat_id=100, callback_data="task:list:mine")
        context = _make_context()
        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = []

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "file_size": 1024,
                    "updated_at": "2026-06-16T22:57:00+03:00",
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "ds_client", fake_ds),
            ):
                asyncio.run(bot.task_callback(update, context))

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("YouTube Clip", text)
        self.assertIn("1. ✅ YouTube Clip", text)

    def test_task_info_callback_opens_youtube_job_without_download_station(self):
        update = _make_callback_update(chat_id=100, callback_data="task:info:yt_1")
        context = _make_context()
        fake_ds = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "file_size": 1024,
                    "updated_at": "2026-06-16T22:57:00+03:00",
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "ds_client", fake_ds),
            ):
                asyncio.run(bot.task_callback(update, context))

        fake_ds.list_tasks.assert_not_called()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("YouTube Clip", text)
        keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("task:list:default", callbacks)
        self.assertIn("task:delete_youtube_ask:yt_1", callbacks)
        self.assertNotIn("task:delete_ask:yt_1", callbacks)

    def test_task_delete_youtube_removes_files_and_job_record(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            file_path = item_dir / "Clip.mp4"
            file_path.write_bytes(b"mp4")
            (item_dir / "poster.jpg").write_bytes(b"poster")
            (root / "Channel" / "channel-poster.png").write_bytes(b"channel poster")

            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "item_dir": str(item_dir),
                    "file_path": str(file_path),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
            ):
                asyncio.run(bot.task_callback(update, context))

            self.assertFalse(item_dir.exists())
            self.assertFalse((root / "Channel").exists())
            self.assertEqual(store.load_youtube_downloads(), {})
            history = store.load_download_history(chat_id=100)
            self.assertEqual(history[-1]["event"], "youtube_deleted")

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("YouTube-ролик удалён", text)
        self.assertIn("Файлов удалено: 2", text)

    def test_task_delete_youtube_clears_record_when_files_are_already_missing(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_dir = root / "Channel" / "Missing Clip"
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "Missing Clip",
                    "item_dir": str(missing_dir),
                    "file_path": str(missing_dir / "Missing Clip.mp4"),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
            ):
                asyncio.run(bot.task_callback(update, context))

            self.assertEqual(store.load_youtube_downloads(), {})

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Уже отсутствовали на NAS: 1", text)

    def test_task_delete_youtube_refreshes_plex_library(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()
        plex = MagicMock()
        plex.refresh_section.return_value = True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            (item_dir / "Clip.mp4").write_bytes(b"mp4")
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "item_dir": str(item_dir),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", "9"),
            ):
                asyncio.run(bot.task_callback(update, context))

        plex.refresh_section.assert_called_once_with("9")
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Plex: обновление библиотеки запущено.", text)

    def test_task_delete_youtube_queues_plex_refresh_retry_on_timeout(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()
        plex = MagicMock()
        plex.refresh_section.side_effect = bot.PlexTimeoutError("timeout")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            (item_dir / "Clip.mp4").write_bytes(b"mp4")
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "item_dir": str(item_dir),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", "9"),
            ):
                asyncio.run(bot.task_callback(update, context))

            pending = store.load_youtube_plex_refresh_pending()

        self.assertEqual(pending["reason"], "transient")
        self.assertEqual(pending["attempts"], 0)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Plex: обновление не удалось запустить, повторю фоном.", text)

    def test_task_delete_youtube_does_not_retry_permanent_plex_auth_error(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()
        plex = MagicMock()
        plex.refresh_section.side_effect = bot.PlexAuthError("bad token")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            (item_dir / "Clip.mp4").write_bytes(b"mp4")
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "item_dir": str(item_dir),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", "9"),
            ):
                asyncio.run(bot.task_callback(update, context))

            pending = store.load_youtube_plex_refresh_pending()

        self.assertEqual(pending, {})
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Plex: не удалось запустить обновление библиотеки.", text)

    def test_task_delete_youtube_queues_short_retry_when_refresh_returns_false(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()
        plex = MagicMock()
        plex.refresh_section.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            (item_dir / "Clip.mp4").write_bytes(b"mp4")
            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "YouTube Clip",
                    "item_dir": str(item_dir),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", "9"),
            ):
                asyncio.run(bot.task_callback(update, context))

            pending = store.load_youtube_plex_refresh_pending()

        self.assertEqual(pending["reason"], "refresh_false")
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Plex: обновление не удалось запустить, повторю фоном.", text)

    def test_task_delete_youtube_keeps_record_for_unsafe_path(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube:yt_1")
        context = _make_context()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "youtube"
            root.mkdir()
            outside = Path(tmp) / "outside" / "Clip"
            outside.mkdir(parents=True)
            (outside / "Clip.mp4").write_bytes(b"mp4")

            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_1": {
                    "id": "yt_1",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "Unsafe Clip",
                    "item_dir": str(outside),
                    "file_path": str(outside / "Clip.mp4"),
                }
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
            ):
                asyncio.run(bot.task_callback(update, context))

            self.assertIn("yt_1", store.load_youtube_downloads())
            self.assertTrue((outside / "Clip.mp4").exists())

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("YouTube-ролик не удалён", text)
        self.assertIn("Пропущено из-за небезопасного пути: 1", text)

    def test_task_delete_youtube_all_removes_terminal_jobs_only(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube_all:mine")
        context = _make_context()
        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done_dir = root / "Channel" / "Done"
            failed_dir = root / "Channel" / "Failed"
            active_dir = root / "Channel" / "Active"
            for item_dir in (done_dir, failed_dir, active_dir):
                item_dir.mkdir(parents=True)
                (item_dir / f"{item_dir.name}.mp4").write_bytes(b"mp4")

            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_done": {
                    "id": "yt_done",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "Done",
                    "item_dir": str(done_dir),
                },
                "yt_failed": {
                    "id": "yt_failed",
                    "chat_id": 100,
                    "status": "failed",
                    "title": "Failed",
                    "item_dir": str(failed_dir),
                },
                "yt_active": {
                    "id": "yt_active",
                    "chat_id": 100,
                    "status": "downloading",
                    "title": "Active",
                    "item_dir": str(active_dir),
                },
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
                patch.object(bot, "ds_client", fake_ds),
            ):
                asyncio.run(bot.task_callback(update, context))

            jobs = store.load_youtube_downloads()
            self.assertEqual(set(jobs), {"yt_active"})
            self.assertFalse(done_dir.exists())
            self.assertFalse(failed_dir.exists())
            self.assertTrue(active_dir.exists())

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("YouTube-ролики удалены", text)
        self.assertIn("Записей очищено: 2", text)
        self.assertIn("Active", text)

    def test_task_delete_youtube_all_refreshes_plex_once(self):
        update = _make_callback_update(chat_id=100, callback_data="task:delete_youtube_all:mine")
        context = _make_context()
        fake_ds = MagicMock()
        fake_ds.list_tasks.return_value = []
        plex = MagicMock()
        plex.refresh_section.return_value = True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_dir = root / "Channel" / "First"
            second_dir = root / "Channel" / "Second"
            for item_dir in (first_dir, second_dir):
                item_dir.mkdir(parents=True)
                (item_dir / f"{item_dir.name}.mp4").write_bytes(b"mp4")

            store = _make_store(tmp)
            store.save_youtube_downloads({
                "yt_first": {
                    "id": "yt_first",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "First",
                    "item_dir": str(first_dir),
                },
                "yt_second": {
                    "id": "yt_second",
                    "chat_id": 100,
                    "status": "completed",
                    "title": "Second",
                    "item_dir": str(second_dir),
                },
            })

            with (
                patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
                patch.object(bot, "ADMIN_CHAT_IDS", set()),
                patch.object(bot, "state_store", store),
                patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                patch.object(bot, "YOUTUBE_DOWNLOAD_DIR", root),
                patch.object(bot, "ds_client", fake_ds),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", "9"),
            ):
                asyncio.run(bot.task_callback(update, context))

        plex.refresh_section.assert_called_once_with("9")
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Plex: обновление библиотеки запущено.", text)

    def test_youtube_plex_refresh_retry_succeeds_and_clears_pending(self):
        plex = MagicMock()
        plex.refresh_section.return_value = True

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_plex_refresh_pending({
                "reason": "transient",
                "attempts": 0,
                "next_retry_at": 0,
                "last_error": "timeout",
            })

            with (
                patch.object(bot, "state_store", store),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", "9"),
            ):
                asyncio.run(bot._run_youtube_plex_refresh_retry_once())

            self.assertEqual(store.load_youtube_plex_refresh_pending(), {})

        plex.refresh_section.assert_called_once_with("9")

    def test_youtube_plex_refresh_retry_stops_after_single_missing_section_retry(self):
        plex = MagicMock()
        plex.find_section_by_title.return_value = ""

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_youtube_plex_refresh_pending({
                "reason": "section_not_found",
                "attempts": 0,
                "next_retry_at": 0,
                "last_error": "library not found",
            })

            with (
                patch.object(bot, "state_store", store),
                patch.object(bot, "PLEX_ENABLED", True),
                patch.object(bot, "plex_client", plex),
                patch.object(bot, "YOUTUBE_PLEX_SECTION", ""),
            ):
                asyncio.run(bot._run_youtube_plex_refresh_retry_once())

            self.assertEqual(store.load_youtube_plex_refresh_pending(), {})

        plex.find_section_by_title.assert_called_once()

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

    def test_progress_panel_updates_for_active_youtube_download(self):
        mock_ds = MagicMock()
        mock_ds.list_tasks.return_value = []
        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        bot.DOWNLOAD_PANEL_MESSAGES[100] = 77
        bot.DOWNLOAD_PANEL_PAGES[100] = 0
        bot.DOWNLOAD_PANEL_SCOPES[100] = bot.TASK_LIST_SCOPE_MY
        bot.DOWNLOAD_PANEL_HAD_ACTIVE[100] = False

        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = _make_store(tmp)
                store.save_youtube_downloads({
                    "yt_1": {
                        "id": "yt_1",
                        "chat_id": 100,
                        "status": "downloading",
                        "title": "YouTube Clip",
                        "downloaded_bytes": 10,
                        "total_bytes": 100,
                    }
                })

                with (
                    patch.object(bot, "ds_client", mock_ds),
                    patch.object(bot, "ADMIN_CHAT_IDS", set()),
                    patch.object(bot, "state_store", store),
                    patch.object(bot, "YOUTUBE_DOWNLOADS_ENABLED", True),
                ):
                    asyncio.run(_run_progress_panel_update_once(app))

            self.assertEqual(app.bot.edit_message_text.await_count, 1)
            self.assertIn("YouTube Clip", app.bot.edit_message_text.await_args.kwargs["text"])
            self.assertTrue(bot.DOWNLOAD_PANEL_HAD_ACTIVE[100])
        finally:
            bot.DOWNLOAD_PANEL_MESSAGES.pop(100, None)
            bot.DOWNLOAD_PANEL_PAGES.pop(100, None)
            bot.DOWNLOAD_PANEL_SCOPES.pop(100, None)
            bot.DOWNLOAD_PANEL_HAD_ACTIVE.pop(100, None)


# ---------------------------------------------------------------------------
# search_cancel tests
# ---------------------------------------------------------------------------


class SearchNoResultsFallbackTests(unittest.TestCase):
    """Handlers for 'no results' fallback buttons: expand trackers, drop quality, combined."""

    def test_expand_all_trackers_sets_all_indexers_and_reruns(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:expand_all_trackers")
        context = _make_context(user_data={
            "srch_query": "Аркейн",
            "srch_search_query": "Аркейн 1080p",
            "srch_jackett_indexers": [{"id": "rutracker"}, {"id": "nnmclub"}, {"id": "kinozal"}],
            "srch_jackett_selected": {"rutracker"},
        })
        with patch.object(bot, "_execute_search", AsyncMock(return_value=bot.SEARCH_RESULTS)) as exec_mock:
            asyncio.run(bot.search_expand_all_trackers(update, context))

        # All known indexers are now selected
        self.assertEqual(
            context.user_data["srch_jackett_selected"],
            {"rutracker", "nnmclub", "kinozal"},
        )
        # _execute_search called with the original search_query (with quality)
        exec_mock.assert_awaited_once()
        _, _, sq = exec_mock.call_args.args
        self.assertEqual(sq, "Аркейн 1080p")

    def test_expand_all_trackers_aborts_when_indexers_unknown(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:expand_all_trackers")
        context = _make_context(user_data={
            "srch_query": "Аркейн",
            "srch_jackett_indexers": [],   # Jackett never queried in this session
        })
        with patch.object(bot, "_execute_search", AsyncMock()) as exec_mock:
            result = asyncio.run(bot.search_expand_all_trackers(update, context))

        exec_mock.assert_not_awaited()
        # Conversation must end with a fallback message
        update.callback_query.edit_message_text.assert_called_once()
        from telegram.ext import ConversationHandler
        self.assertEqual(result, ConversationHandler.END)

    def test_no_quality_all_trackers_drops_quality_and_broadens(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:no_quality_all_trackers")
        context = _make_context(user_data={
            "srch_query": "Аркейн",
            "srch_search_query": "Аркейн 1080p",   # what was last executed
            "srch_jackett_indexers": [{"id": "rutracker"}, {"id": "nnmclub"}],
            "srch_jackett_selected": {"rutracker"},
        })
        with patch.object(bot, "_execute_search", AsyncMock(return_value=bot.SEARCH_RESULTS)) as exec_mock:
            asyncio.run(bot.search_no_quality_all_trackers(update, context))

        # Trackers broadened
        self.assertEqual(
            context.user_data["srch_jackett_selected"],
            {"rutracker", "nnmclub"},
        )
        # Quality dropped — _execute_search called with the BASE query, not search_query
        _, _, sq = exec_mock.call_args.args
        self.assertEqual(sq, "Аркейн")

    def test_no_quality_all_trackers_aborts_when_base_lost(self):
        update = _make_callback_update(chat_id=100, callback_data="srch:no_quality_all_trackers")
        context = _make_context(user_data={
            "srch_query": "",   # base lost
            "srch_jackett_indexers": [{"id": "rutracker"}],
        })
        with patch.object(bot, "_execute_search", AsyncMock()) as exec_mock:
            result = asyncio.run(bot.search_no_quality_all_trackers(update, context))

        exec_mock.assert_not_awaited()
        from telegram.ext import ConversationHandler
        self.assertEqual(result, ConversationHandler.END)

    def test_no_results_flags_detects_quality_suffix(self):
        """_no_results_flags returns has_quality=True when srch_query differs from search_query."""
        context = _make_context(user_data={"srch_query": "Аркейн"})
        has_q, _ = bot._no_results_flags(context, "Аркейн 1080p")
        self.assertTrue(has_q)

    def test_no_results_flags_no_quality_when_queries_match(self):
        context = _make_context(user_data={"srch_query": "Аркейн"})
        has_q, _ = bot._no_results_flags(context, "Аркейн")
        self.assertFalse(has_q)

    def test_no_results_flags_jackett_can_expand_strict_subset(self):
        context = _make_context(user_data={
            "srch_query": "X",
            "srch_jackett_indexers": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "srch_jackett_selected": {"a"},
        })
        with patch.object(bot, "jackett_client", object()):
            _, can_exp = bot._no_results_flags(context, "X")
        self.assertTrue(can_exp)

    def test_no_results_flags_jackett_cannot_expand_when_all_selected(self):
        context = _make_context(user_data={
            "srch_query": "X",
            "srch_jackett_indexers": [{"id": "a"}, {"id": "b"}],
            "srch_jackett_selected": {"a", "b"},
        })
        with patch.object(bot, "jackett_client", object()):
            _, can_exp = bot._no_results_flags(context, "X")
        self.assertFalse(can_exp)

    def test_no_results_flags_jackett_cannot_expand_when_no_indexers_known(self):
        context = _make_context(user_data={
            "srch_query": "X",
            "srch_jackett_indexers": [],
        })
        with patch.object(bot, "jackett_client", object()):
            _, can_exp = bot._no_results_flags(context, "X")
        self.assertFalse(can_exp)

    def test_no_results_flags_jackett_cannot_expand_when_no_jackett(self):
        context = _make_context(user_data={
            "srch_query": "X",
            "srch_jackett_indexers": [{"id": "a"}, {"id": "b"}],
            "srch_jackett_selected": {"a"},
        })
        with patch.object(bot, "jackett_client", None):
            _, can_exp = bot._no_results_flags(context, "X")
        self.assertFalse(can_exp)


class SearchResultsTextTests(unittest.TestCase):
    def test_partial_results_explain_download_and_notification_buttons(self):
        text = bot._build_results_text(
            [{
                "title": "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10",
                "size": "10 GB",
                "seeders": 12,
                "partial": True,
                "ep_str": "1-8 из 10",
            }],
            "Клиника 1080p",
            0,
        )

        self.assertIn("⬇️ N — варианты скачивания; 🔔 N — варианты уведомлений.", text)

    def test_plain_results_do_not_explain_subscribe_button(self):
        text = bot._build_results_text(
            [{"title": "Драйв", "size": "8 GB", "seeders": 7}],
            "Драйв 1080p",
            0,
        )

        self.assertNotIn("🔔 N", text)


class TaskAddedMessageTests(unittest.TestCase):
    def test_magnet_without_task_id_uses_honest_intro(self):
        text = bot._task_added_message(
            "magnet-ссылка",
            accepted_without_task_id=True,
        )

        self.assertTrue(text.startswith("✅ Magnet отправлен в очередь скачивания"))
        self.assertNotIn("Задача добавлена", text)

    def test_regular_task_keeps_added_intro(self):
        with patch.object(bot, "PLEX_ENABLED", True):
            text = bot._task_added_message("torrent-файл", title="Movie 1080p", task_id="task_123")

        self.assertTrue(text.startswith("✅ Задача добавлена в очередь скачивания"))
        self.assertIn("Раздача: Movie 1080p", text)
        self.assertNotIn("ID: task_123", text)
        self.assertIn("Что дальше:", text)
        self.assertIn("бот сообщит об этом, затем проверит Plex", text)

    def test_regular_task_without_plex_mentions_finish_only(self):
        with patch.object(bot, "PLEX_ENABLED", False):
            text = bot._task_added_message("torrent-файл", title="Movie 1080p", task_id="task_123")

        self.assertIn("Когда загрузка завершится, бот сообщит об этом.", text)
        self.assertNotIn("проверит Plex", text)

    def test_successful_tracker_result_is_hidden_from_added_message(self):
        result = bot.TrackerApplyResult(added_count=5, available_count=5)

        with patch.object(bot, "_public_trackers_enabled", return_value=True):
            text = bot._task_added_message("torrent-файл", task_id="task_123", tracker_result=result)

        self.assertNotIn("Public-трекеры", text)

    def test_tracker_skip_reason_remains_visible(self):
        result = bot.TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")

        with patch.object(bot, "_public_trackers_enabled", return_value=True):
            text = bot._task_added_message("torrent-файл", task_id="task_123", tracker_result=result)

        self.assertIn("Public-трекеры: приватный torrent, не добавляю", text)


class TorrentFileProcessingTests(unittest.TestCase):
    def test_torrent_file_without_task_id_is_not_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_path = Path(tmp) / "movie.torrent"
            temp_path.write_bytes(b"d4:infod4:name5:movieee")
            message = MagicMock()
            message.edit_text = AsyncMock()
            context = MagicMock()
            context.application = MagicMock()
            mock_ds = MagicMock()
            mock_ds.create_torrent_file.return_value = ""

            with (
                patch.object(bot, "ds_client", mock_ds),
                patch.object(bot, "_add_public_trackers_to_download_task") as trackers,
                patch.object(bot, "_remember_task_owner") as remember_owner,
            ):
                asyncio.run(
                    bot._do_process_torrent(
                        message,
                        context,
                        temp_path,
                        "movie.torrent",
                        chat_id=100,
                    )
                )

            self.assertEqual(message.edit_text.await_count, 2)
            final_text = message.edit_text.await_args.args[0]
            self.assertIn("Torrent-файл отправлен в очередь скачивания", final_text)
            self.assertIn("бот пока не видит созданную задачу", final_text)
            self.assertNotIn("Не удалось обработать .torrent", final_text)
            trackers.assert_not_called()
            remember_owner.assert_not_called()
            self.assertFalse(temp_path.exists())


class TaskCallbackErrorKeyboardTests(unittest.TestCase):
    def _buttons(self, keyboard) -> dict[str, str]:
        return {button.text: button.callback_data for row in keyboard.inline_keyboard for button in row}

    def test_get_task_error_offers_retry_list_and_close(self):
        update = _make_callback_update(chat_id=100, callback_data="task:info:task_123")
        context = _make_context()
        fake_ds = MagicMock()
        fake_ds.list_tasks.side_effect = bot.DownloadStationError("DS down")

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(load_approved_chat_ids=MagicMock(return_value=set()))),
            patch.object(bot, "_can_access_task_id", return_value=True),
            patch.object(bot, "ds_client", fake_ds),
        ):
            asyncio.run(bot.task_callback(update, context))

        call = update.callback_query.edit_message_text.await_args
        self.assertIn("Не удалось получить задачу", call.args[0])
        self.assertNotIn("DS down", call.args[0])
        self.assertIn("Download Station сейчас", call.args[0])
        buttons = self._buttons(call.kwargs["reply_markup"])
        self.assertEqual(buttons["🔄 Попробовать снова"], "task:info:task_123")
        self.assertEqual(buttons["📚 К списку загрузок"], "task:list:mine")
        self.assertEqual(buttons["✖️ Закрыть"], "task:close:")


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
        context.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)

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
        context.bot.delete_message.assert_any_await(chat_id=100, message_id=77)
        context.bot.delete_message.assert_any_await(chat_id=100, message_id=42)
        self.assertEqual(context.bot.delete_message.await_count, 2)

    def test_deletes_command_when_no_photo(self):
        update = _make_command_update()
        context = _make_context()
        asyncio.run(search_cancel(update, context))
        context.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)


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


class TaskCardRegistrationTests(unittest.TestCase):
    def setUp(self):
        TASK_CARD_MESSAGES.clear()
        bot.DOWNLOAD_PANEL_MESSAGES.clear()
        bot.DOWNLOAD_PANEL_PAGES.clear()
        bot.DOWNLOAD_PANEL_SCOPES.clear()
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.clear()

    def tearDown(self):
        TASK_CARD_MESSAGES.clear()
        bot.DOWNLOAD_PANEL_MESSAGES.clear()
        bot.DOWNLOAD_PANEL_PAGES.clear()
        bot.DOWNLOAD_PANEL_SCOPES.clear()
        bot.DOWNLOAD_PANEL_HAD_ACTIVE.clear()

    def test_task_card_registration_detaches_same_message_from_download_panel(self):
        bot.DOWNLOAD_PANEL_MESSAGES[100] = 42
        bot.DOWNLOAD_PANEL_PAGES[100] = 1
        bot.DOWNLOAD_PANEL_SCOPES[100] = bot.TASK_LIST_SCOPE_ALL
        bot.DOWNLOAD_PANEL_HAD_ACTIVE[100] = True

        with (
            patch.object(bot, "_task_owner", return_value=100),
            patch.object(bot, "_remember_task_owner") as remember_owner,
        ):
            bot._register_task_card_message(chat_id=100, message_id=42, task_id="tid1")

        self.assertIn((100, 42), TASK_CARD_MESSAGES["tid1"])
        self.assertNotIn(100, bot.DOWNLOAD_PANEL_MESSAGES)
        self.assertNotIn(100, bot.DOWNLOAD_PANEL_PAGES)
        self.assertNotIn(100, bot.DOWNLOAD_PANEL_SCOPES)
        self.assertNotIn(100, bot.DOWNLOAD_PANEL_HAD_ACTIVE)
        remember_owner.assert_not_called()


class TextMessageEntryTaskCardPreservationTests(unittest.IsolatedAsyncioTestCase):
    """text_message_entry must NOT delete a message that is a live task card.

    Scenario: user starts a download from search results → the search UI message
    is edited in-place into a task card and registered in TASK_CARD_MESSAGES.
    srch_ui_msg_id still points to that message_id.  When the user types a new
    search query, the old message must be preserved so the background monitor can
    update it to a progress card.
    """

    def setUp(self):
        TASK_CARD_MESSAGES.clear()

    def tearDown(self):
        TASK_CARD_MESSAGES.clear()

    def _make_update(self, text: str = "Дюна", chat_id: int = 100):
        chat = MagicMock()
        chat.id = chat_id
        msg = MagicMock()
        msg.text = text
        msg.chat_id = chat_id
        update = MagicMock()
        update.effective_chat = chat
        update.effective_user = MagicMock()
        update.effective_user.id = chat_id
        update.message = msg
        update.callback_query = None
        return update

    def _make_context(self, msg_id: int, chat_id: int = 100):
        context = MagicMock()
        context.user_data = {
            "srch_ui_msg_id": msg_id,
            "srch_ui_chat_id": chat_id,
        }
        context.bot = AsyncMock()
        context.bot.delete_message = AsyncMock()
        return context

    async def test_does_not_delete_task_card_message(self):
        """If srch_ui_msg_id points to a task card, it must NOT be deleted."""
        TASK_CARD_MESSAGES["task-1"] = {(100, 999)}
        update = self._make_update()
        context = self._make_context(msg_id=999, chat_id=100)

        with (
            patch("bot._is_allowed", return_value=True),
            patch("bot.rutracker_client", new=MagicMock()),
            patch("bot.search_got_query", new=AsyncMock(return_value=3)),
        ):
            await text_message_entry(update, context)

        context.bot.delete_message.assert_not_called()

    async def test_deletes_stale_search_message_when_not_a_task_card(self):
        """If srch_ui_msg_id does NOT belong to a task card, it should be deleted."""
        # TASK_CARD_MESSAGES is empty — no task card registered
        update = self._make_update()
        context = self._make_context(msg_id=888, chat_id=100)

        with (
            patch("bot._is_allowed", return_value=True),
            patch("bot.rutracker_client", new=MagicMock()),
            patch("bot.search_got_query", new=AsyncMock(return_value=3)),
        ):
            await text_message_entry(update, context)

        context.bot.delete_message.assert_called_once_with(chat_id=100, message_id=888)


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
            bootstrap_retries=5,
            drop_pending_updates=True,
            allowed_updates=TELEGRAM_ALLOWED_UPDATES,
        )

    def test_setup_starts_subscription_loop_for_jackett_only_mode(self):
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
        self.assertIn("seasons", [command.command for command in public_commands])
        self.assertIn("_subscription_check_loop", created)
        self.assertIn("_jackett_warmup_loop", created)
        self.assertIn("_jackett_guardian_loop", created)
        self.assertIn("_progress_update_loop", created)

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

    def setUp(self):
        self._allowed_patch = patch.object(bot, "ALLOWED_CHAT_IDS", {100})
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()

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

    def test_season_complete_keeps_subscription_if_download_failed(self):
        """Season complete but DS error → subscription is NOT removed (retry next check)."""
        sub = self._make_sub(last_episode_end=9, total_episodes=10)
        subs = {"jackett:aaa": sub}
        app = self._make_app()

        mock_rt = MagicMock()
        mock_rt.get_topic_title.return_value = (
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-10 из 10 [WEB-DL]"
        )
        mock_rt.download_torrent.side_effect = RutrackerError("connection failed")

        with (
            patch.object(bot, "rutracker_client", mock_rt),
            patch.object(bot, "ds_client", MagicMock()),
        ):
            result = asyncio.run(
                _check_jackett_sub_via_rutracker_direct(app, subs, "jackett:aaa", sub)
            )

        self.assertTrue(result)
        # Download failed → subscription must be kept for retry
        self.assertIn("jackett:aaa", subs)
        # Notification about failure should still be sent
        app.bot.send_message.assert_awaited_once()
        sent_text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("вручную", sent_text)

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
