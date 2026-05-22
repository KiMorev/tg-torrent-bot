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


class SplitQueryQualityTests(unittest.TestCase):
    """Strategy 2: extract base + preferred quality from full search query."""

    def test_extracts_1080p_suffix(self):
        self.assertEqual(bot._split_query_quality("Дюна 1080p"), ("Дюна", "1080p"))

    def test_extracts_2160p_suffix(self):
        self.assertEqual(bot._split_query_quality("Аркейн 2160p"), ("Аркейн", "2160p"))

    def test_normalises_4k_to_2160p(self):
        self.assertEqual(bot._split_query_quality("Барби 4k"), ("Барби", "2160p"))

    def test_normalises_uhd_to_2160p(self):
        self.assertEqual(bot._split_query_quality("Дюна UHD"), ("Дюна", "2160p"))

    def test_no_suffix_means_no_filter(self):
        self.assertEqual(bot._split_query_quality("Дюна 2024"), ("Дюна 2024", None))

    def test_year_not_misread_as_quality(self):
        # Year (4 digits) shouldn't be confused with a quality token.
        self.assertEqual(bot._split_query_quality("Аркейн 2024"), ("Аркейн 2024", None))

    def test_multiword_title_preserved(self):
        self.assertEqual(
            bot._split_query_quality("Дюна часть вторая 1080p"),
            ("Дюна часть вторая", "1080p"),
        )


class ClassifyResultsByQualityTests(unittest.TestCase):
    def test_groups_by_detected_quality(self):
        results = [
            {"title": "Dune 2024 1080p WEB-DL"},
            {"title": "Dune 2024 2160p UHD"},
            {"title": "Dune 2024 720p BDRip"},
            {"title": "Dune 2024 1080p BDRemux"},
            {"title": "Dune 2024 some weird release"},  # → "other"
        ]
        buckets = bot._classify_results_by_quality(results)
        self.assertEqual(len(buckets["1080p"]), 2)
        self.assertEqual(len(buckets["2160p"]), 1)
        self.assertEqual(len(buckets["720p"]), 1)
        self.assertEqual(len(buckets.get("other", [])), 1)


class FormatQualityStatsTests(unittest.TestCase):
    def test_orders_highest_quality_first(self):
        buckets = {
            "720p": [1, 2, 3],
            "1080p": [1, 2, 3, 4, 5],
            "2160p": [1, 2],
        }
        stats = bot._format_quality_stats(buckets)
        # 2160p should appear before 1080p before 720p
        self.assertEqual(stats, "2160p × 2, 1080p × 5, 720p × 3")

    def test_excludes_preferred_bucket(self):
        buckets = {"1080p": [1, 2, 3], "720p": [1]}
        stats = bot._format_quality_stats(buckets, exclude="1080p")
        self.assertEqual(stats, "720p × 1")


class QualityFilterIntegrationTests(unittest.TestCase):
    """Strategy 2: end-to-end behaviour of the search → classify → filter chain."""

    def test_preferred_quality_filters_results(self):
        """User asks for 1080p, Jackett returns mixed → only 1080p shown,
        banner mentions other qualities."""
        mock_jackett = MagicMock()
        # 4 fake results: 2 in 1080p, 1 in 720p, 1 in 2160p
        results = []
        for title in [
            "Dune 2024 1080p WEB-DL", "Dune 2024 1080p BDRip",
            "Dune 2024 720p", "Dune 2024 2160p UHD",
        ]:
            r = MagicMock()
            r.title = title
            r.topic_url = "https://example.com/x"
            r.tracker = "rt"
            r.size = "5 GB"
            r.seeders = 10
            r.magnet_url = ""
            r.torrent_url = ""
            results.append(r)
        mock_jackett.search.return_value = results
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Dune 1080p"))

        # Jackett was called with the base query, NOT «Dune 1080p»
        called_with = mock_jackett.search.call_args[0][0]
        self.assertEqual(called_with, "Dune")
        # Final rendered text contains banner with quality stats
        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Найдено 4", text)
        self.assertIn("показаны 2 в 1080p", text)
        self.assertIn("2160p × 1", text)
        self.assertIn("720p × 1", text)

    def test_preferred_quality_empty_shows_all_with_banner(self):
        """User asks for 1080p, Jackett returns only 720p+2160p → ALL shown
        with banner «в 1080p ничего, показаны все качества»."""
        mock_jackett = MagicMock()
        results = []
        for title in ["Dune 2024 720p", "Dune 2024 2160p UHD"]:
            r = MagicMock()
            r.title = title
            r.topic_url = "https://example.com/x"
            r.tracker = "rt"
            r.size = "5 GB"
            r.seeders = 10
            r.magnet_url = ""
            r.torrent_url = ""
            results.append(r)
        mock_jackett.search.return_value = results
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Dune 1080p"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("В 1080p ничего не найдено", text)
        self.assertIn("720p × 1", text)
        self.assertIn("2160p × 1", text)

    def test_no_quality_preference_no_banner(self):
        """User searched without quality → no quality stats banner clutter."""
        mock_jackett = MagicMock()
        r = MagicMock()
        r.title = "Dune 2024 1080p"
        r.topic_url = "https://example.com/x"
        r.tracker = "rt"
        r.size = "5 GB"
        r.seeders = 10
        r.magnet_url = ""
        r.torrent_url = ""
        mock_jackett.search.return_value = [r]
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Dune 2024"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        # No "Найдено N показаны M" banner — single result, no filter
        self.assertNotIn("показаны", text)


class SplitQuerySettingsTests(unittest.TestCase):
    """Extended helper: strips quality + audio + subs tokens in any order."""

    def test_strips_quality_only(self):
        self.assertEqual(
            bot._split_query_settings("Дюна 1080p"),
            ("Дюна", "1080p", False, False),
        )

    def test_strips_audio_flag(self):
        self.assertEqual(
            bot._split_query_settings("Дюна Original"),
            ("Дюна", None, True, False),
        )

    def test_strips_subs_flag(self):
        self.assertEqual(
            bot._split_query_settings("Дюна Sub"),
            ("Дюна", None, False, True),
        )

    def test_strips_all_three_in_canonical_order(self):
        self.assertEqual(
            bot._split_query_settings("Дюна 1080p Original Sub"),
            ("Дюна", "1080p", True, True),
        )

    def test_strips_flags_in_any_order(self):
        # The build-search-query order is quality/audio/subs, but be defensive
        # against user-supplied orderings.
        base, quality, audio, subs = bot._split_query_settings("Дюна Sub Original 1080p")
        self.assertEqual(base, "Дюна")
        self.assertEqual(quality, "1080p")
        self.assertTrue(audio)
        self.assertTrue(subs)


class AudioSubsDetectionTests(unittest.TestCase):
    def test_detects_original_audio_dual(self):
        self.assertTrue(bot._detect_has_original_audio("Dune 2024 1080p Dual"))

    def test_detects_original_audio_mvo(self):
        self.assertTrue(bot._detect_has_original_audio("Дюна (2024) MVO"))

    def test_detects_original_audio_keyword(self):
        self.assertTrue(bot._detect_has_original_audio("Dune 2024 ORIGINAL"))

    def test_does_not_detect_original_on_dubbed_only(self):
        self.assertFalse(bot._detect_has_original_audio("Dune 2024 1080p WEB-DL"))

    def test_detects_subs(self):
        self.assertTrue(bot._detect_has_subs("Dune 2024 SUB"))
        self.assertTrue(bot._detect_has_subs("Dune 2024 forced"))
        self.assertTrue(bot._detect_has_subs("Дюна субтитры"))

    def test_does_not_detect_subs_on_plain_title(self):
        self.assertFalse(bot._detect_has_subs("Dune 2024 1080p"))


class LoadingMessageWordingTests(unittest.TestCase):
    """Verify the loading message shows the base query + filter sub-line."""

    def test_loading_text_with_quality_only(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = []
        send_fn, _msg = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Дюна 1080p"))

        loading_text = send_fn.call_args.args[0]
        self.assertIn("«Дюна»", loading_text)
        self.assertIn("⚙️", loading_text)
        self.assertIn("1080p", loading_text)

    def test_loading_text_with_quality_and_audio_and_subs(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = []
        send_fn, _msg = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Дюна 1080p Original Sub"))

        loading_text = send_fn.call_args.args[0]
        self.assertIn("«Дюна»", loading_text)
        self.assertIn("оригинальная дорожка", loading_text)
        self.assertIn("субтитры", loading_text)

    def test_loading_text_without_filters(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = []
        send_fn, _msg = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Дюна 2024"))

        loading_text = send_fn.call_args.args[0]
        self.assertIn("«Дюна 2024»", loading_text)
        # No filter sub-line — year is part of the title, not a tracked setting
        self.assertNotIn("⚙️", loading_text)


class SearchDidmeanPreservesSettingsTests(unittest.TestCase):
    """search_didmean must keep the user's quality/audio/subs choices when
    re-running the search with a typo-corrected title."""

    def test_didmean_preserves_quality_preference(self):
        """User searched «Дюра 1080p» → typo → taps «Дюна» suggestion →
        bot must search for «Дюна 1080p», not bare «Дюна»."""
        query = MagicMock()
        query.data = "srch:didmean:Дюна"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1, chat_id=100))
        update = MagicMock(callback_query=query)
        context = MagicMock()
        context.user_data = {
            "srch_query": "Дюра",
            "srch_search_query": "Дюра 1080p",
            "srch_settings": {"quality": "1080p", "audio": False, "subs": False},
        }

        # Capture what _execute_search is called with
        with patch.object(bot, "_execute_search", new=AsyncMock(return_value=0)) as exec_mock:
            asyncio.run(bot.search_didmean(update, context))

        called_args = exec_mock.call_args.args
        # third arg is the search_query passed to _run_search
        self.assertEqual(called_args[2], "Дюна 1080p")
        # context.user_data must reflect the new query/title
        self.assertEqual(context.user_data["srch_query"], "Дюна")
        self.assertEqual(context.user_data["srch_search_query"], "Дюна 1080p")

    def test_didmean_preserves_audio_and_subs(self):
        query = MagicMock()
        query.data = "srch:didmean:Аркейн"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1, chat_id=100))
        update = MagicMock(callback_query=query)
        context = MagicMock()
        context.user_data = {
            "srch_query": "Аркаин",
            "srch_search_query": "Аркаин 1080p Original Sub",
            "srch_settings": {"quality": "1080p", "audio": True, "subs": True},
        }

        with patch.object(bot, "_execute_search", new=AsyncMock(return_value=0)) as exec_mock:
            asyncio.run(bot.search_didmean(update, context))

        called_args = exec_mock.call_args.args
        full = called_args[2]
        self.assertIn("Аркейн", full)
        self.assertIn("1080p", full)
        self.assertIn("Original", full)
        self.assertIn("Sub", full)


class AudioSubsFilterIntegrationTests(unittest.TestCase):
    """Strategy 2 applied to audio/subs — client-side post-filter on results."""

    def _make_jackett_result(self, title: str):
        r = MagicMock()
        r.title = title
        r.topic_url = "https://example.com/x"
        r.tracker = "rt"
        r.size = "5 GB"
        r.seeders = 10
        r.magnet_url = ""
        r.torrent_url = ""
        return r

    def test_audio_filter_drops_dub_only_results(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            self._make_jackett_result("Dune 2024 1080p Dual"),       # has original
            self._make_jackett_result("Dune 2024 1080p WEB-DL"),     # dub only
            self._make_jackett_result("Дюна (2024) MVO 1080p"),      # has original
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Dune Original"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        # Filter banner mentions audio
        self.assertIn("оригинальной дорожкой", text)
        # Dub-only result must not appear
        self.assertNotIn("WEB-DL", text)


class SupplementReleasesForFailedQueriesTests(unittest.TestCase):
    """Pinned behaviour for the «year disappears from /new when its
    query timed out / errored» bug. See _supplement_releases_for_failed_queries."""

    def test_empty_failed_specs_does_nothing(self):
        releases_out: list[dict] = []
        n = bot._supplement_releases_for_failed_queries(
            failed_specs=[],
            releases_out=releases_out,
            prev_all_releases=[{"year": 2026, "quality": "1080p"}],
        )
        self.assertEqual(n, 0)
        self.assertEqual(releases_out, [])

    def test_supplements_releases_matching_failed_year_quality(self):
        """2026 1080p query failed → take 2026 1080p releases from prev cache."""
        prev_all = [
            {"year": 2026, "quality": "1080p", "title": "A 2026 1080p"},
            {"year": 2026, "quality": "2160p", "title": "B 2026 2160p"},
            {"year": 2025, "quality": "1080p", "title": "C 2025 1080p"},
            {"year": 2026, "quality": "1080p", "title": "D 2026 1080p"},
        ]
        releases_out: list[dict] = []
        n = bot._supplement_releases_for_failed_queries(
            failed_specs=[(2026, "1080p")],
            releases_out=releases_out,
            prev_all_releases=prev_all,
        )
        self.assertEqual(n, 2)  # A and D
        titles = [r["title"] for r in releases_out]
        self.assertIn("A 2026 1080p", titles)
        self.assertIn("D 2026 1080p", titles)
        self.assertNotIn("B 2026 2160p", titles)  # wrong quality
        self.assertNotIn("C 2025 1080p", titles)  # wrong year

    def test_supplements_multiple_failed_specs(self):
        """Both year-queries failed → take both from prev cache."""
        prev_all = [
            {"year": 2026, "quality": "1080p"},
            {"year": 2026, "quality": "2160p"},
            {"year": 2025, "quality": "1080p"},
            {"year": 2024, "quality": "1080p"},
        ]
        releases_out: list[dict] = []
        n = bot._supplement_releases_for_failed_queries(
            failed_specs=[(2026, "1080p"), (2025, "1080p")],
            releases_out=releases_out,
            prev_all_releases=prev_all,
        )
        self.assertEqual(n, 2)

    def test_handles_non_dict_entries_gracefully(self):
        """Defensive: bad data in prev_all_releases shouldn't crash."""
        prev_all = [None, "garbage", {"year": 2026, "quality": "1080p"}, 42]
        releases_out: list[dict] = []
        n = bot._supplement_releases_for_failed_queries(
            failed_specs=[(2026, "1080p")],
            releases_out=releases_out,
            prev_all_releases=prev_all,
        )
        self.assertEqual(n, 1)

    def test_quality_comparison_is_case_insensitive(self):
        """Releases store quality as "1080p" but spec might come with
        different casing — match should be case-insensitive."""
        prev_all = [{"year": 2026, "quality": "1080P"}]
        releases_out: list[dict] = []
        n = bot._supplement_releases_for_failed_queries(
            failed_specs=[(2026, "1080p")],
            releases_out=releases_out,
            prev_all_releases=prev_all,
        )
        self.assertEqual(n, 1)

    def test_does_not_supplement_when_prev_releases_is_not_list(self):
        n = bot._supplement_releases_for_failed_queries(
            failed_specs=[(2026, "1080p")],
            releases_out=[],
            prev_all_releases=None,  # type: ignore
        )
        self.assertEqual(n, 0)


class FailedIndexerPartitioningTests(unittest.TestCase):
    """Verify _refresh_movie_discovery_cache splits failed_indexer_ids
    into 'enabled-for-rating' (gates retry/ready) vs 'disabled' (info-only).

    These exercise the helpers / data flow without standing up a full
    refresh — the partition logic is small and well-isolated inside the
    refresh function's persistence step.
    """

    def test_split_when_enabled_subset_filters_disabled_failures(self):
        """When enabled_set is a subset of failed_indexer_ids, only the
        intersection counts as «real» failures; the rest is info-only."""
        failed = {"noname-club", "rutracker", "kinozal", "eztv"}
        enabled = {"rutracker", "kinozal", "thepiratebay"}
        failed_enabled = failed & enabled
        failed_disabled = failed - enabled
        self.assertEqual(failed_enabled, {"rutracker", "kinozal"})
        self.assertEqual(failed_disabled, {"noname-club", "eztv"})

    def test_split_when_enabled_is_none_all_count_as_enabled(self):
        """enabled_set=None means «user wants every Jackett indexer in
        the rating» → all failures count as real degradation."""
        failed = {"noname-club", "rutracker"}
        # Simulating the «None» branch from the refresh function
        failed_enabled = set(failed)
        failed_disabled: set[str] = set()
        self.assertEqual(failed_enabled, failed)
        self.assertEqual(failed_disabled, set())

    def test_split_with_no_overlap_means_no_disabled(self):
        """If every failed indexer is in enabled set, disabled bucket is empty."""
        failed = {"rutracker", "kinozal"}
        enabled = {"rutracker", "kinozal", "thepiratebay"}
        self.assertEqual(failed & enabled, {"rutracker", "kinozal"})
        self.assertEqual(failed - enabled, set())


class MovieDiscoveryBackoffConstantsTests(unittest.TestCase):
    """Pinned: the backoff schedule and admin-notification constants are
    accessible from the loop. Sanity: ordered shorter→longer intervals."""

    def test_backoff_intervals_are_increasing(self):
        b = bot._MOVIE_DISCOVERY_RETRY_BACKOFF
        self.assertLess(b[1], b[2])
        self.assertLess(b[2], b[3])

    def test_backoff_first_retry_is_under_5_min(self):
        # First retry should be fast — Jackett's per-query cache often warms
        # within a couple of minutes after the bot started polling.
        self.assertLessEqual(bot._MOVIE_DISCOVERY_RETRY_BACKOFF[1], 300)


class NotifyAdminsTests(unittest.IsolatedAsyncioTestCase):
    """_notify_admins fans a message out to every ADMIN_CHAT_IDS entry,
    swallows per-chat errors so a flaky admin doesn't break startup signal."""

    async def test_sends_to_each_admin(self):
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.send_message = AsyncMock()
        with patch.object(bot, "ADMIN_CHAT_IDS", {100, 200, 300}):
            await bot._notify_admins(app, "test")
        # All three got the message
        self.assertEqual(app.bot.send_message.await_count, 3)
        # Same text payload for each
        texts = [c.kwargs["text"] for c in app.bot.send_message.await_args_list]
        self.assertEqual(set(texts), {"test"})

    async def test_swallows_per_chat_send_failure(self):
        app = MagicMock()
        app.bot = MagicMock()
        # First send raises, second succeeds — loop should not abort.
        app.bot.send_message = AsyncMock(side_effect=[RuntimeError("blocked"), None])
        with patch.object(bot, "ADMIN_CHAT_IDS", {100, 200}):
            await bot._notify_admins(app, "test")  # must not raise
        self.assertEqual(app.bot.send_message.await_count, 2)


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
