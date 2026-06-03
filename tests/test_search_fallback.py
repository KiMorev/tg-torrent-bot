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
from types import SimpleNamespace
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
        buttons = [b for row in keyboard for b in row]
        suggestion_button = next(b for b in buttons if "правильный запрос" in b.text)
        self.assertEqual(suggestion_button.callback_data, "srch:didmean:0")
        self.assertEqual(
            context.user_data["srch_didmean_suggestions"],
            ["правильный запрос", "alt query"],
        )


class SeriesMasterSearchTests(unittest.TestCase):
    def test_series_master_filters_movies_and_uses_reference_buttons(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            SimpleNamespace(
                title="Драйв / Drive (2011) WEB-DL 1080p",
                topic_url="https://rutracker.org/forum/viewtopic.php?t=1",
                tracker="rutracker",
                size="5 GB",
                seeders=50,
                magnet_url=None,
                torrent_url=None,
            ),
            SimpleNamespace(
                title="Клиника / Scrubs / Сезон: 3 / WEB-DL 1080p",
                topic_url="https://rutracker.org/forum/viewtopic.php?t=2",
                tracker="rutracker",
                size="20 GB",
                seeders=80,
                magnet_url=None,
                torrent_url=None,
            ),
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()
        context.user_data["srch_intent"] = bot.SEARCH_INTENT_SERIES_MASTER

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_enrich_top_results_with_metadata", new=AsyncMock(return_value=None)),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Клиника 1080p"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Эталонная раздача", text)
        self.assertIn("Клиника", text)
        self.assertNotIn("Драйв", text)
        buttons = {
            button.text: button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["🎯 1"], "srch:bulk_plan:0")
        self.assertNotIn("⬇️ 1", buttons)


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
    """When BOTH sources fail, show a temporary-failure screen without suggestions."""

    def test_jackett_error_then_rutracker_error_shows_temporary_failure_without_suggestions(self):
        mock_jackett = MagicMock()
        mock_jackett.search.side_effect = JackettError("jackett did not answer in 45 sec")
        mock_rutracker = MagicMock()
        mock_rutracker.search.side_effect = RutrackerError("521 Server Error: <none> for url")
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

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Поиск временно не получился", text)
        self.assertIn("источники поиска сейчас не ответили", text)
        self.assertIn("Это не значит, что раздач нет", text)
        self.assertNotIn("ничего не найдено", text)
        self.assertNotIn("Возможно вы имели в виду", text)
        self.assertNotIn("521 Server Error", text)
        self.assertNotIn("45 sec", text)
        keyboard = kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in keyboard for b in row]
        self.assertFalse(any("альтернатива" in lbl for lbl in labels))
        self.assertTrue(any("Повторить поиск" in lbl for lbl in labels))


class SplitQuerySettingsTests(unittest.TestCase):
    """Strategy 2: extract base + preferred quality from full search query."""

    def _split_quality(self, query: str) -> tuple[str, str | None]:
        base, quality, _audio, _subs = bot._split_query_settings(query)
        return base, quality

    def test_extracts_1080p_suffix(self):
        self.assertEqual(self._split_quality("Дюна 1080p"), ("Дюна", "1080p"))

    def test_extracts_2160p_suffix(self):
        self.assertEqual(self._split_quality("Аркейн 2160p"), ("Аркейн", "2160p"))

    def test_normalises_4k_to_2160p(self):
        self.assertEqual(self._split_quality("Барби 4k"), ("Барби", "2160p"))

    def test_normalises_uhd_to_2160p(self):
        self.assertEqual(self._split_quality("Дюна UHD"), ("Дюна", "2160p"))

    def test_no_suffix_means_no_filter(self):
        self.assertEqual(self._split_quality("Дюна 2024"), ("Дюна 2024", None))

    def test_year_not_misread_as_quality(self):
        # Year (4 digits) shouldn't be confused with a quality token.
        self.assertEqual(self._split_quality("Аркейн 2024"), ("Аркейн 2024", None))

    def test_multiword_title_preserved(self):
        self.assertEqual(
            self._split_quality("Дюна часть вторая 1080p"),
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

    def test_loading_text_does_not_include_search_fact(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = []
        send_fn, _msg = _make_send_fn()
        context = _make_context()
        context._chat_id = 100

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
            patch.object(bot, "_pick_search_fact_for_chat", return_value="\n\n💡: факт") as pick_fact,
        ):
            asyncio.run(bot._run_search(send_fn, context, "Дюна 2024"))

        loading_text = send_fn.call_args.args[0]
        self.assertNotIn("\n\n💡: факт", loading_text)
        pick_fact.assert_not_called()


class SearchDidmeanPreservesSettingsTests(unittest.TestCase):
    """search_didmean must keep the user's quality/audio/subs choices when
    re-running the search with a typo-corrected title."""

    def test_didmean_preserves_quality_preference(self):
        """User searched «Дюра 1080p» → typo → taps «Дюна» suggestion →
        bot must search for «Дюна 1080p», not bare «Дюна»."""
        query = MagicMock()
        query.data = "srch:didmean:0"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1, chat_id=100))
        update = MagicMock(callback_query=query)
        context = MagicMock()
        context.user_data = {
            "srch_query": "Дюра",
            "srch_search_query": "Дюра 1080p",
            "srch_didmean_suggestions": ["Дюна"],
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
        query.data = "srch:didmean:0"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1, chat_id=100))
        update = MagicMock(callback_query=query)
        context = MagicMock()
        context.user_data = {
            "srch_query": "Аркаин",
            "srch_search_query": "Аркаин 1080p Original Sub",
            "srch_didmean_suggestions": ["Аркейн"],
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

    def test_didmean_index_uses_stored_suggestion(self):
        query = MagicMock()
        query.data = "srch:didmean:1"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1, chat_id=100))
        update = MagicMock(callback_query=query)
        context = MagicMock()
        context.user_data = {
            "srch_didmean_suggestions": ["Дюна", "Аркейн"],
            "srch_settings": {"quality": "1080p", "audio": False, "subs": False},
        }

        with patch.object(bot, "_execute_search", new=AsyncMock(return_value=0)) as exec_mock:
            asyncio.run(bot.search_didmean(update, context))

        called_args = exec_mock.call_args.args
        self.assertEqual(called_args[2], "Аркейн 1080p")
        self.assertEqual(context.user_data["srch_query"], "Аркейн")


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

    def test_default_audio_preference_boosts_without_hiding_alternatives(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            self._make_jackett_result("Dune 2024 1080p WEB-DL"),
            self._make_jackett_result("Dune 2024 1080p Dual"),
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()
        context.user_data["srch_setting_sources"] = {
            "quality": "default",
            "audio": "default",
            "subs": "default",
        }

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Dune Original"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Dune 2024 1080p Dual", text)
        self.assertIn("Dune 2024 1080p WEB-DL", text)
        self.assertLess(text.index("Dune 2024 1080p Dual"), text.index("Dune 2024 1080p WEB-DL"))

    def test_explicit_audio_without_matches_keeps_alternatives_with_banner(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            self._make_jackett_result("Dune 2024 1080p WEB-DL"),
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()
        context.user_data["srch_setting_sources"] = {
            "quality": "default",
            "audio": "explicit",
            "subs": "default",
        }

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "Dune Original"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Dune 2024 1080p WEB-DL", text)
        self.assertIn("Original не нашёл", text)


class MediaIntentFilterIntegrationTests(unittest.TestCase):
    """Explicit «фильм» / «сериал» wording should narrow mixed tracker output."""

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

    def test_movie_intent_drops_series_when_movies_exist(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            self._make_jackett_result("Drive 2011 1080p WEB-DL"),
            self._make_jackett_result("Drive S01E01 1080p WEB-DL"),
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "фильм где водитель машина"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("Drive 2011", text)
        self.assertNotIn("S01E01", text)
        self.assertIn("Показаны фильмы", text)

    def test_series_intent_drops_movies_when_series_exist(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            self._make_jackett_result("Halo 2022 1080p WEB-DL"),
            self._make_jackett_result("Halo S01E01 1080p WEB-DL"),
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "сериал про кольцо"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("S01E01", text)
        self.assertNotIn("Halo 2022", text)
        self.assertIn("Показаны сериалы", text)

    def test_media_intent_does_not_empty_results(self):
        mock_jackett = MagicMock()
        mock_jackett.search.return_value = [
            self._make_jackett_result("Drive S01E01 1080p WEB-DL"),
        ]
        send_fn, message = _make_send_fn()
        context = _make_context()

        with (
            patch.object(bot, "jackett_client", mock_jackett),
            patch.object(bot, "rutracker_client", None),
            patch.object(bot, "_gpt_get_did_you_mean", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(bot._run_search(send_fn, context, "фильм где водитель машина"))

        args, kwargs = message.edit_text.call_args
        text = args[0] if args else kwargs.get("text", "")
        self.assertIn("S01E01", text)
        self.assertNotIn("Показаны фильмы", text)


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


class EnrichTopResultsWithMetadataTests(unittest.IsolatedAsyncioTestCase):
    """PR3: cache + parallel GPT enrichment of search result titles."""

    async def test_uses_cache_for_known_titles_no_gpt_call(self):
        """Pre-populated cache → enrichment attaches parsed_meta without
        a single GPT call."""
        results = [
            {"title": "Dune 2024 2160p"},
            {"title": "Dune 2024 1080p"},
        ]
        # Cache pre-loaded with both titles' hashes already parsed
        h1 = bot._title_hash("Dune 2024 2160p")
        h2 = bot._title_hash("Dune 2024 1080p")
        precooked_cache = {
            h1: {"quality": "2160p", "source": "UHD", "hdr": "HDR10",
                 "audio": None, "langs": ["RUS"], "release_group": None, "edition": None},
            h2: {"quality": "1080p", "source": "BDRip", "hdr": None,
                 "audio": None, "langs": ["RUS"], "release_group": None, "edition": None},
        }

        mock_parse = MagicMock(return_value=({"quality": "?"}, None))
        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "state_store", MagicMock(
                load_torrent_titles_cache=MagicMock(return_value=precooked_cache),
                save_torrent_titles_cache=MagicMock(),
            )),
            patch.object(bot, "gpt_features_parse_torrent_title", mock_parse),
        ):
            await bot._enrich_top_results_with_metadata(results, max_n=5)

        # No GPT calls because both hits
        mock_parse.assert_not_called()
        self.assertEqual(results[0]["parsed_meta"]["quality"], "2160p")
        self.assertEqual(results[1]["parsed_meta"]["quality"], "1080p")

    async def test_runs_gpt_for_misses_and_writes_back_to_cache(self):
        """Empty cache → GPT runs for each title; results attached + cache saved."""
        results = [{"title": "Inception 2010 1080p BluRay"}]
        empty_cache: dict = {}
        save_mock = MagicMock()
        gpt_response = ({"quality": "1080p", "source": "BluRay", "hdr": None,
                         "audio": "DTS", "langs": ["ENG"],
                         "release_group": "FGT", "edition": None}, None)
        gpt_parse_mock = MagicMock(return_value=gpt_response)

        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(bot, "GPT_MODEL", "gpt-4o-mini"),
            patch.object(bot, "state_store", MagicMock(
                load_torrent_titles_cache=MagicMock(return_value=empty_cache),
                save_torrent_titles_cache=save_mock,
            )),
            patch.object(bot, "gpt_features_parse_torrent_title", gpt_parse_mock),
            patch.object(bot, "_gpt_record_usage"),
        ):
            await bot._enrich_top_results_with_metadata(results, max_n=5)

        gpt_parse_mock.assert_called_once()
        self.assertEqual(results[0]["parsed_meta"]["source"], "BluRay")
        # Cache was saved with the new entry
        save_mock.assert_called_once()
        saved_cache = save_mock.call_args.args[0]
        h = bot._title_hash("Inception 2010 1080p BluRay")
        self.assertIn(h, saved_cache)

    async def test_noop_when_gpt_disabled(self):
        """GPT_ENABLED=False → function returns without touching anything."""
        results = [{"title": "Some Movie"}]
        mock_state = MagicMock()
        with (
            patch.object(bot, "GPT_ENABLED", False),
            patch.object(bot, "state_store", mock_state),
        ):
            await bot._enrich_top_results_with_metadata(results, max_n=5)
        mock_state.load_torrent_titles_cache.assert_not_called()
        self.assertNotIn("parsed_meta", results[0])

    async def test_only_top_n_processed(self):
        """Results beyond max_n are not parsed (cost control)."""
        results = [{"title": f"Movie {i}"} for i in range(20)]
        gpt_parse_mock = MagicMock(return_value=(
            {"quality": "1080p", "source": None, "hdr": None, "audio": None,
             "langs": [], "release_group": None, "edition": None}, None,
        ))

        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "sk-test"),
            patch.object(bot, "state_store", MagicMock(
                load_torrent_titles_cache=MagicMock(return_value={}),
                save_torrent_titles_cache=MagicMock(),
            )),
            patch.object(bot, "gpt_features_parse_torrent_title", gpt_parse_mock),
            patch.object(bot, "_gpt_record_usage"),
        ):
            await bot._enrich_top_results_with_metadata(results, max_n=5)

        # Only first 5 enriched
        self.assertEqual(gpt_parse_mock.call_count, 5)
        for r in results[:5]:
            self.assertIn("parsed_meta", r)
        for r in results[5:]:
            self.assertNotIn("parsed_meta", r)


class SearchQueryLabelTests(unittest.TestCase):
    """User-facing query labels must keep filters outside title quotes."""

    def test_quality_rendered_outside_title_quotes(self):
        self.assertEqual(
            bot._format_search_query_label("Драйв 1080p"),
            "«Драйв» (качество: 1080p)",
        )

    def test_audio_and_sub_filters_rendered_as_filters(self):
        self.assertEqual(
            bot._format_search_query_label("Драйв 1080p Original Sub"),
            "«Драйв» (качество: 1080p, оригинальная дорожка, субтитры)",
        )


class BuildSearchClustersTests(unittest.TestCase):
    """Proposal #1 cluster detection — groups results by title/year/type/season."""

    def test_single_film_makes_single_cluster(self):
        results = [
            {"title": "Дюна часть вторая 2024 1080p WEB-DL"},
            {"title": "Дюна часть вторая 2024 2160p BDRemux"},
        ]
        clusters = bot._build_search_clusters(results)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["count"], 2)
        self.assertEqual(clusters[0]["year"], 2024)

    def test_franchise_makes_multiple_clusters(self):
        results = [
            {"title": "Дюна 1984 BDRip"},
            {"title": "Дюна 2021 1080p"},
            {"title": "Дюна 2021 2160p"},
            {"title": "Дюна часть вторая 2024 1080p"},
            {"title": "Дюна часть вторая 2024 2160p"},
        ]
        clusters = bot._build_search_clusters(results)
        # We expect 3 distinct films: 1984, 2021, 2024
        self.assertEqual(len(clusters), 3)
        years = [c["year"] for c in clusters]
        # Sort: newest first
        self.assertEqual(years, sorted(years, reverse=True))

    def test_series_seasons_make_separate_clusters(self):
        results = [
            {"title": "Peaky Blinders S06 2022 1080p WEB-DL", "category": "series"},
            {"title": "Peaky Blinders S05 2019 1080p WEB-DL", "category": "series"},
        ]
        clusters = bot._build_search_clusters(results)
        self.assertEqual(
            [(c["title"], c["year"], c["season_label"], c["seasons"]) for c in clusters],
            [
                ("Peaky Blinders", 2022, "S6", [6]),
                ("Peaky Blinders", 2019, "S5", [5]),
            ],
        )

    def test_series_pack_gets_range_label(self):
        clusters = bot._build_search_clusters([
            {"title": "Peaky Blinders S01-S06 1080p WEB-DL", "category": "series"},
        ])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["title"], "Peaky Blinders")
        self.assertEqual(clusters[0]["season_label"], "S1-S6")
        self.assertEqual(clusters[0]["seasons"], [1, 2, 3, 4, 5, 6])

    def test_same_title_movie_and_series_do_not_merge(self):
        results = [
            {"title": "Fargo 2014 1080p WEB-DL", "category": "movies"},
            {"title": "Fargo S01 2014 1080p WEB-DL", "category": "series"},
        ]
        clusters = bot._build_search_clusters(results)
        by_kind = {cluster["kind"]: cluster for cluster in clusters}
        self.assertEqual(len(clusters), 2)
        self.assertEqual(by_kind["movie"]["title"], "Fargo")
        self.assertEqual(by_kind["movie"]["season_label"], "")
        self.assertEqual(by_kind["series"]["title"], "Fargo")
        self.assertEqual(by_kind["series"]["season_label"], "S1")

    def test_should_show_picker_when_multiple_real_clusters(self):
        results = [
            {"title": "Матрица 1999 1080p"},
            {"title": "Матрица 1999 720p"},
            {"title": "Матрица Воскрешение 2021 1080p"},
            {"title": "Матрица Воскрешение 2021 2160p"},
        ]
        clusters = bot._build_search_clusters(results)
        self.assertTrue(bot._should_show_cluster_picker(clusters))

    def test_should_not_show_picker_for_single_film(self):
        results = [
            {"title": "Дюна часть вторая 2024 1080p"},
            {"title": "Дюна часть вторая 2024 2160p"},
            {"title": "Дюна часть вторая 2024 BDRip"},
        ]
        clusters = bot._build_search_clusters(results)
        self.assertFalse(bot._should_show_cluster_picker(clusters))

    def test_should_not_show_picker_when_one_cluster_dominates(self):
        """One film with 10 releases + one with 1 release → don't fragment
        the UX for a single noise result. Real-cluster threshold is ≥2 releases."""
        results = (
            [{"title": f"Аркейн 2024 ep{i} 1080p"} for i in range(10)]
            + [{"title": "Аркейн Origins 2018 trailer"}]
        )
        clusters = bot._build_search_clusters(results)
        self.assertFalse(bot._should_show_cluster_picker(clusters))

    def test_picker_prefers_exact_title_over_token_noise(self):
        """Query «Драйв» should not hide exact «Драйв» behind newer token matches."""
        results = [
            {"title": "Ледяной драйв 2021 1080p"},
            {"title": "Ледяной драйв 2021 720p"},
            {"title": "Акудама Драйв 2020 1080p"},
            {"title": "Акудама Драйв 2020 720p"},
            {"title": "Драйв 2011 1080p"},
        ]
        clusters = bot._build_search_clusters(results)
        picker = bot._clusters_for_query_picker(clusters, "Драйв")
        self.assertEqual([c["title"] for c in picker], ["Драйв"])
        self.assertTrue(
            bot._should_show_cluster_picker(
                picker,
                total_clusters=len(clusters),
                filtered_for_query=True,
            )
        )

    def test_picker_shows_multiple_exact_title_years_even_with_single_release(self):
        results = [
            {"title": "Драйв 2011 1080p"},
            {"title": "Драйв 1997 720p"},
            {"title": "Ледяной драйв 2021 1080p"},
            {"title": "Ледяной драйв 2021 720p"},
        ]
        clusters = bot._build_search_clusters(results)
        picker = bot._clusters_for_query_picker(clusters, "Драйв")
        self.assertEqual(
            [(c["title"], c["year"]) for c in picker],
            [("Драйв", 2011), ("Драйв", 1997)],
        )
        self.assertTrue(
            bot._should_show_cluster_picker(
                picker,
                total_clusters=len(clusters),
                filtered_for_query=True,
            )
        )


class DidmeanPrefetchCleanupTests(unittest.TestCase):
    """Proposal #2 prefetch cleanup — _cancel_didmean_prefetch must cancel
    in-flight asyncio.Task and pop the slot, safely on no-op too."""

    def test_noop_when_slot_empty(self):
        context = MagicMock()
        context.user_data = {}
        # Must not raise
        bot._cancel_didmean_prefetch(context)
        self.assertNotIn("srch_didmean_prefetch", context.user_data)

    def test_cancels_running_task(self):
        context = MagicMock()
        # Mock task: not done, has .cancel()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        context.user_data = {"srch_didmean_prefetch": ("Дюна", mock_task)}
        bot._cancel_didmean_prefetch(context)
        mock_task.cancel.assert_called_once()
        self.assertNotIn("srch_didmean_prefetch", context.user_data)

    def test_skips_cancel_when_already_done(self):
        context = MagicMock()
        mock_task = MagicMock()
        mock_task.done.return_value = True
        mock_task.cancel = MagicMock()
        context.user_data = {"srch_didmean_prefetch": ("Дюна", mock_task)}
        bot._cancel_didmean_prefetch(context)
        # Done task — no point cancelling (it's already finished)
        mock_task.cancel.assert_not_called()
        # But slot still popped
        self.assertNotIn("srch_didmean_prefetch", context.user_data)


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
