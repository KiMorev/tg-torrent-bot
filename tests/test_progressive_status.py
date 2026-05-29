"""Tests for progressive_status — the long-running operation UI helper.

Covers:
* Initial text + gif sent on .start()
* Stage updates fire at the right times (.edit_text called per stage)
* .stop() cancels the task and deletes the gif
* Graceful degradation: missing gif path doesn't crash
* Telegram failures (send_animation throws, edit_text throws) don't propagate
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

from progressive_status import (
    ProgressiveStatus,
    SEARCH_ANIMATION_PATH, VOICE_ANIMATION_PATH,
    search_stages, voice_stages,
)


def _make_text_msg() -> MagicMock:
    m = MagicMock()
    m.edit_text = AsyncMock()
    m.delete = AsyncMock()
    m.chat_id = 100
    m.message_id = 42
    return m


def _make_gif_msg() -> MagicMock:
    m = MagicMock()
    m.delete = AsyncMock()
    return m


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_sends_initial_text(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=_make_text_msg())
        bot.send_animation = AsyncMock(return_value=_make_gif_msg())

        p = ProgressiveStatus(
            bot, chat_id=100,
            initial_text="Старт", stages=[],
            gif_path=None,
        )
        msg = await p.start()
        self.assertIsNotNone(msg)
        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["text"], "Старт")
        self.assertEqual(kwargs["chat_id"], 100)
        # No gif path → send_animation not called.
        bot.send_animation.assert_not_called()

    async def test_start_sends_gif_when_path_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(b"fake-mp4-bytes")
            gif_path = Path(tmp.name)
        try:
            bot = MagicMock()
            bot.send_message = AsyncMock(return_value=_make_text_msg())
            bot.send_animation = AsyncMock(return_value=_make_gif_msg())

            p = ProgressiveStatus(
                bot, chat_id=100, initial_text="X", stages=[],
                gif_path=gif_path,
            )
            await p.start()
            bot.send_animation.assert_awaited_once()
            self.assertIsNotNone(p.gif_msg)
        finally:
            gif_path.unlink()

    async def test_start_skips_gif_when_path_missing(self):
        """File doesn't exist → no send_animation, no exception."""
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=_make_text_msg())
        bot.send_animation = AsyncMock()

        p = ProgressiveStatus(
            bot, chat_id=100, initial_text="X", stages=[],
            gif_path=Path("/tmp/does-not-exist-12345.mp4"),
        )
        await p.start()
        bot.send_animation.assert_not_called()
        self.assertIsNone(p.gif_msg)

    async def test_gif_failure_does_not_break_status(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(b"x")
            gif_path = Path(tmp.name)
        try:
            bot = MagicMock()
            bot.send_message = AsyncMock(return_value=_make_text_msg())
            bot.send_animation = AsyncMock(side_effect=RuntimeError("rate limit"))

            p = ProgressiveStatus(
                bot, chat_id=100, initial_text="X", stages=[],
                gif_path=gif_path,
            )
            msg = await p.start()
            # text_msg still returned
            self.assertIsNotNone(msg)
            # gif_msg stays None on failure
            self.assertIsNone(p.gif_msg)
        finally:
            gif_path.unlink()

    async def test_initial_send_failure_returns_none(self):
        """If we can't even send the initial text, return None cleanly."""
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        p = ProgressiveStatus(
            bot, chat_id=100, initial_text="X", stages=[(1.0, "next")],
            gif_path=None,
        )
        result = await p.start()
        self.assertIsNone(result)
        # No stage task should be scheduled.
        self.assertIsNone(p._task)


class StageRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stages_fire_in_order(self):
        text_msg = _make_text_msg()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=text_msg)

        p = ProgressiveStatus(
            bot, chat_id=100, initial_text="Initial",
            stages=[(0.01, "Stage 1"), (0.02, "Stage 2")],
            gif_path=None,
        )
        await p.start()
        # Let the background stage runner finish instead of relying on a tiny
        # scheduler sleep window.
        await asyncio.wait_for(p._task, timeout=1.0)
        # Both stages edited the text in order.
        calls = text_msg.edit_text.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], "Stage 1")
        self.assertEqual(calls[1].args[0], "Stage 2")

    async def test_stop_cancels_pending_stages(self):
        text_msg = _make_text_msg()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=text_msg)

        p = ProgressiveStatus(
            bot, chat_id=100, initial_text="Init",
            stages=[(1.0, "Should not fire")],
            gif_path=None,
        )
        await p.start()
        await asyncio.sleep(0.01)
        # Stop before the 1-sec stage fires.
        await p.stop()
        # Wait past the original stage time.
        await asyncio.sleep(0.05)
        text_msg.edit_text.assert_not_awaited()

    async def test_stage_edit_failure_stops_updates(self):
        """If a stage edit fails (rate limit, deleted msg), the runner stops
        gracefully — no exception, no further stages attempted."""
        text_msg = _make_text_msg()
        text_msg.edit_text = AsyncMock(side_effect=RuntimeError("rate limit"))
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=text_msg)

        p = ProgressiveStatus(
            bot, chat_id=100, initial_text="Init",
            stages=[(0.01, "A"), (0.02, "B"), (0.03, "C")],
            gif_path=None,
        )
        await p.start()
        await asyncio.sleep(0.1)
        # Only the first attempt was made; subsequent stages skipped.
        self.assertEqual(text_msg.edit_text.await_count, 1)

    async def test_stages_unsorted_input_runs_in_order(self):
        """Caller can pass stages in any order — helper sorts them."""
        text_msg = _make_text_msg()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=text_msg)

        p = ProgressiveStatus(
            bot, chat_id=100, initial_text="Init",
            stages=[(0.03, "Third"), (0.01, "First"), (0.02, "Second")],
            gif_path=None,
        )
        await p.start()
        await asyncio.wait_for(p._task, timeout=0.2)
        calls = text_msg.edit_text.await_args_list
        self.assertEqual([c.args[0] for c in calls], ["First", "Second", "Third"])


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_deletes_gif(self):
        gif_msg = _make_gif_msg()
        text_msg = _make_text_msg()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=text_msg)

        p = ProgressiveStatus(bot, chat_id=100, initial_text="X", stages=[], gif_path=None)
        p.text_msg = text_msg
        p.gif_msg = gif_msg
        await p.stop()
        gif_msg.delete.assert_awaited_once()

    async def test_stop_is_idempotent(self):
        bot = MagicMock()
        text_msg = _make_text_msg()
        gif_msg = _make_gif_msg()
        p = ProgressiveStatus(bot, chat_id=100, initial_text="X", stages=[], gif_path=None)
        p.text_msg = text_msg
        p.gif_msg = gif_msg
        await p.stop()
        await p.stop()  # second call should not raise / no double-delete
        gif_msg.delete.assert_awaited_once()

    async def test_stop_swallows_gif_delete_errors(self):
        gif_msg = _make_gif_msg()
        gif_msg.delete = AsyncMock(side_effect=RuntimeError("already deleted"))
        bot = MagicMock()
        p = ProgressiveStatus(bot, chat_id=100, initial_text="X", stages=[], gif_path=None)
        p.gif_msg = gif_msg
        # Should not raise.
        await p.stop()


class StageConfigTests(unittest.TestCase):
    """Sanity checks on the default stage configurations."""

    def test_search_stages_has_two_escalations(self):
        stages = search_stages()
        self.assertEqual(len(stages), 2)
        # First escalation at t=10s, second at t=25s.
        self.assertEqual(stages[0][0], 10.0)
        self.assertEqual(stages[1][0], 25.0)
        # Text must mention activity and tell the user not to repeat-tap.
        self.assertIn("ищу", stages[0][1].lower())
        self.assertIn("повторно нажимать не нужно", stages[0][1].lower())
        self.assertIn("покажу понятную ошибку", stages[1][1].lower())

    def test_voice_stages_has_one_escalation(self):
        stages = voice_stages()
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0][0], 10.0)
        # Russian wording referencing spy theme («переслушиваем»).
        self.assertIn("переслушиваем", stages[0][1].lower())


class AssetPathTests(unittest.TestCase):
    def test_search_animation_points_to_repo_assets_dir(self):
        # Path resolution should land inside the repo's assets/ folder.
        self.assertTrue(str(SEARCH_ANIMATION_PATH).endswith("searching_cats.mp4"))
        self.assertIn("assets", str(SEARCH_ANIMATION_PATH))

    def test_voice_animation_points_to_repo_assets_dir(self):
        self.assertTrue(str(VOICE_ANIMATION_PATH).endswith("listening_spies.mp4"))
        self.assertIn("assets", str(VOICE_ANIMATION_PATH))


if __name__ == "__main__":
    unittest.main()
