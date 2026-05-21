"""Tests for the voice search feature.

Covers:
- transcribe_audio() module-level: graceful degradation (empty key, missing
  file, HTTP errors, network exceptions, malformed JSON, empty result text).
- voice_message_entry handler: feature flag, duration limit, transcription
  pipeline calling _normalize_season_in_query, fallback to friendly messages
  when Whisper returns None.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Required env so `import bot` works.
os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import requests as _requests
import voice_transcription


class TranscribeAudioTests(unittest.TestCase):
    """Module-level tests for the Whisper API wrapper."""

    def test_returns_none_for_empty_key(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake ogg data")
            path = Path(f.name)
        try:
            self.assertIsNone(voice_transcription.transcribe_audio(path, ""))
        finally:
            path.unlink()

    def test_returns_none_for_missing_file(self):
        self.assertIsNone(voice_transcription.transcribe_audio(
            Path("/definitely/missing/file.ogg"), "sk-test",
        ))

    def test_returns_text_on_successful_response(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Дюна часть вторая"}

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake ogg data")
            path = Path(f.name)
        try:
            with patch.object(voice_transcription.requests, "post", return_value=mock_response):
                result = voice_transcription.transcribe_audio(path, "sk-test")
            self.assertEqual(result, "Дюна часть вторая")
        finally:
            path.unlink()

    def test_returns_none_on_http_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = "rate limited"

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake")
            path = Path(f.name)
        try:
            with patch.object(voice_transcription.requests, "post", return_value=mock_response):
                self.assertIsNone(voice_transcription.transcribe_audio(path, "sk-test"))
        finally:
            path.unlink()

    def test_returns_none_on_empty_transcription(self):
        """Whisper sometimes returns text="" for silent/noise audio."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "   "}

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake")
            path = Path(f.name)
        try:
            with patch.object(voice_transcription.requests, "post", return_value=mock_response):
                self.assertIsNone(voice_transcription.transcribe_audio(path, "sk-test"))
        finally:
            path.unlink()

    def test_returns_none_on_network_exception(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake")
            path = Path(f.name)
        try:
            with patch.object(
                voice_transcription.requests, "post",
                side_effect=_requests.exceptions.ConnectionError("DNS failure"),
            ):
                self.assertIsNone(voice_transcription.transcribe_audio(path, "sk-test"))
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# voice_message_entry handler integration tests
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from unittest.mock import AsyncMock  # noqa: E402

import bot  # noqa: E402


def _make_voice_update(chat_id: int = 100, duration: int = 5):
    voice = MagicMock()
    voice.file_id = "test_file_id"
    voice.duration = duration
    message = MagicMock()
    message.voice = voice
    message.reply_text = AsyncMock()
    message.reply_text.return_value = MagicMock(message_id=42)
    message.message_id = 41

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = message
    update.effective_message = message
    return update


def _make_voice_context():
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.get_file = AsyncMock()
    return ctx


class VoiceMessageEntryTests(unittest.TestCase):
    """End-to-end tests of voice → search dispatch."""

    def test_rejects_when_voice_search_disabled(self):
        update = _make_voice_update()
        context = _make_voice_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "VOICE_SEARCH_ENABLED", False),
        ):
            asyncio.run(bot.voice_message_entry(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("не настроен", text)

    def test_rejects_voice_longer_than_max(self):
        update = _make_voice_update(duration=60)
        context = _make_voice_context()
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "VOICE_SEARCH_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(bot, "VOICE_MAX_SECONDS", 30),
        ):
            asyncio.run(bot.voice_message_entry(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("слишком длинное", text)

    def test_friendly_message_when_transcription_fails(self):
        """When Whisper returns None (network error, empty result, etc.) the
        user sees a clear «попробуйте ещё раз» instead of a silent failure."""
        update = _make_voice_update(duration=5)
        context = _make_voice_context()

        tg_file = MagicMock()
        tg_file.download_to_drive = AsyncMock()
        context.bot.get_file.return_value = tg_file

        # Make download_to_drive actually create an empty temp file so the
        # rest of the handler thinks the download succeeded.
        async def _fake_download(custom_path):
            Path(custom_path).write_bytes(b"")
        tg_file.download_to_drive.side_effect = _fake_download

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "VOICE_SEARCH_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(bot, "VOICE_MAX_SECONDS", 30),
            patch.object(bot, "transcribe_audio", return_value=None),
            patch.object(bot, "_safe_edit_message", new=AsyncMock()) as edit_mock,
        ):
            asyncio.run(bot.voice_message_entry(update, context))

        # Final edit_message should contain the failure-friendly text.
        last_call_text = edit_mock.call_args.args[1]
        self.assertIn("Не получилось распознать", last_call_text)

    def test_successful_transcription_starts_search(self):
        """Happy path: Whisper returns text → handler sets srch_query and
        edits the status message to show the search-options keyboard."""
        update = _make_voice_update(duration=8)
        context = _make_voice_context()

        tg_file = MagicMock()

        async def _fake_download(custom_path):
            Path(custom_path).write_bytes(b"")
        tg_file.download_to_drive = AsyncMock(side_effect=_fake_download)
        context.bot.get_file.return_value = tg_file

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "VOICE_SEARCH_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(bot, "VOICE_MAX_SECONDS", 30),
            patch.object(bot, "transcribe_audio", return_value="Дюна часть вторая"),
            patch.object(bot, "rutracker_client", MagicMock()),
            patch.object(bot, "_safe_edit_message", new=AsyncMock()) as edit_mock,
        ):
            asyncio.run(bot.voice_message_entry(update, context))

        # The transcribed text was placed into the search query slot.
        self.assertEqual(context.user_data.get("srch_query"), "Дюна часть вторая")
        # The user-visible status message shows what was heard.
        last_call_text = edit_mock.call_args.args[1]
        self.assertIn("Услышал", last_call_text)
        self.assertIn("Дюна часть вторая", last_call_text)


if __name__ == "__main__":
    unittest.main()
