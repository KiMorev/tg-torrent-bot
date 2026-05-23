"""Tests for the search-quality improvements when the query returns 0 results.

Covers four orthogonal protections that prevent confusing «возможно вы имели
в виду» suggestions:

  A) Multi-tracker coverage loss — if Jackett errored and the user had
     non-rutracker indexers selected, RT-direct fallback is incomplete and
     did-you-mean would be misleading. Show retry hint instead.

  B) KP-verify the user's ORIGINAL query — if they typed a real film that
     just happens to not be on trackers right now, don't insult them with
     typo-fixes; surface a «found on KP, retry later» message.

  C) KP-verify each GPT suggestion — drop hallucinated titles before the
     user sees them.

  D) Prompt updates: ordering (top-1 = most likely), existence check,
     description recognition («фильм где Гослинг ездит» → «Драйв»).
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


class IsRutrackerOnlyTests(unittest.TestCase):
    """_is_rutracker_only_indexer_set — multi-tracker coverage detection."""

    def _indexers(self) -> list[dict]:
        return [
            {"id": "rutracker", "name": "Rutracker"},
            {"id": "kinozal",   "name": "Kinozal"},
            {"id": "nnmclub",   "name": "NNMClub"},
        ]

    def test_none_selection_treated_as_rt_only(self):
        self.assertTrue(bot._is_rutracker_only_indexer_set(None, self._indexers()))

    def test_empty_selection_treated_as_rt_only(self):
        self.assertTrue(bot._is_rutracker_only_indexer_set(set(), self._indexers()))

    def test_rutracker_only_selection(self):
        self.assertTrue(bot._is_rutracker_only_indexer_set(
            {"rutracker"}, self._indexers(),
        ))

    def test_kinozal_alone_is_not_rt_only(self):
        self.assertFalse(bot._is_rutracker_only_indexer_set(
            {"kinozal"}, self._indexers(),
        ))

    def test_rutracker_plus_kinozal_is_not_rt_only(self):
        self.assertFalse(bot._is_rutracker_only_indexer_set(
            {"rutracker", "kinozal"}, self._indexers(),
        ))

    def test_case_insensitive_matching(self):
        self.assertTrue(bot._is_rutracker_only_indexer_set(
            {"RUTRACKER"}, [{"id": "RuTracker"}],
        ))


class KpVerifyTitleSyncTests(unittest.TestCase):
    """_kp_verify_title_sync — the existence check with default_on_unknown."""

    def setUp(self):
        # Make cache empty before each test so we don't leak state.
        bot._kp_exists_cache.clear()

    def test_returns_true_when_kp_has_match(self):
        kp = MagicMock()
        kp.search_movie.return_value = MagicMock()  # truthy = found
        with patch.object(bot, "kinopoisk_client", kp):
            self.assertTrue(bot._kp_verify_title_sync("Drive"))

    def test_returns_false_when_kp_has_no_match(self):
        kp = MagicMock()
        kp.search_movie.return_value = None
        with patch.object(bot, "kinopoisk_client", kp):
            self.assertFalse(bot._kp_verify_title_sync("BogusFakeMovie123"))

    def test_returns_default_when_kp_client_none_default_true(self):
        with patch.object(bot, "kinopoisk_client", None):
            # Default-true path: don't drop suggestions due to missing KP.
            self.assertTrue(bot._kp_verify_title_sync("X", default_on_unknown=True))

    def test_returns_default_when_kp_client_none_default_false(self):
        with patch.object(bot, "kinopoisk_client", None):
            # Default-false path: don't suppress did-you-mean on missing KP.
            self.assertFalse(bot._kp_verify_title_sync("X", default_on_unknown=False))

    def test_returns_default_on_kp_exception(self):
        kp = MagicMock()
        kp.search_movie.side_effect = RuntimeError("KP down")
        with patch.object(bot, "kinopoisk_client", kp):
            self.assertTrue(bot._kp_verify_title_sync("X", default_on_unknown=True))
            self.assertFalse(bot._kp_verify_title_sync("Y", default_on_unknown=False))

    def test_empty_title_always_false(self):
        with patch.object(bot, "kinopoisk_client", MagicMock()):
            self.assertFalse(bot._kp_verify_title_sync(""))
            self.assertFalse(bot._kp_verify_title_sync("   "))

    def test_cache_hit_skips_kp_call(self):
        kp = MagicMock()
        kp.search_movie.return_value = MagicMock()
        with patch.object(bot, "kinopoisk_client", kp):
            bot._kp_verify_title_sync("Drive")
            bot._kp_verify_title_sync("Drive")
            bot._kp_verify_title_sync("drive")  # case normalization
        # Only one KP call despite three lookups.
        self.assertEqual(kp.search_movie.call_count, 1)

    def test_cache_eviction_when_oversized(self):
        kp = MagicMock()
        kp.search_movie.return_value = MagicMock()
        with patch.object(bot, "kinopoisk_client", kp):
            for i in range(bot._KP_EXISTS_CACHE_MAX + 50):
                bot._kp_verify_title_sync(f"title{i}")
        # Cache stayed within bound.
        self.assertLessEqual(len(bot._kp_exists_cache), bot._KP_EXISTS_CACHE_MAX)


class KpVerifyTitlesParallelTests(unittest.IsolatedAsyncioTestCase):
    """_kp_verify_titles — parallel verification of multiple titles."""

    def setUp(self):
        bot._kp_exists_cache.clear()

    async def test_returns_per_title_results(self):
        kp = MagicMock()
        kp.search_movie.side_effect = lambda t: MagicMock() if t in {"Drive", "Snatch"} else None
        with patch.object(bot, "kinopoisk_client", kp):
            out = await bot._kp_verify_titles(["Drive", "FakeMovie", "Snatch"])
        self.assertEqual(out["Drive"], True)
        self.assertEqual(out["FakeMovie"], False)
        self.assertEqual(out["Snatch"], True)

    async def test_empty_list_returns_empty_dict(self):
        self.assertEqual(await bot._kp_verify_titles([]), {})

    async def test_per_title_exceptions_become_permissive_true(self):
        """When a single title's check explodes, treat as True (permissive)
        rather than failing the whole batch."""
        kp = MagicMock()
        def fake_search(t):
            if t == "broken":
                raise RuntimeError("explosion")
            return MagicMock() if t == "ok" else None
        kp.search_movie.side_effect = fake_search
        with patch.object(bot, "kinopoisk_client", kp):
            out = await bot._kp_verify_titles(["ok", "broken", "missing"])
        self.assertTrue(out["ok"])
        # «broken» raised — permissive True
        self.assertTrue(out["broken"])
        self.assertFalse(out["missing"])


class FullSearchFlowQualityProtectionTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end-ish checks on _run_search no-results path: which message
    text/keyboard does the user actually see for each failure mode?"""

    def setUp(self):
        bot._kp_exists_cache.clear()

    def _make_context(self, *, jackett_selected=None, jackett_indexers=None):
        ctx = MagicMock()
        ctx.user_data = {
            "srch_query": "Q",
            "srch_search_query": "Q",
            "srch_quality": "1080p",
        }
        if jackett_selected is not None:
            ctx.user_data["srch_jackett_selected"] = jackett_selected
        if jackett_indexers is not None:
            ctx.user_data["srch_jackett_indexers"] = jackett_indexers
        return ctx

    async def _run_search_capture_text(
        self, ctx, *, jackett_search_raises: Exception | None = None,
        rt_search_results=None,
    ):
        """Drive _run_search, return the final text shown to user."""
        message = MagicMock()
        message.message_id = 1
        message.chat_id = 100
        message.edit_text = AsyncMock(return_value=message)

        async def send_fn(text, **kw):
            message.edit_text(text, **kw)  # store first call
            return message

        if jackett_search_raises:
            mock_jackett = MagicMock(
                search=MagicMock(side_effect=jackett_search_raises),
                get_indexers=MagicMock(return_value=ctx.user_data.get(
                    "srch_jackett_indexers", [])),
            )
        else:
            mock_jackett = MagicMock(
                search=MagicMock(return_value=[]),
                get_indexers=MagicMock(return_value=ctx.user_data.get(
                    "srch_jackett_indexers", [])),
            )
        mock_rutracker = MagicMock(
            search=MagicMock(return_value=rt_search_results or []),
        )
        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", mock_rutracker),
            patch.object(bot, "_gpt_get_did_you_mean",
                         new=AsyncMock(return_value=["Suggestion A"])),
            patch.object(bot, "_kp_verify_title_sync", return_value=True),
            patch.object(bot, "_kp_verify_titles",
                         new=AsyncMock(return_value={"Suggestion A": True})),
        ):
            await bot._run_search(send_fn, ctx, "Some Title")

        # Extract text from the last edit_text call.
        args, kwargs = message.edit_text.call_args
        return args[0] if args else kwargs.get("text", "")

    async def test_multi_tracker_failure_shows_retry_hint(self):
        """Bug A: Jackett errored + user had Kinozal selected → suppress
        did-you-mean, show retry message."""
        from jackett import JackettError
        ctx = self._make_context(
            jackett_selected={"rutracker", "kinozal"},
            jackett_indexers=[
                {"id": "rutracker"}, {"id": "kinozal"},
            ],
        )
        text = await self._run_search_capture_text(
            ctx, jackett_search_raises=JackettError("connection refused"),
        )
        # Retry-hint branch — not the «возможно вы имели в виду» branch.
        self.assertIn("временный сбой", text.lower())
        self.assertNotIn("возможно вы имели в виду", text.lower())

    async def test_rutracker_only_failure_keeps_did_you_mean(self):
        """When the user's selection covers only rutracker (the default),
        a Jackett failure + empty RT-direct IS a definitive 0-result →
        did-you-mean should still fire."""
        from jackett import JackettError
        ctx = self._make_context(
            jackett_selected={"rutracker"},
            jackett_indexers=[{"id": "rutracker"}],
        )
        text = await self._run_search_capture_text(
            ctx, jackett_search_raises=JackettError("timeout"),
        )
        # «временный сбой» branch must NOT fire — coverage wasn't lost.
        self.assertNotIn("временный сбой", text.lower())

    async def test_original_query_on_kp_swaps_message(self):
        """Bug B: when KP confirms the user's query is a real film, show
        the «есть на КП, но в трекерах сейчас нет» branch instead of
        did-you-mean variations."""
        ctx = self._make_context(
            jackett_selected={"rutracker"},
            jackett_indexers=[{"id": "rutracker"}],
        )
        message = MagicMock()
        message.message_id = 1
        message.chat_id = 100
        message.edit_text = AsyncMock(return_value=message)

        async def send_fn(text, **kw):
            message.edit_text(text, **kw)
            return message

        mock_jackett = MagicMock(
            search=MagicMock(return_value=[]),
            get_indexers=MagicMock(return_value=[{"id": "rutracker"}]),
        )
        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", MagicMock()),
            patch.object(bot, "_gpt_get_did_you_mean",
                         new=AsyncMock(return_value=["Wrong Suggestion"])),
            patch.object(bot, "_kp_verify_title_sync", return_value=True),
            patch.object(bot, "_kp_verify_titles",
                         new=AsyncMock(return_value={"Wrong Suggestion": True})),
        ):
            await bot._run_search(send_fn, ctx, "Гангстерленд")

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        # «Found on KP» branch wording.
        self.assertIn("Кинопоиске", text)
        # Did-you-mean buttons NOT rendered (suggestions suppressed).
        keyboard = kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in keyboard for b in row]
        self.assertFalse(any("Wrong Suggestion" in lbl for lbl in labels))


class GptPromptOrderingTests(unittest.TestCase):
    """The prompt updates (D) are encoded as substrings the prompt MUST
    contain. Use string-level tests since we can't actually call OpenAI."""

    def _get_prompt(self):
        # Crude but robust: read the source line that defines `system_prompt`.
        import gpt_features
        import inspect
        src = inspect.getsource(gpt_features.did_you_mean)
        return src

    def test_ordering_rule_explicit_in_prompt(self):
        src = self._get_prompt()
        self.assertIn("MOST LIKELY", src)
        self.assertIn("index 0", src)

    def test_existence_rule_explicit_in_prompt(self):
        src = self._get_prompt()
        self.assertIn("does this film/series actually exist", src)
        self.assertIn("Kinopoisk", src)

    def test_description_recognition_in_prompt(self):
        src = self._get_prompt()
        self.assertIn("DESCRIPTION", src)
        self.assertIn("Райан Гослинг", src)  # canonical example

    def test_gangsterland_real_film_example(self):
        """Confirms the prompt teaches GPT not to invent fixes for «Гангстерленд»."""
        src = self._get_prompt()
        self.assertIn("Гангстерленд", src)


if __name__ == "__main__":
    unittest.main()
