"""Search-fallback policy tests (Jackett + Rutracker interplay).

Pinned behaviour:
  - Jackett returned [] → SKIP Rutracker fallback (it's currently broken at
    search/login pages anyway), go straight to no-results with did-you-mean.
  - Jackett ERRORED → Rutracker direct fallback (existing alternative source).
  - Both sources fail → no-results screen with explanatory banner, NOT a
    fatal error screen — user still sees did-you-mean.
  - Pure-Rutracker install (no Jackett) → RutrackerError stays fatal
    (no fallback to soften the blow).
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from jackett import JackettError
from rutracker import RutrackerError


def _make_send_fn():
    """Return (send_fn, message_mock). send_fn awaits to message_mock; edits
    on message_mock.edit_text are captured for assertion."""
    message = MagicMock()
    message.message_id = 1
    message.chat_id = 100
    message.edit_text = AsyncMock(return_value=None)
    send_fn = AsyncMock(return_value=message)
    return send_fn, message


def _make_context():
    ctx = MagicMock()
    ctx.user_data = {
        # Pre-populate selected indexers to skip the get_indexers branch.
        "srch_jackett_indexers": [{"id": "rutracker"}],
        "srch_jackett_selected": {"rutracker"},
        "srch_query": "test",
    }
    return ctx


class JackettEmptyDoesNotFallbackToRutrackerTests(unittest.TestCase):
    """The main bug fix: Jackett returning [] must NOT trigger Rutracker
    direct (which is currently broken and would error)."""

    def test_jackett_empty_does_not_call_rutracker(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = []  # 0 results — authoritative
        mock_rutracker = MagicMock()
        mock_rutracker.search = MagicMock(side_effect=AssertionError(
            "Rutracker MUST NOT be called when Jackett returned 0 results"
        ))
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rutracker),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "несуществующий запрос"))

        mock_jackett.search.assert_called_once()
        mock_rutracker.search.assert_not_called()

    def test_jackett_empty_shows_no_results_screen_with_did_you_mean(self):
        """No-results screen text must include the query and the GPT suggestions
        must appear as buttons (verified via the keyboard's callback_data)."""
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = []
        mock_rutracker = MagicMock()
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rutracker),
            patch.object(
                bot, "_gpt_get_did_you_mean",
                new=AsyncMock(return_value=["правильный запрос", "alt query"]),
            ),
        ):
            asyncio.run(bot._run_search(send_fn, context, "опечатка"))

        # edit_text called with no-results text
        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("опечатка", text)
        self.assertIn("ничего не найдено", text)
        # Did-you-mean suggestions appear in the inline keyboard
        keyboard = kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in keyboard for b in row]
        self.assertTrue(
            any("правильный запрос" in lbl for lbl in labels),
            f"Expected did-you-mean suggestion in keyboard: {labels}",
        )


class JackettErrorFallsBackToRutrackerTests(unittest.TestCase):
    """Distinguish: Jackett raising IS a legit reason to try Rutracker."""

    def test_jackett_error_triggers_rutracker_fallback(self):
        mock_jackett = MagicMock()
        mock_jackett.search.side_effect = JackettError("indexer offline")
        # Rutracker returns one fake result so we know it was tried + succeeded.
        rt_result = MagicMock()
        rt_result.topic_id = "12345"
        rt_result.title = "Some Movie [2024]"
        rt_result.category = "Movies"
        rt_result.size = "5 GB"
        rt_result.seeders = 42
        mock_rutracker = MagicMock()
        mock_rutracker.search.return_value = [rt_result]
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rutracker),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Some Movie"))

        mock_jackett.search.assert_called_once()
        mock_rutracker.search.assert_called_once()
        # Result must end up rendered to the user, not 0-results screen.
        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Some Movie [2024]", text)


class DoubleFailureLandsInNoResultsTests(unittest.TestCase):
    """When BOTH sources fail, we want the no-results screen (with did-you-
    mean), NOT the dead-end fatal error screen."""

    def test_jackett_error_then_rutracker_error_shows_no_results_with_banner(self):
        mock_jackett = MagicMock()
        mock_jackett.search.side_effect = JackettError("jackett down")
        mock_rutracker = MagicMock()
        mock_rutracker.search.side_effect = RutrackerError("rutracker login broken")
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rutracker),
            patch.object(
                bot, "_gpt_get_did_you_mean",
                new=AsyncMock(return_value=["альтернатива"]),
            ),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Some Query"))

        # No-results screen rendered (not fatal error screen).
        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("ничего не найдено", text)
        # Banner mentions both sources being down.
        self.assertIn("Оба источника недоступны", text)
        self.assertIn("Jackett", text)
        self.assertIn("Rutracker", text)
        # Did-you-mean suggestion still surfaced even on double failure.
        keyboard = kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in keyboard for b in row]
        self.assertTrue(any("альтернатива" in lbl for lbl in labels))


class RutrackerOnlyInstallKeepsFatalErrorTests(unittest.TestCase):
    """Pure-Rutracker install (no Jackett configured) → RutrackerError stays
    fatal — no fallback to soften the blow."""

    def test_rutracker_only_error_returns_fatal_error_screen(self):
        mock_rutracker = MagicMock()
        mock_rutracker.search.side_effect = RutrackerError("captcha required")
        send_fn, message = _make_send_fn()
        context = _make_context()
        context.user_data.pop("srch_jackett_selected", None)  # no Jackett ctx
        context.user_data.pop("srch_jackett_indexers", None)

        with (
            patch.object(bot, "jackett_client", None),  # critical: Jackett unconfigured
            patch.object(bot, "rutracker_client", mock_rutracker),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            result = asyncio.run(bot._run_search(send_fn, context, "Test"))

        # Fatal path returns ConversationHandler.END (some integer constant);
        # what matters is the error message vs no-results text — they are distinct.
        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertNotIn("ничего не найдено", text)


if __name__ == "__main__":
    unittest.main()
