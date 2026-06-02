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
from search_facts import SearchFact


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


class SearchFactCatalogGenerationTests(unittest.TestCase):
    def _catalog_text(self, count: int = 60) -> str:
        import json

        tags = ("cinema", "horror", "sci-fi", "fantasy", "animation", "comedy", "action", "series")
        return json.dumps(
            {
                "facts": [
                    {
                        "id": f"gpt:fact_{i}",
                        "text": f"короткий русский кинофакт номер {i} помогает ждать поиск без повторов.",
                        "tags": [tags[i % len(tags)]],
                    }
                    for i in range(count)
                ],
                "aliases": {f"alias_{i}": [tags[i % len(tags)]] for i in range(8)},
                "markers": {"generated_for": "search_waiting_facts"},
            },
            ensure_ascii=False,
        )

    def test_generate_search_fact_catalog_accepts_valid_json(self) -> None:
        usage_sink: list[dict] = []
        with patch.object(
            gpt_features,
            "chat_completion",
            return_value=(
                {"text": self._catalog_text(), "input_tokens": 10, "output_tokens": 20, "model": "gpt-4o-mini"},
                None,
            ),
        ):
            catalog, error = gpt_features.generate_search_fact_catalog(
                existing_facts=[SearchFact(id="local:1", text="старый факт", tags=("cinema",))],
                existing_aliases={"кино": ("cinema",)},
                api_key="key",
                usage_sink=usage_sink,
            )

        self.assertIsNone(error)
        self.assertIsNotNone(catalog)
        self.assertEqual(len(catalog["facts"]), 60)
        self.assertEqual(usage_sink[0]["input_tokens"], 10)

    def test_generate_search_fact_catalog_rejects_invalid_catalog(self) -> None:
        with patch.object(
            gpt_features,
            "chat_completion",
            return_value=(
                {"text": '{"facts":[],"aliases":{} }', "input_tokens": 10, "output_tokens": 20, "model": "gpt-4o-mini"},
                None,
            ),
        ):
            catalog, error = gpt_features.generate_search_fact_catalog(
                existing_facts=[],
                existing_aliases={},
                api_key="key",
            )

        self.assertIsNone(catalog)
        self.assertEqual(error, "invalid_catalog")


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


class SearchFailureAdviceTests(unittest.TestCase):
    def _fake_chat_response(self, payload: dict):
        import json as _json
        return ({
            "text": _json.dumps(payload),
            "input_tokens": 120, "output_tokens": 30, "model": "gpt-4o-mini",
        }, None)

    def test_returns_sanitized_advice(self):
        payload = {
            "reason": "tracker_scope",
            "message": "Похоже, сейчас смотрим не все трекеры.",
            "suggested_action": "expand_trackers",
            "suggested_queries": ["Дюна", "Dune"],
        }
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(payload),
        ):
            advice, err = gpt_features.diagnose_search_failure(
                query="Дюра",
                base_query="Дюра",
                can_expand_trackers=True,
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(advice["reason"], "tracker_scope")
        self.assertEqual(advice["suggested_action"], "expand_trackers")
        self.assertEqual(advice["suggested_queries"], ["Дюна", "Dune"])

    def test_rejects_unavailable_action(self):
        payload = {
            "reason": "tracker_scope",
            "message": "Нужен более широкий поиск.",
            "suggested_action": "expand_trackers",
            "suggested_queries": [],
        }
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(payload),
        ):
            advice, err = gpt_features.diagnose_search_failure(
                query="Дюна",
                can_expand_trackers=False,
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(advice["suggested_action"], "manual_search")

    def test_filters_duplicate_suggested_queries(self):
        payload = {
            "reason": "title_variant",
            "message": "Попробуйте другое написание.",
            "suggested_action": "try_original_title",
            "suggested_queries": ["Дюра", "Дюна", "Dune", "Дюна"],
        }
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(payload),
        ):
            advice, _err = gpt_features.diagnose_search_failure(
                query="Дюра",
                suggestions=["Dune"],
                api_key="sk-test",
            )
        self.assertEqual(advice["suggested_queries"], ["Дюна"])


class SeriesBulkCandidateAdviceTests(unittest.TestCase):
    def _fake_chat_response(self, payload: dict):
        import json as _json
        return ({
            "text": _json.dumps(payload),
            "input_tokens": 180, "output_tokens": 40, "model": "gpt-4o-mini",
        }, None)

    def test_returns_indexed_hints(self):
        payload = {
            "notes": [
                {"index": 1, "hint": "Совпали качество и Original, но озвучку стоит проверить."},
                {"index": 2, "hint": "Много сидов, но есть риск по субтитрам."},
            ]
        }
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(payload),
        ):
            hints, err = gpt_features.explain_series_bulk_candidates(
                series_title="Клиника",
                season=2,
                profile={"quality": "1080p"},
                candidates=[{"title": "S02 1080p"}, {"title": "S02 WEB-DL"}],
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(hints[0], "Совпали качество и Original, но озвучку стоит проверить.")
        self.assertEqual(hints[1], "Много сидов, но есть риск по субтитрам.")

    def test_ignores_out_of_range_notes(self):
        payload = {
            "notes": [
                {"index": 9, "hint": "не тот индекс"},
                {"index": 1, "hint": "Нормальный кандидат."},
            ]
        }
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(payload),
        ):
            hints, err = gpt_features.explain_series_bulk_candidates(
                series_title="Клиника",
                season=2,
                profile={},
                candidates=[{"title": "S02"}],
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(hints, {0: "Нормальный кандидат."})


class MovieNotificationReleaseChoiceTests(unittest.TestCase):
    def _fake_chat_response(self, payload: dict):
        import json as _json
        return ({
            "text": _json.dumps(payload),
            "input_tokens": 150, "output_tokens": 25, "model": "gpt-4o-mini",
        }, None)

    def test_accepts_confident_pick(self):
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response({
                "pick": 2,
                "confidence": 0.86,
                "reason": "ближе к предпочтениям качества",
            }),
        ):
            index, reason, err = gpt_features.choose_movie_notification_release(
                title="Дюна",
                year=2024,
                defaults={"quality": "4K"},
                candidates=[{"title": "1080p"}, {"title": "2160p"}],
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(index, 1)
        self.assertIn("качества", reason)

    def test_rejects_low_confidence_pick(self):
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response({
                "pick": 2,
                "confidence": 0.4,
                "reason": "не уверен",
            }),
        ):
            index, reason, err = gpt_features.choose_movie_notification_release(
                title="Дюна",
                year=2024,
                defaults={},
                candidates=[{"title": "A"}, {"title": "B"}],
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertIsNone(index)
        self.assertEqual(reason, "не уверен")


class ParseTorrentTitleTests(unittest.TestCase):
    """PR3: structured-metadata extraction from raw torrent titles."""

    def _fake_chat_response(self, payload: dict):
        import json as _json
        return ({
            "text": _json.dumps(payload),
            "input_tokens": 200, "output_tokens": 80, "model": "gpt-4o-mini",
        }, None)

    def test_returns_structured_meta_for_realistic_title(self):
        payload = {
            "quality": "2160p", "source": "UHD BDRemux",
            "hdr": "HDR10+/DV", "audio": "TrueHD 7.1 Atmos",
            "langs": ["RUS", "UKR", "ENG"],
            "release_group": "AMS", "edition": "Theatrical",
        }
        with patch.object(gpt_features, "chat_completion",
                          return_value=self._fake_chat_response(payload)):
            meta, err = gpt_features.parse_torrent_title(
                title="Dune.Part.Two.2024.2160p.UHD.BDRemux.HDR10+.DV.AMS",
                api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertEqual(meta["quality"], "2160p")
        self.assertEqual(meta["source"], "UHD BDRemux")
        self.assertEqual(meta["hdr"], "HDR10+/DV")
        self.assertEqual(meta["audio"], "TrueHD 7.1 Atmos")
        self.assertEqual(meta["langs"], ["RUS", "UKR", "ENG"])
        self.assertEqual(meta["release_group"], "AMS")
        self.assertEqual(meta["edition"], "Theatrical")

    def test_handles_minimal_title_with_mostly_nulls(self):
        """Short title without tech tokens → GPT returns mostly null fields."""
        payload = {
            "quality": None, "source": None, "hdr": None, "audio": None,
            "langs": [], "release_group": None, "edition": None,
        }
        with patch.object(gpt_features, "chat_completion",
                          return_value=self._fake_chat_response(payload)):
            meta, err = gpt_features.parse_torrent_title(
                title="Some movie", api_key="sk-test",
            )
        self.assertIsNone(err)
        self.assertIsNone(meta["quality"])
        self.assertEqual(meta["langs"], [])

    def test_empty_title_returns_empty_error(self):
        meta, err = gpt_features.parse_torrent_title(
            title="", api_key="sk-test",
        )
        self.assertIsNone(meta)
        self.assertEqual(err, "empty")

    def test_propagates_chat_error(self):
        with patch.object(gpt_features, "chat_completion",
                          return_value=(None, "quota_exceeded")):
            meta, err = gpt_features.parse_torrent_title(
                title="X 2024 1080p", api_key="sk-test",
            )
        self.assertIsNone(meta)
        self.assertEqual(err, "quota_exceeded")

    def test_normalizes_langs_uppercase(self):
        """langs in response may come lowercase or mixed; we standardize."""
        payload = {
            "quality": "1080p", "source": None, "hdr": None, "audio": None,
            "langs": ["rus", "Eng"], "release_group": None, "edition": None,
        }
        with patch.object(gpt_features, "chat_completion",
                          return_value=self._fake_chat_response(payload)):
            meta, _ = gpt_features.parse_torrent_title(
                title="X 1080p multi", api_key="sk-test",
            )
        self.assertEqual(meta["langs"], ["RUS", "ENG"])


class ExplainMovieCardTests(unittest.TestCase):
    """PR2: 1-line «why this film» explanation generator."""

    def _fake_chat_response(self, text: str):
        return ({
            "text": '{"text": "%s"}' % text,
            "input_tokens": 250, "output_tokens": 50, "model": "gpt-4o-mini",
        }, None)

    def test_returns_explanation_text(self):
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response("Sci-fi эпик Вильнёва — для фанатов оригинала"),
        ):
            text, err = gpt_features.explain_movie_card(
                title="Дюна: Часть вторая",
                year=2024,
                rating=8.4,
                genres=["sci-fi"],
                synopsis="Пол Атрейдес объединяется с Чани...",
                api_key="sk-test",
            )
        self.assertEqual(text, "Sci-fi эпик Вильнёва — для фанатов оригинала")
        self.assertIsNone(err)

    def test_empty_title_returns_empty_error(self):
        text, err = gpt_features.explain_movie_card(
            title="", year=2024, rating=None, genres=[], api_key="sk-test",
        )
        self.assertIsNone(text)
        self.assertEqual(err, "empty")

    def test_propagates_chat_error(self):
        with patch.object(
            gpt_features, "chat_completion", return_value=(None, "quota_exceeded"),
        ):
            text, err = gpt_features.explain_movie_card(
                title="X", year=2024, rating=7.0, genres=[], api_key="sk-test",
            )
        self.assertIsNone(text)
        self.assertEqual(err, "quota_exceeded")

    def test_truncates_overly_long_response(self):
        long_text = "Ы" * 200
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response(long_text),
        ):
            text, _err = gpt_features.explain_movie_card(
                title="X", year=2024, rating=7.0, genres=[], api_key="sk-test",
            )
        self.assertLessEqual(len(text), 130)
        self.assertTrue(text.endswith("…"))

    def test_works_without_synopsis(self):
        """Synopsis-less mode: prompt notes «нет — опирайся только на жанр и
        название», call still succeeds (just less specific result)."""
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_response("Атмосферный sci-fi для…"),
        ):
            text, _err = gpt_features.explain_movie_card(
                title="X", year=2024, rating=7.0,
                genres=["sci-fi"], synopsis="",
                api_key="sk-test",
            )
        self.assertEqual(text, "Атмосферный sci-fi для…")


class EnrichTop10WithExplanationsTests(unittest.TestCase):
    """bot._enrich_top10_with_explanations orchestrates KP synopsis fetch
    + GPT explanation generation for the top-10 of /new only."""

    def setUp(self):
        import bot
        self.bot = bot

    def _make_cache(self, n_cards: int, with_explanation: list[bool] = None):
        cards = []
        kp_cache = {}
        for i in range(n_cards):
            kp_id = 1000 + i
            cards.append({
                "title": f"Film {i}", "year": 2024, "rating": 7.5,
                "genres": ["drama"], "kp_id": kp_id,
            })
            entry = {"kp_id": kp_id, "title": f"Film {i}"}
            if with_explanation and i < len(with_explanation) and with_explanation[i]:
                entry["explanation"] = f"Cached explanation {i}"
                entry["synopsis"] = "Cached synopsis"
            kp_cache[f"film {i}|2024"] = entry
        return {"cards": cards, "kp_cache": kp_cache}

    def test_skips_when_gpt_disabled_branch_handled_in_caller(self):
        """The helper itself doesn't check GPT_ENABLED — caller does. So if
        we call it directly, it tries to generate. Verified by other tests
        that mock chat_completion."""
        # Smoke test: no cards → no work, no exceptions.
        import asyncio
        cache = {"cards": [], "kp_cache": {}}
        asyncio.run(self.bot._enrich_top10_with_explanations(cache))

    def test_only_processes_top10_not_all_30(self):
        """Cards 11-30 must NOT have GPT/KP calls made for them."""
        import asyncio
        cache = self._make_cache(30)

        explain_calls = []
        def fake_explain(*, title, **_kw):
            explain_calls.append(title)
            return ("OK explanation", None)

        with (
            patch.object(self.bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(self.bot, "GPT_MODEL", "gpt-4o-mini"),
            patch.object(self.bot, "kinopoisk_client",
                         MagicMock(get_film_synopsis=MagicMock(return_value="syn"))),
            patch.object(self.bot, "gpt_features_explain_movie_card", side_effect=fake_explain),
            patch.object(self.bot, "_gpt_record_usage"),
        ):
            asyncio.run(self.bot._enrich_top10_with_explanations(cache))

        # Only the first 10 cards' titles were used in GPT calls
        self.assertEqual(len(explain_calls), 10)
        self.assertEqual(explain_calls[0], "Film 0")
        self.assertEqual(explain_calls[-1], "Film 9")

    def test_reuses_cached_explanations(self):
        """If kp_cache entry already has `explanation`, no new GPT call."""
        import asyncio
        # All 10 top-10 cards already have cached explanations
        cache = self._make_cache(10, with_explanation=[True] * 10)

        explain_mock = MagicMock(return_value=("new", None))
        with (
            patch.object(self.bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(self.bot, "kinopoisk_client",
                         MagicMock(get_film_synopsis=MagicMock(return_value="syn"))),
            patch.object(self.bot, "gpt_features_explain_movie_card", explain_mock),
            patch.object(self.bot, "_gpt_record_usage"),
        ):
            asyncio.run(self.bot._enrich_top10_with_explanations(cache))

        # No new GPT calls
        explain_mock.assert_not_called()
        # Cached explanation attached to card
        self.assertEqual(cache["cards"][0]["explanation"], "Cached explanation 0")

    def test_graceful_fallback_on_gpt_error(self):
        """GPT timeout → card simply has no explanation, no exception."""
        import asyncio
        cache = self._make_cache(3)

        with (
            patch.object(self.bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(self.bot, "kinopoisk_client",
                         MagicMock(get_film_synopsis=MagicMock(return_value="syn"))),
            patch.object(self.bot, "gpt_features_explain_movie_card",
                         return_value=(None, "timeout")),
            patch.object(self.bot, "_gpt_record_usage"),
        ):
            asyncio.run(self.bot._enrich_top10_with_explanations(cache))

        # No card got an explanation, no crash
        for card in cache["cards"][:3]:
            self.assertNotIn("explanation", card)


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


class GptUsageSinkTests(unittest.TestCase):
    """Plumbing real {input_tokens, output_tokens, model} from chat_completion
    up through the feature wrappers to the usage tracker — replaces the prior
    hardcoded estimates in _gpt_record_usage calls."""

    def _fake_chat_ok(self, *, text='{"pick":1,"confidence":0.9,"reason":"ok"}',
                     in_tok=123, out_tok=45, model="gpt-4o-mini"):
        return ({
            "text": text,
            "input_tokens": in_tok, "output_tokens": out_tok, "model": model,
        }, None)

    def test_parse_torrent_title_records_real_usage_into_sink(self):
        fake = ({
            "text": '{"quality":"2160p","source":"BDRemux","hdr":null,'
                    '"audio":null,"langs":["RUS"],"release_group":null,'
                    '"edition":null}',
            "input_tokens": 187, "output_tokens": 42, "model": "gpt-4o-mini-2024-07-18",
        }, None)
        sink: list = []
        with patch.object(gpt_features, "chat_completion", return_value=fake):
            meta, err = gpt_features.parse_torrent_title(
                title="Dune.2024.2160p.BDRemux.RUS", api_key="sk-test",
                usage_sink=sink,
            )
        self.assertIsNone(err)
        self.assertEqual(meta["quality"], "2160p")
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["input_tokens"], 187)
        self.assertEqual(sink[0]["output_tokens"], 42)
        self.assertEqual(sink[0]["model"], "gpt-4o-mini-2024-07-18")

    def test_kp_confidence_check_records_usage_even_when_low_confidence(self):
        """Tokens are spent before we know the confidence is too low — must
        still be recorded so /admin shows the real cost."""
        sink: list = []
        with patch.object(
            gpt_features, "chat_completion",
            return_value=self._fake_chat_ok(
                text='{"pick":1,"confidence":0.3,"reason":"meh"}',
                in_tok=210, out_tok=18,
            ),
        ):
            idx, _conf, err = gpt_features.kp_confidence_check(
                query="X", candidates=[{"title_ru": "Y"}], api_key="sk-test",
                usage_sink=sink,
            )
        self.assertIsNone(idx)
        self.assertIsNone(err)
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["input_tokens"], 210)

    def test_usage_sink_empty_on_network_error(self):
        """No API call succeeded → no usage to record. Sink stays empty."""
        sink: list = []
        with patch.object(
            gpt_features, "chat_completion", return_value=(None, "timeout"),
        ):
            meta, err = gpt_features.parse_torrent_title(
                title="X", api_key="sk-test", usage_sink=sink,
            )
        self.assertIsNone(meta)
        self.assertEqual(err, "timeout")
        self.assertEqual(sink, [])

    def test_usage_sink_records_even_on_local_parse_failure(self):
        """API returned 200 + tokens, but JSON didn't parse — tokens were
        still spent, so /admin must count them."""
        fake = ({
            "text": "not-json-content",
            "input_tokens": 99, "output_tokens": 7, "model": "gpt-4o-mini",
        }, None)
        sink: list = []
        with patch.object(gpt_features, "chat_completion", return_value=fake):
            meta, err = gpt_features.parse_torrent_title(
                title="X", api_key="sk-test", usage_sink=sink,
            )
        self.assertIsNone(meta)
        self.assertEqual(err, "parse")
        # Real tokens must still be plumbed up so /admin shows true cost.
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["input_tokens"], 99)
        self.assertEqual(sink[0]["output_tokens"], 7)

    def test_did_you_mean_propagates_usage(self):
        import json as _json
        fake = ({
            "text": _json.dumps({"suggestions": ["Дюна"]}),
            "input_tokens": 55, "output_tokens": 12, "model": "gpt-4o-mini",
        }, None)
        sink: list = []
        with patch.object(gpt_features, "chat_completion", return_value=fake):
            sugs, err = gpt_features.did_you_mean(
                query="Дюра", api_key="sk-test", usage_sink=sink,
            )
        self.assertIsNone(err)
        self.assertEqual(sugs, ["Дюна"])
        self.assertEqual(sink[0]["input_tokens"], 55)


class EstimateCostUnknownModelTests(unittest.TestCase):
    def test_returns_none_for_unknown_model(self):
        self.assertIsNone(
            gpt_client.estimate_chat_cost_usd(100, 50, model="claude-3-haiku")
        )

    def test_prefix_match_for_dated_variant(self):
        # "gpt-4o-mini-2024-07-18" should resolve via prefix → use mini pricing,
        # not the more-expensive gpt-4o rate.
        cost = gpt_client.estimate_chat_cost_usd(
            1_000_000, 0, model="gpt-4o-mini-2024-07-18",
        )
        self.assertIsNotNone(cost)
        self.assertAlmostEqual(cost, 0.150, places=5)

    def test_longest_prefix_wins(self):
        # "gpt-4o-2024-08-06" must NOT match "gpt-4o-mini" — it's plain gpt-4o.
        cost = gpt_client.estimate_chat_cost_usd(
            1_000_000, 0, model="gpt-4o-2024-08-06",
        )
        self.assertAlmostEqual(cost, 2.500, places=5)


class GptRecordUsageRealVsEstimateTests(unittest.TestCase):
    """_gpt_record_usage must (a) prefer real usage when supplied,
    (b) handle unknown models by counting tokens but flagging cost-unknown."""

    def setUp(self):
        import bot
        self.bot = bot

    def _isolated_usage_io(self):
        """Return a (load, save) pair backed by a single shared dict so
        we can observe writes within a single test."""
        store: dict = {"data": {}}
        def load():
            import copy
            return copy.deepcopy(store["data"])
        def save(payload):
            import copy
            store["data"] = copy.deepcopy(payload)
        return store, load, save

    def test_real_usage_overrides_fallback_estimate(self):
        store, load, save = self._isolated_usage_io()
        with (
            patch.object(self.bot.state_store, "load_gpt_usage", side_effect=load),
            patch.object(self.bot.state_store, "save_gpt_usage", side_effect=save),
        ):
            self.bot._gpt_record_usage(
                feature="quality_parse",
                input_tokens=200, output_tokens=80,  # fallback
                error_label=None,
                usage={"input_tokens": 17, "output_tokens": 3, "model": "gpt-4o-mini"},
            )
        bucket = store["data"]["features"]["quality_parse"]
        # Real values stored, not fallback.
        self.assertEqual(bucket["input_tokens"], 17)
        self.assertEqual(bucket["output_tokens"], 3)
        self.assertEqual(bucket["real_usage_calls"], 1)
        self.assertEqual(bucket["estimate_calls"], 0)

    def test_fallback_used_when_no_usage_supplied(self):
        store, load, save = self._isolated_usage_io()
        with (
            patch.object(self.bot.state_store, "load_gpt_usage", side_effect=load),
            patch.object(self.bot.state_store, "save_gpt_usage", side_effect=save),
        ):
            self.bot._gpt_record_usage(
                feature="quality_parse",
                input_tokens=200, output_tokens=80,
                error_label="timeout",
                usage=None,
            )
        bucket = store["data"]["features"]["quality_parse"]
        self.assertEqual(bucket["input_tokens"], 200)
        self.assertEqual(bucket["output_tokens"], 80)
        self.assertEqual(bucket["estimate_calls"], 1)
        self.assertEqual(bucket["real_usage_calls"], 0)

    def test_unknown_model_records_tokens_but_flags_cost_unknown(self):
        store, load, save = self._isolated_usage_io()
        with (
            patch.object(self.bot.state_store, "load_gpt_usage", side_effect=load),
            patch.object(self.bot.state_store, "save_gpt_usage", side_effect=save),
        ):
            self.bot._gpt_record_usage(
                feature="kp_confidence",
                input_tokens=0, output_tokens=0,
                error_label=None,
                usage={"input_tokens": 50, "output_tokens": 10, "model": "claude-3-haiku"},
            )
        bucket = store["data"]["features"]["kp_confidence"]
        self.assertEqual(bucket["input_tokens"], 50)
        self.assertEqual(bucket["output_tokens"], 10)
        self.assertEqual(bucket["cost_unknown_calls"], 1)
        self.assertEqual(bucket["estimated_cost_usd"], 0.0)
        self.assertIn("claude-3-haiku", bucket["unknown_models"])

    def test_success_clears_previous_last_error(self):
        store, load, save = self._isolated_usage_io()
        store["data"] = {
            "month": "2026-05",
            "features": {},
            "last_error": {"ts": "2026-05-28T10:00:00+03:00", "feature": "explain_card", "type": "network"},
        }
        with (
            patch.object(self.bot.state_store, "load_gpt_usage", side_effect=load),
            patch.object(self.bot.state_store, "save_gpt_usage", side_effect=save),
        ):
            self.bot._gpt_record_usage(
                feature="explain_card",
                input_tokens=0, output_tokens=0,
                error_label=None,
                usage={"input_tokens": 50, "output_tokens": 10, "model": "gpt-4o-mini"},
            )
        self.assertNotIn("last_error", store["data"])


if __name__ == "__main__":
    unittest.main()
