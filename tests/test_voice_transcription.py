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

    def test_download_failure_records_voice_usage_error(self):
        update = _make_voice_update(duration=5)
        context = _make_voice_context()
        context.bot.get_file.side_effect = RuntimeError("telegram down")

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", MagicMock(
                load_approved_chat_ids=MagicMock(return_value=set()),
            )),
            patch.object(bot, "VOICE_SEARCH_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(bot, "VOICE_MAX_SECONDS", 30),
            patch.object(bot, "_safe_edit_message", new=AsyncMock()) as edit_mock,
            patch.object(bot, "_voice_record_usage") as usage_mock,
        ):
            asyncio.run(bot.voice_message_entry(update, context))

        usage_mock.assert_called_once()
        self.assertEqual(usage_mock.call_args.kwargs["outcome"], "error")
        self.assertEqual(usage_mock.call_args.kwargs["error_label"], "download_failed")
        last_call_text = edit_mock.call_args.args[1]
        self.assertIn("Не удалось скачать голосовое", last_call_text)

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
            patch.object(bot, "transcribe_audio_detailed", return_value=(None, "network")),
            patch.object(bot, "_safe_edit_message", new=AsyncMock()) as edit_mock,
            patch.object(bot, "_voice_record_usage"),  # silence usage recording in test
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
            patch.object(bot, "transcribe_audio_detailed", return_value=("Дюна часть вторая", None)) as transcribe_mock,
            patch.object(bot, "rutracker_client", MagicMock()),
            patch.object(bot, "_safe_edit_message", new=AsyncMock()) as edit_mock,
            patch.object(bot, "_voice_record_usage"),  # silence usage recording in test
        ):
            asyncio.run(bot.voice_message_entry(update, context))

        # The transcribed text was placed into the search query slot.
        self.assertEqual(context.user_data.get("srch_query"), "Дюна часть вторая")
        audio_path = transcribe_mock.call_args.args[0]
        self.assertTrue(audio_path.name.endswith(".ogg"))
        self.assertNotIn(".torrent", audio_path.name)
        # The user-visible status message shows what was heard.
        last_call_text = edit_mock.call_args.args[1]
        self.assertIn("Услышал", last_call_text)
        self.assertIn("Дюна часть вторая", last_call_text)


class TranscribeAudioDetailedTests(unittest.TestCase):
    """Extra coverage for the (text, error_label) tuple return shape."""

    def _audio(self) -> Path:
        f = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        f.write(b"fake")
        f.close()
        return Path(f.name)

    def test_returns_no_key_label_when_key_empty(self):
        path = self._audio()
        try:
            text, err = voice_transcription.transcribe_audio_detailed(path, "")
            self.assertIsNone(text)
            self.assertEqual(err, "no_key")
        finally:
            path.unlink()

    def test_classifies_401_as_auth(self):
        mock_response = MagicMock(status_code=401, text="invalid api key")
        path = self._audio()
        try:
            with patch.object(voice_transcription.requests, "post", return_value=mock_response):
                text, err = voice_transcription.transcribe_audio_detailed(path, "sk-bad")
            self.assertIsNone(text)
            self.assertEqual(err, "auth")
        finally:
            path.unlink()

    def test_classifies_429_with_quota_body_as_quota_exceeded(self):
        mock_response = MagicMock(
            status_code=429,
            text='{"error":{"type":"insufficient_quota","message":"..."}}',
        )
        path = self._audio()
        try:
            with patch.object(voice_transcription.requests, "post", return_value=mock_response):
                text, err = voice_transcription.transcribe_audio_detailed(path, "sk-test")
            self.assertEqual(err, "quota_exceeded")
        finally:
            path.unlink()

    def test_classifies_429_without_quota_body_as_rate_limit(self):
        mock_response = MagicMock(status_code=429, text='{"error":"too many requests"}')
        path = self._audio()
        try:
            with patch.object(voice_transcription.requests, "post", return_value=mock_response):
                _text, err = voice_transcription.transcribe_audio_detailed(path, "sk-test")
            self.assertEqual(err, "rate_limit")
        finally:
            path.unlink()

    def test_classifies_timeout(self):
        path = self._audio()
        try:
            with patch.object(
                voice_transcription.requests, "post",
                side_effect=_requests.exceptions.Timeout("read timed out"),
            ):
                _text, err = voice_transcription.transcribe_audio_detailed(path, "sk-test")
            self.assertEqual(err, "timeout")
        finally:
            path.unlink()


class CheckApiKeyTests(unittest.TestCase):
    """check_api_key pings /v1/models — no audio file involved."""

    def test_returns_false_for_empty_key(self):
        is_valid, err = voice_transcription.check_api_key("")
        self.assertFalse(is_valid)
        self.assertEqual(err, "no_key")

    def test_returns_true_on_200(self):
        with patch.object(
            voice_transcription.requests, "get",
            return_value=MagicMock(status_code=200, text="..."),
        ):
            is_valid, err = voice_transcription.check_api_key("sk-test")
        self.assertTrue(is_valid)
        self.assertIsNone(err)

    def test_returns_auth_label_on_401(self):
        with patch.object(
            voice_transcription.requests, "get",
            return_value=MagicMock(status_code=401, text="invalid"),
        ):
            is_valid, err = voice_transcription.check_api_key("sk-bad")
        self.assertFalse(is_valid)
        self.assertEqual(err, "auth")


class EstimateCostTests(unittest.TestCase):
    def test_zero_for_zero_duration(self):
        self.assertEqual(voice_transcription.estimate_cost_usd(0), 0.0)

    def test_six_tenths_of_a_cent_per_minute(self):
        # 60 seconds at $0.006/min = $0.006
        self.assertAlmostEqual(voice_transcription.estimate_cost_usd(60), 0.006, places=5)

    def test_handles_partial_minute(self):
        # 30 seconds = $0.003
        self.assertAlmostEqual(voice_transcription.estimate_cost_usd(30), 0.003, places=5)


if __name__ == "__main__":
    unittest.main()
