"""Tests for the 1.3b subscription policy picker UI in bot.py.

The picker replaces the old «⬇️📺 Серии» / «⬇️🎯 Сезон» pair with a single
«🔔 N» button → preset picker (Style D) → optional advanced 2-step menu.
This test file covers:
  - search_subscribe_pick renders the preset keyboard with all 5 options
  - Each preset translates to the correct (notify_policy, download_policy) pair
  - The advanced flow stashes step-1 choice and applies it at commit time
  - Edge cases: stale index, missing user_data
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
from subscription_policy import (
    DOWNLOAD_AUTO_EACH_UPDATE, DOWNLOAD_NOTIFY_ONLY,
    DOWNLOAD_ONLY_WHEN_COMPLETE,
    NOTIFY_EACH_UPDATE, NOTIFY_FINAL_ONLY, NOTIFY_SILENT,
)


def _make_query(data: str) -> MagicMock:
    """Build a callback-query-shaped mock that records edit_message_text calls."""
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return q


def _make_context(*, results: list[dict] | None = None) -> MagicMock:
    ctx = MagicMock()
    if results is None:
        results = [{"title": "Test Show", "partial": True}]
    ctx.user_data = {"srch_results": results}
    return ctx


class SearchSubscribePickTests(unittest.TestCase):
    """search_subscribe_pick — first tap «🔔 N» opens the preset picker."""

    def test_renders_preset_picker_with_all_options(self):
        update = MagicMock(callback_query=_make_query("srch:sub_pick:0"))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_pick(update, ctx))

        update.callback_query.edit_message_text.assert_awaited_once()
        call = update.callback_query.edit_message_text.await_args
        text = call.args[0]
        kb = call.kwargs.get("reply_markup")
        # Hint-line above buttons explains «push» / «качать».
        self.assertIn("push", text)
        self.assertIn("качать", text)
        # All 5 buttons present (4 presets + advanced + back = 6 actually).
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("📺" in l for l in labels))  # each
        self.assertTrue(any("🎯" in l for l in labels))  # final
        self.assertTrue(any("📦" in l for l in labels))  # after-finale (NEW)
        self.assertTrue(any("🔕" in l for l in labels))  # notify-only
        self.assertTrue(any("⚙️" in l for l in labels))  # advanced
        self.assertTrue(any("К результатам" in l for l in labels))  # back

    def test_stale_index_returns_error_message(self):
        update = MagicMock(callback_query=_make_query("srch:sub_pick:5"))
        ctx = _make_context(results=[{"title": "X", "partial": True}])
        asyncio.run(bot.search_subscribe_pick(update, ctx))
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Результат недоступен", text)


class SearchSubscribePresetTests(unittest.TestCase):
    """search_subscribe_preset — direct subscribe with the chosen policy pair."""

    def _drive_preset(self, code: str):
        update = MagicMock(callback_query=_make_query(f"srch:sub_preset:0:{code}"))
        ctx = _make_context()
        captured = {}

        async def fake_download_and_add(query, context, index, **kw):
            captured["index"] = index
            captured["kwargs"] = kw
            return 0  # ConversationHandler state

        with patch.object(bot, "_download_and_add", side_effect=fake_download_and_add):
            asyncio.run(bot.search_subscribe_preset(update, ctx))
        return captured

    def test_each_preset_maps_to_each_update_auto(self):
        c = self._drive_preset("each")
        self.assertTrue(c["kwargs"]["subscribe"])
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def test_final_preset_maps_to_final_only_auto(self):
        c = self._drive_preset("final")
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)
        self.assertEqual(c["kwargs"]["notify_mode"], "season_complete")

    def test_after_finale_preset_uses_only_when_complete(self):
        """The new 1.3 capability — wait for season then download as a batch."""
        c = self._drive_preset("after")
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)

    def test_notify_only_preset_skips_download(self):
        c = self._drive_preset("notify")
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_NOTIFY_ONLY)

    def test_unknown_preset_returns_error(self):
        update = MagicMock(callback_query=_make_query("srch:sub_preset:0:bogus"))
        ctx = _make_context()
        with patch.object(bot, "_download_and_add", AsyncMock()) as dl:
            asyncio.run(bot.search_subscribe_preset(update, ctx))
        dl.assert_not_awaited()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Неизвестный", text)


class SearchSubscribeAdvancedFlowTests(unittest.TestCase):
    """Advanced 2-step menu: notify → download → commit."""

    def test_step1_renders_notify_options(self):
        update = MagicMock(callback_query=_make_query("srch:sub_advanced:0"))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_advanced(update, ctx))

        call = update.callback_query.edit_message_text.await_args
        text = call.args[0]
        kb = call.kwargs.get("reply_markup")
        self.assertIn("Шаг 1", text)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        # Three notify choices + back.
        self.assertTrue(any("каждой" in l for l in labels))
        self.assertTrue(any("сезон закроется" in l for l in labels))
        self.assertTrue(any("Не уведомлять" in l for l in labels))

    def test_step1_to_step2_carries_notify_choice(self):
        """After picking notify_policy in step 1, step 2 must show the
        downstream choice and remember the upstream pick via user_data."""
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_notify:0:{NOTIFY_FINAL_ONLY}"
        ))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_set_notify(update, ctx))

        self.assertEqual(
            ctx.user_data.get("srch_sub_notify_policy"), NOTIFY_FINAL_ONLY,
        )
        call = update.callback_query.edit_message_text.await_args
        text = call.args[0]
        kb = call.kwargs.get("reply_markup")
        self.assertIn("Шаг 2", text)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        # Step 2 must offer the only_when_complete option.
        self.assertTrue(any("Одним торрентом" in l for l in labels))

    def test_step2_commits_with_both_axes(self):
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_download:0:{DOWNLOAD_ONLY_WHEN_COMPLETE}"
        ))
        ctx = _make_context()
        # Simulate step 1 having stashed the notify choice.
        ctx.user_data["srch_sub_notify_policy"] = NOTIFY_SILENT

        captured = {}

        async def fake_download_and_add(query, context, index, **kw):
            captured.update(kw)
            return 0

        with patch.object(bot, "_download_and_add", side_effect=fake_download_and_add):
            asyncio.run(bot.search_subscribe_set_download(update, ctx))

        self.assertTrue(captured["subscribe"])
        self.assertEqual(captured["notify_policy"], NOTIFY_SILENT)
        self.assertEqual(captured["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)
        # Notify stash should be popped to prevent leakage into next session.
        self.assertNotIn("srch_sub_notify_policy", ctx.user_data)


class SearchSubscribeBackToResultsTests(unittest.TestCase):
    """«⬅️ К результатам» must re-render the results keyboard."""

    def test_rerenders_when_results_present(self):
        update = MagicMock(callback_query=_make_query("srch:sub_back_results:0"))
        ctx = _make_context(results=[{"title": "Test", "partial": True}])
        ctx.user_data["srch_results_page"] = 0

        with patch.object(bot, "_build_results_text", return_value="🔎 results"):
            asyncio.run(bot.search_subscribe_back_to_results(update, ctx))

        # Edit was called with a keyboard (re-rendered results screen).
        call = update.callback_query.edit_message_text.await_args
        self.assertIsNotNone(call.kwargs.get("reply_markup"))

    def test_missing_results_graceful_error(self):
        update = MagicMock(callback_query=_make_query("srch:sub_back_results:0"))
        ctx = _make_context(results=[])
        asyncio.run(bot.search_subscribe_back_to_results(update, ctx))
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Результаты потеряны", text)


if __name__ == "__main__":
    unittest.main()
