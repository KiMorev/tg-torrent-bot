"""Progressive status messages with optional animated GIF.

Long-running operations (Jackett search ~5-40s, Whisper transcription ~5-30s)
benefit from progressive feedback so the user knows the bot is still working.
Without progress indication, a 30-second wait feels broken; with periodic
text edits + an animation, it feels intentional.

Public API:

  ProgressiveStatus(bot, chat_id, initial_text, stages, gif_path)
      .start() → sends initial text + (optional) gif, schedules stage updates
      .stop() → cancels the updater, deletes the gif, returns the text Message
                (so the caller can edit it with the final result)

Stages are a list of ``(delay_seconds, text)`` pairs. Each stage fires
sequentially; the updater stops naturally if all stages elapse without
``.stop()`` being called.

GIF is sent as a SEPARATE message (Telegram doesn't allow editing a text
message into an animation in-place). It's deleted on .stop() so the user
ends up with a single clean «result» message.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("tg_torrent_drop")


class ProgressiveStatus:
    """Lifecycle wrapper for the «long operation» loading UI.

    Designed to be cheap to construct and graceful on every failure mode —
    no exception from this helper should ever propagate to the calling
    handler. The worst case is no progressive update, which degrades to
    the existing single-message behaviour.
    """

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        *,
        initial_text: str,
        stages: list[tuple[float, str]],
        gif_path: Path | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.initial_text = initial_text
        # Sort by delay so consumers can pass stages in any order.
        self.stages = sorted(stages, key=lambda s: s[0])
        self.gif_path = gif_path
        self.reply_to_message_id = reply_to_message_id
        self.parse_mode = parse_mode
        self.text_msg: Any = None
        self.gif_msg: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> Any:
        """Send initial text + gif. Returns the text Message (for the
        caller to keep a handle on)."""
        try:
            self.text_msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=self.initial_text,
                reply_to_message_id=self.reply_to_message_id,
                parse_mode=self.parse_mode,
            )
        except Exception:
            logger.warning(
                "Progressive status: initial send failed for chat=%s",
                self.chat_id, exc_info=True,
            )
            return None

        if self.gif_path is not None and self.gif_path.exists():
            try:
                with open(self.gif_path, "rb") as fh:
                    self.gif_msg = await self.bot.send_animation(
                        chat_id=self.chat_id,
                        animation=fh,
                    )
            except Exception:
                logger.debug(
                    "Progressive status: gif send failed for %s — falling back to text only",
                    self.gif_path, exc_info=True,
                )
                self.gif_msg = None

        if self.stages:
            try:
                self._task = asyncio.create_task(self._run_stages())
            except RuntimeError:
                # No running loop (rare in tests) — skip the stage updates,
                # the initial text message still works.
                self._task = None

        return self.text_msg

    async def _run_stages(self) -> None:
        """Background loop: sleeps to each stage and edits the text.

        Stages are absolute-from-start times: e.g. [(10, "..."), (25, "...")]
        means «at t+10s show first text, at t+25s show second text».
        """
        try:
            elapsed = 0.0
            for delay, text in self.stages:
                wait = max(0.0, delay - elapsed)
                if wait:
                    await asyncio.sleep(wait)
                elapsed = delay
                if self._stopped:
                    return
                try:
                    if self.text_msg is not None:
                        await self.text_msg.edit_text(text, parse_mode=self.parse_mode)
                except Exception:
                    # Stop trying on the first failure (likely message deleted
                    # by caller or rate limit) — no point spamming Telegram.
                    logger.debug(
                        "Progressive status: stage edit failed at t=%ss — stopping updates",
                        delay, exc_info=True,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Progressive status: stage runner crashed", exc_info=True)

    async def stop(self) -> Any:
        """Cancel updater + delete the gif. Returns the text Message so the
        caller can edit it with the final result. Idempotent — safe to call
        multiple times.
        """
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.gif_msg is not None:
            try:
                await self.gif_msg.delete()
            except Exception:
                logger.debug("Progressive status: gif delete failed", exc_info=True)
            self.gif_msg = None
        return self.text_msg


# ─── Concrete stage configurations ────────────────────────────────────

# Asset paths. Files in assets/ get COPY'd into the Docker image at /app/assets/.
# Telegram's send_animation accepts both .mp4 and .gif transparently — we use
# MP4 because it's ~10× smaller than equivalent-quality GIF (Telegram displays
# both as inline animations, user-visible identically).
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SEARCH_ANIMATION_PATH = ASSETS_DIR / "searching_cats.mp4"
VOICE_ANIMATION_PATH = ASSETS_DIR / "listening_spies.mp4"


def search_stages() -> list[tuple[float, str]]:
    """Progressive stages for the «search in trackers» loading UI."""
    return [
        (10.0, "🔎 Очень активно ищем…"),
        (25.0, "⌛ Это занимает дольше обычного, ещё держимся…"),
    ]


def voice_stages() -> list[tuple[float, str]]:
    """Progressive stages for Whisper transcription."""
    return [
        (10.0, "🎧 Переслушиваем чтобы наверняка…"),
    ]
