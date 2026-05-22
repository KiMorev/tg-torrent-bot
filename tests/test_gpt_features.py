"""Tests for the GPT-powered precision-improvement helpers.

Module-level: gpt_client.chat_completion error classification + JSON parsing.
Feature-level: kp_confidence_check + did_you_mean prompt handling, graceful
degradation on API failure or malformed responses.

Integration-level (bot.py): _gpt_validate_kp_match returns True when GPT
disabled / errors (graceful fallback), False on low confidence.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import requests as _requests
import gpt_client
import gpt_features


class ChatCompletionTests(unittest.TestCase):
    def test_returns_no_key_for_empty_api_key(self):
        result, err = gpt_client.chat_completion(messages=[], api_key="")
        self.assertIsNone(result)
        self.assertEqual(err, "no_key")

    def test_parses_successful_response(self):
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            "model": "gpt-4o-mini",
        }
        with patch.object(gpt_client.requests, "post", return_value=mock_response):
            result, err = gpt_client.chat_completion(
                messages=[{"role": "user", "content": "Hi"}],
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(result["text"], "Hello")
        self.assertEqual(result["input_tokens"], 5)
        self.assertEqual(result["output_tokens"], 1)

    def test_classifies_quota_exceeded_429(self):
        mock_response = MagicMock(status_code=429, text='"insufficient_quota"')
        with patch.object(gpt_client.requests, "post", return_value=mock_response):
            _result, err = gpt_client.chat_completion(messages=[], api_key="sk-test")
        self.assertEqual(err, "quota_exceeded")

    def test_classifies_auth_401(self):
        mock_response = MagicMock(status_code=401, text="invalid key")
        with patch.object(gpt_client.requests, "post", return_value=mock_response):
            _result, err = gpt_client.chat_completion(messages=[], api_key="sk-bad")
        self.assertEqual(err, "auth")

    def test_returns_timeout_label_on_timeout(self):
        with patch.object(
            gpt_client.requests, "post",
            side_effect=_requests.exceptions.Timeout("read timed out"),
        ):
            _result, err = gpt_client.chat_completion(messages=[], api_key="sk-test")
        self.assertEqual(err, "timeout")

    def test_returns_empty_label_when_model_returns_empty(self):
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "  "}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0},
        }
        with patch.object(gpt_client.requests, "post", return_value=mock_response):
            _result, err = gpt_client.chat_completion(messages=[], api_key="sk-test")
        self.assertEqual(err, "empty")


class EstimateCostTests(unittest.TestCase):
    def test_input_tokens_priced_at_input_rate(self):
        # 1M input tokens × $0.150 = $0.15
        self.assertAlmostEqual(
            gpt_client.estimate_chat_cost_usd(1_000_000, 0), 0.15, places=5,
        )

    def test_output_tokens_priced_at_output_rate(self):
        self.assertAlmostEqual(
            gpt_client.estimate_chat_cost_usd(0, 1_000_000), 0.60, places=5,
        )

    def test_combined(self):
        # 200 in + 50 out ≈ 0.00003 + 0.00003 ≈ $0.00006
        self.assertAlmostEqual(
            gpt_client.estimate_chat_cost_usd(200, 50), 0.00006, places=7,
        )


class KpConfidenceCheckTests(unittest.TestCase):
    """Verify the prompt → JSON → (idx, conf, err) decoding."""

    def _fake_chat_response(self, pick: int, confidence: float):
        return ({
            "text": '{"pick": %d, "confidence": %f, "reason": "ok"}' % (pick, confidence),
            "input_tokens": 200, "output_tokens": 30, "model": "gpt-4o-mini",
        }, None)

    def test_accepts_high_confidence_match(self):
        candidates = [{"title_ru": "Дюна", "title_en": "Dune", "year": 2024}]
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(1, 0.95),
        ):
            idx, conf, err = gpt_features.kp_confidence_check(
                query="Дюна 2024", candidates=candidates, api_key="sk-test",
            )
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(conf, 0.95, places=2)
        self.assertIsNone(err)

    def test_rejects_low_confidence(self):
        candidates = [{"title_ru": "Что-то", "year": 2024}]
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(1, 0.4),
        ):
            idx, _conf, err = gpt_features.kp_confidence_check(
                query="Дюна", candidates=candidates, api_key="sk-test",
            )
        self.assertIsNone(idx)
        self.assertIsNone(err)

    def test_rejects_pick_zero(self):
        candidates = [{"title_ru": "Не то", "year": 2024}]
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(0, 0.9),
        ):
            idx, _conf, err = gpt_features.kp_confidence_check(
                query="Дюна", candidates=candidates, api_key="sk-test",
            )
        self.assertIsNone(idx)
        self.assertIsNone(err)

    def test_returns_error_on_chat_failure(self):
        with patch.object(
            gpt_features, "chat_completion", return_value=(None, "timeout"),
        ):
            idx, _conf, err = gpt_features.kp_confidence_check(
                query="Дюна",
                candidates=[{"title_ru": "X"}],
                api_key="sk-test",
            )
        self.assertIsNone(idx)
        self.assertEqual(err, "timeout")

    def test_returns_empty_for_empty_candidates(self):
        idx, _conf, err = gpt_features.kp_confidence_check(
            query="X", candidates=[], api_key="sk-test",
        )
        self.assertIsNone(idx)
        self.assertEqual(err, "empty")


class DidYouMeanTests(unittest.TestCase):
    def _fake_chat_response(self, suggestions: list[str]):
        import json as _json
        return ({
            "text": _json.dumps({"suggestions": suggestions}),
            "input_tokens": 100, "output_tokens": 80, "model": "gpt-4o-mini",
        }, None)

    def test_returns_list_of_suggestions(self):
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(["Дюна", "Dune 2024", "Дюна часть вторая"]),
        ):
            suggestions, err = gpt_features.did_you_mean(query="Дюра", api_key="sk-test")
        self.assertEqual(suggestions, ["Дюна", "Dune 2024", "Дюна часть вторая"])
        self.assertIsNone(err)

    def test_truncates_to_max_suggestions(self):
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(["a", "b", "c", "d", "e"]),
        ):
            suggestions, _err = gpt_features.did_you_mean(
                query="x", api_key="sk-test", max_suggestions=2,
            )
        self.assertEqual(len(suggestions), 2)

    def test_filters_empty_strings(self):
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(["", "  ", "Дюна"]),
        ):
            suggestions, _err = gpt_features.did_you_mean(query="x", api_key="sk-test")
        self.assertEqual(suggestions, ["Дюна"])

    def test_returns_empty_on_chat_error(self):
        with patch.object(
            gpt_features, "chat_completion", return_value=(None, "network"),
        ):
            suggestions, err = gpt_features.did_you_mean(query="x", api_key="sk-test")
        self.assertEqual(suggestions, [])
        self.assertEqual(err, "network")


class GptValidateKpMatchTests(unittest.TestCase):
    """bot.py wrapper around kp_confidence_check — graceful behaviour matters."""

    def setUp(self):
        import bot
        self.bot = bot
        # Lightweight KinopoiskMovieMatch surrogate
        self.match = MagicMock()
        self.match.title_ru = "Дюна"
        self.match.title_en = "Dune"
        self.match.year = 2024
        self.match.rating = 8.4
        self.match.genres = ["sci-fi"]

    def test_accepts_match_when_gpt_disabled(self):
        with patch.object(self.bot, "GPT_ENABLED", False):
            self.assertTrue(self.bot._gpt_validate_kp_match("Дюна 2024", self.match))

    def test_accepts_match_when_gpt_errors_out(self):
        """If OpenAI is unreachable, keep the original behaviour (use match)
        rather than silently drop all KP enrichment from /new."""
        with (
            patch.object(self.bot, "GPT_ENABLED", True),
            patch.object(self.bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(
                self.bot, "gpt_kp_confidence_check",
                return_value=(None, 0.0, "timeout"),
            ),
            patch.object(self.bot, "_gpt_record_usage"),
        ):
            self.assertTrue(self.bot._gpt_validate_kp_match("Дюна 2024", self.match))

    def test_rejects_when_gpt_says_no_match(self):
        with (
            patch.object(self.bot, "GPT_ENABLED", True),
            patch.object(self.bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(
                self.bot, "gpt_kp_confidence_check",
                return_value=(None, 0.3, None),  # low confidence, no error
            ),
            patch.object(self.bot, "_gpt_record_usage"),
        ):
            self.assertFalse(self.bot._gpt_validate_kp_match("Аркейн", self.match))

    def test_accepts_when_gpt_picks_match(self):
        with (
            patch.object(self.bot, "GPT_ENABLED", True),
            patch.object(self.bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(
                self.bot, "gpt_kp_confidence_check",
                return_value=(0, 0.95, None),
            ),
            patch.object(self.bot, "_gpt_record_usage"),
        ):
            self.assertTrue(self.bot._gpt_validate_kp_match("Дюна 2024", self.match))


if __name__ == "__main__":
    unittest.main()
