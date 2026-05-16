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
    _enrich_cards_with_plex,
    _format_kp_votes,
    _format_movie_discovery_cache,
    _get_movie_subscriptions,
    _is_movie_subscribed,
    _set_movie_subscription,
    _run_movie_discovery_notifications,
    _flush_pending_movie_notifications,
    _merge_notification_stubs,
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
    movie_new_close_callback,
    movie_new_command,
    movie_new_refresh_callback,
    help_close_callback,
    search_cancel,
    search_timeout,
    setup_bot_commands,
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


class MovieDiscoveryNotificationTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _run_movie_discovery_notifications."""

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

    def _make_card(self, title: str, first_seen_at: str) -> dict:
        return {
            "title": title,
            "year": 2026,
            "first_seen_at": first_seen_at,
            "rating": 7.5,
        }

    async def test_initialises_baseline_on_first_run_without_notifying(self):
        settings: dict = {}
        self._patch_settings(settings)
        app = MagicMock()
        cache = {
            "updated_at": "2026-05-15 10:00",
            "cards": [self._make_card("Фильм", "2026-05-14 08:00")],
        }
        await _run_movie_discovery_notifications(cache, app)
        # Should NOT send any messages
        app.bot.send_message.assert_not_called()
        # Should set baseline timestamp
        self.assertEqual(settings["movie_notify_last_run_at"], "2026-05-15 10:00")

    async def test_sends_notifications_for_new_cards(self):
        settings = {
            "movie_notify_last_run_at": "2026-05-14 12:00",
            "movie_subscriptions": {"100": {"subscribed_at": "2026-05-14 11:00"}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 12:00",
            "cards": [
                self._make_card("Старый фильм", "2026-05-13 10:00"),  # before last_run_at
                self._make_card("Новый фильм", "2026-05-15 10:00"),   # after last_run_at
            ],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_called_once()
        call_kwargs = app.bot.send_message.call_args
        self.assertEqual(call_kwargs.kwargs["chat_id"], 100)
        self.assertIn("Новый фильм", call_kwargs.kwargs["text"])
        self.assertNotIn("Старый фильм", call_kwargs.kwargs["text"])

    async def test_only_top10_cards_are_considered(self):
        """Cards beyond position 10 must not trigger notifications."""
        settings = {
            "movie_notify_last_run_at": "2026-05-14 12:00",
            "movie_subscriptions": {"100": {"subscribed_at": "2026-05-14 11:00"}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        # 12 cards — only first 10 count; card 11+ is "new" but should be ignored
        cards = [self._make_card(f"Старый {i}", "2026-05-13 10:00") for i in range(10)]
        cards.append(self._make_card("Новый вне топ10", "2026-05-15 10:00"))
        cards.append(self._make_card("Тоже вне топ10", "2026-05-15 10:00"))
        cache = {"updated_at": "2026-05-15 12:00", "cards": cards}
        await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()

    async def test_no_notification_when_no_subscribers(self):
        settings = {
            "movie_notify_last_run_at": "2026-05-14 12:00",
            "movie_subscriptions": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 12:00",
            "cards": [self._make_card("Новый фильм", "2026-05-15 10:00")],
        }
        await _run_movie_discovery_notifications(cache, app)
        app.bot.send_message.assert_not_called()

    async def test_notification_keyboard_has_open_and_unsub_buttons(self):
        settings = {
            "movie_notify_last_run_at": "2026-05-14 12:00",
            "movie_subscriptions": {"100": {"subscribed_at": "2026-05-14 11:00"}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 12:00",
            "cards": [self._make_card("Новый фильм", "2026-05-15 10:00")],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        call_kwargs = app.bot.send_message.call_args.kwargs
        keyboard = call_kwargs["reply_markup"]
        buttons = {btn.text: btn.callback_data for row in keyboard.inline_keyboard for btn in row}
        self.assertIn("🎬 Открыть /new", buttons)
        self.assertEqual(buttons["🎬 Открыть /new"], "new:open")
        self.assertIn("🔕 Отписаться", buttons)
        self.assertTrue(buttons["🔕 Отписаться"].endswith(":new_unsub"))

    async def test_updates_last_run_at_after_sending(self):
        settings = {
            "movie_notify_last_run_at": "2026-05-14 12:00",
            "movie_subscriptions": {"100": {}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 12:00",
            "cards": [self._make_card("Новый фильм", "2026-05-15 10:00")],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)
        # last_run_at must be updated to current time (not the card's time)
        self.assertGreater(settings["movie_notify_last_run_at"], "2026-05-14 12:00")


class MovieNotificationWindowTests(unittest.IsolatedAsyncioTestCase):
    """Tests for time-window logic: deferred/pending notifications and flush."""

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

    def _make_card(self, title: str, first_seen_at: str) -> dict:
        return {"title": title, "year": 2026, "first_seen_at": first_seen_at, "rating": 7.0}

    # -- _merge_notification_stubs --

    def test_merge_stubs_deduplicates_by_title_year(self):
        existing = [{"title": "Фильм А", "year": 2026}, {"title": "Фильм Б", "year": 2025}]
        new = [{"title": "Фильм А", "year": 2026}, {"title": "Фильм В", "year": 2024}]
        result = _merge_notification_stubs(existing, new)
        titles = [s["title"] for s in result]
        self.assertEqual(titles, ["Фильм А", "Фильм Б", "Фильм В"])

    def test_merge_stubs_preserves_insertion_order(self):
        existing = [{"title": "A", "year": 2026}]
        new = [{"title": "B", "year": 2025}, {"title": "C", "year": 2024}]
        result = _merge_notification_stubs(existing, new)
        self.assertEqual([s["title"] for s in result], ["A", "B", "C"])

    # -- out-of-window deferral --

    async def test_out_of_window_defers_to_pending(self):
        """When outside quiet hours, new cards are added to pending and NOT sent."""
        settings = {
            "movie_notify_last_run_at": "2026-05-14 12:00",
            "movie_subscriptions": {"100": {}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 02:00",
            "cards": [self._make_card("Ночной фильм", "2026-05-15 01:00")],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=False):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_message.assert_not_called()
        pending = settings.get("movie_notify_pending") or []
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["title"], "Ночной фильм")

    async def test_out_of_window_accumulates_pending_without_duplicates(self):
        """Multiple out-of-window refreshes must deduplicate pending stubs."""
        settings = {
            "movie_notify_last_run_at": "2026-05-14 23:00",
            "movie_notify_pending": [{"title": "Старый Pending", "year": 2026}],
            "movie_subscriptions": {"100": {}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 02:00",
            "cards": [
                self._make_card("Старый Pending", "2026-05-15 00:00"),
                self._make_card("Новый фильм", "2026-05-15 01:00"),
            ],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=False):
            await _run_movie_discovery_notifications(cache, app)

        pending = settings.get("movie_notify_pending") or []
        titles = [s["title"] for s in pending]
        self.assertIn("Старый Pending", titles)
        self.assertIn("Новый фильм", titles)
        # No duplicates
        self.assertEqual(len(titles), len(set(titles)))

    async def test_in_window_sends_pending_plus_new_and_clears(self):
        """Inside quiet hours: pending stubs + new cards are sent together, pending cleared."""
        settings = {
            "movie_notify_last_run_at": "2026-05-14 23:00",
            "movie_notify_pending": [{"title": "Отложенный фильм", "year": 2025, "rating": 6.5}],
            "movie_subscriptions": {"200": {}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        cache = {
            "updated_at": "2026-05-15 09:30",
            "cards": [self._make_card("Утренний фильм", "2026-05-15 09:00")],
        }
        with unittest.mock.patch("bot._is_in_notification_window", return_value=True):
            await _run_movie_discovery_notifications(cache, app)

        app.bot.send_message.assert_called_once()
        text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Отложенный фильм", text)
        self.assertIn("Утренний фильм", text)
        # Pending must be cleared after send
        self.assertEqual(settings.get("movie_notify_pending"), [])

    # -- _flush_pending_movie_notifications --

    async def test_flush_sends_pending_and_clears(self):
        settings = {
            "movie_notify_pending": [{"title": "Ожидающий фильм", "year": 2026, "rating": 8.0}],
            "movie_subscriptions": {"300": {}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        await _flush_pending_movie_notifications(app)

        app.bot.send_message.assert_called_once()
        text = app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Ожидающий фильм", text)
        self.assertEqual(settings.get("movie_notify_pending"), [])

    async def test_flush_does_nothing_when_no_pending(self):
        settings = {
            "movie_notify_pending": [],
            "movie_subscriptions": {"300": {}},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        await _flush_pending_movie_notifications(app)
        app.bot.send_message.assert_not_called()

    async def test_flush_clears_pending_even_with_no_subscribers(self):
        settings = {
            "movie_notify_pending": [{"title": "Фильм", "year": 2026}],
            "movie_subscriptions": {},
        }
        self._patch_settings(settings)
        app = AsyncMock()
        await _flush_pending_movie_notifications(app)
        app.bot.send_message.assert_not_called()
        self.assertEqual(settings.get("movie_notify_pending"), [])


# ---------------------------------------------------------------------------
# Plex pre-download check helpers
# ---------------------------------------------------------------------------


class PlexPreDownloadCheckTests(unittest.TestCase):
    """Tests for _plex_is_series, _plex_pre_check, _plex_confirm_text."""

    def _make_movie(self, resolution: str = "1080"):
        m = MagicMock()
        m.title = "Dune"
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
            patch.object(bot, "_plex_library", {("other movie", 2020): self._make_movie()}),
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

        with (
            patch.object(bot, "_plex_library", {("dune", 2021): movie}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=movie),
            patch.object(bot, "_plex_machine_id", "abc123"),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Dune.2021.1080p", [100], max_attempts=1, interval_seconds=0
            ))

        fake_app.bot.send_message.assert_awaited_once()
        call_kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], 100)
        self.assertIn("✅", call_kwargs["text"])
        self.assertIn("Dune.2021.1080p", call_kwargs["text"])

    def test_poll_after_finish_sends_timeout_notification_when_not_found(self):
        """Polling should send a timeout-notification when exhausted without finding the movie."""
        fake_app = MagicMock()
        fake_app.bot.send_message = AsyncMock()

        with (
            patch.object(bot, "_plex_library", {}),
            patch.object(bot, "_refresh_plex_library", AsyncMock()),
            patch.object(bot, "_plex_find_by_ds_title", return_value=None),
            patch.object(bot, "_PLEX_POLLING_TASKS", {}),
        ):
            asyncio.run(_plex_poll_after_finish(
                fake_app, "task1", "Some.Movie.2024", [100], max_attempts=1, interval_seconds=0
            ))

        fake_app.bot.send_message.assert_awaited_once()
        call_kwargs = fake_app.bot.send_message.call_args.kwargs
        self.assertIn("⚠️", call_kwargs["text"])
        self.assertIn("Some.Movie.2024", call_kwargs["text"])

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
        self.assertIn("⚠️", fake_app.bot.send_message.call_args.kwargs["text"])

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

    def test_plex_button_appears_only_for_final_download_statuses(self):
        with patch.object(bot, "PLEX_ENABLED", True):
            finished_labels = self._labels(_notification_keyboard("tid1", "finished", "bt"))
            seeding_labels = self._labels(_notification_keyboard("tid1", "seeding", "bt"))
            error_labels = self._labels(_notification_keyboard("tid1", "error", "bt"))

        self.assertIn("▶️ Открыть Plex (iOS)", finished_labels)
        self.assertIn("▶️ Открыть Plex (iOS)", seeding_labels)
        self.assertNotIn("▶️ Открыть Plex (iOS)", error_labels)

    def test_plex_button_uses_plex_scheme_not_http(self):
        """Кнопка должна открывать Plex-приложение (plex://), а не HTTP URL в браузере."""
        with patch.object(bot, "PLEX_ENABLED", True):
            keyboard = _notification_keyboard("tid1", "finished", "bt")
        urls = self._urls(keyboard)
        plex_url = urls.get("▶️ Открыть Plex (iOS)", "")
        self.assertTrue(
            plex_url.startswith("plex://"),
            f"Ожидался plex:// URL, получен: {plex_url!r}",
        )


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
