"""Search defaults, resolution priorities and mandatory source exclusions."""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot


def _release(title, *, seeders=10):
    return SimpleNamespace(
        title=title, seeders=seeders, size="5 GB", category="Movies",
        tracker="rutracker", topic_id="123", topic_url="https://example.com/123",
        magnet_url="", torrent_url="",
    )


def _context(base="Dune"):
    return SimpleNamespace(user_data={
        "srch_query": base,
        "srch_jackett_selected": {"rutracker"},
        "srch_jackett_indexers": [{"id": "rutracker", "name": "Rutracker"}],
    }, bot=Mock())


def _message_and_query(data="srch:quick"):
    message = SimpleNamespace(message_id=1, chat_id=100, chat=SimpleNamespace(id=100))
    message.edit_text = AsyncMock(return_value=message)
    message.get_bot = lambda: Mock()
    query = SimpleNamespace(
        data=data, message=message, answer=AsyncMock(),
        edit_message_text=AsyncMock(return_value=message),
    )
    return message, query


class SearchQualityPolicyTests(unittest.TestCase):
    def test_global_defaults_and_missing_query_quality_are_4k(self):
        self.assertEqual(bot._global_search_defaults()["quality"], "4K")
        self.assertEqual(bot._build_search_query("Dune", {}), "Dune 4K")
        self.assertIn("4K", bot._settings_text({}))

    def test_new_defaults_preserve_later_personal_choices_and_explicit_query(self):
        store = SimpleNamespace(load_user_search_defaults=Mock(return_value=None))
        context = _context()
        with patch.object(bot, "state_store", store):
            base, settings = bot._apply_intent_to_search_state(
                context, bot.parse_search_intent("Dune"), chat_id=100, fallback_text="Dune",
            )
            self.assertEqual(settings["quality"], "4K")
            self.assertEqual(context.user_data["srch_setting_sources"]["quality"], "default")
            store.load_user_search_defaults.return_value = {"quality": "720p"}
            self.assertEqual(bot._search_settings_for_chat(100)["quality"], "720p")
            _, settings = bot._apply_intent_to_search_state(
                context, bot.parse_search_intent("Dune 1080p"),
                chat_id=100, fallback_text="Dune 1080p", force_base="Dune 2021",
            )
        self.assertEqual(settings["quality"], "1080p")
        self.assertEqual(context.user_data["srch_setting_sources"]["quality"], "explicit")

    def test_2160_fallback_is_1080_before_other_qualities(self):
        results = [
            {"title": "Dune (2021) 1080p WEB-DL"},
            {"title": "Dune (2021) 720p BDRip"},
        ]
        shown, banner = bot._apply_search_quality_preference(results, "4K")
        self.assertEqual(shown, results[:1])
        self.assertIn("В 2160p ничего не найдено", banner)
        self.assertIn("Показаны 1080p", banner)
        shown, _ = bot._apply_search_quality_preference(results[1:], "2160p")
        self.assertEqual(shown, results[1:])

    def test_resolution_precedes_all_secondary_bonuses(self):
        low = {
            "title": "Dune (2021) 1080p WEB-DL 3xDUB Sub", "seeders": 500,
            "voice_preferred": True, "audio_preferred": True, "subs_preferred": True,
            "recommended": True,
        }
        high = {"title": "Dune (2021) 2160p WEBRip", "seeders": 0}
        results = [low, high]
        bot._sort_search_results(results)
        self.assertIs(results[0], high)
        self.assertTrue(high["recommended"])
        self.assertNotIn("recommended", low)

    def test_secondary_preferences_still_order_equal_resolution(self):
        ordinary = {"title": "Dune (2021) 2160p WEB-DL", "seeders": 500}
        voice = {"title": "Dune (2021) 2160p BDRip", "voice_preferred": True}
        results = [ordinary, voice]
        bot._sort_search_results(results)
        self.assertIs(results[0], voice)


class SearchQualityFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [
            patch.object(bot, "search_stages", return_value=[]),
            patch.object(bot, "SEARCH_ANIMATION_PATH", Path("missing-quality-test-animation.mp4")),
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_enrich_top_results_with_metadata", AsyncMock()),
            patch.object(bot, "_enrich_results_with_plex_hints", AsyncMock()),
            patch.object(bot, "_enrich_clusters_with_plex_hints", AsyncMock()),
            patch.object(bot, "_gpt_get_did_you_mean", AsyncMock(return_value=[])),
            patch.object(bot, "_gpt_get_search_failure_advice", AsyncMock(return_value=None)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def _search(self, releases, *, base="Dune", query="Dune 4K"):
        context = _context(base)
        message, callback = _message_and_query()
        source = SimpleNamespace(search=Mock(return_value=releases))
        with patch.object(bot, "jackett_client", source), patch.object(bot, "rutracker_client", None):
            state = await bot._run_search(AsyncMock(return_value=message), context, query)
        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(source.search.call_args.args[0], bot._split_query_settings(query)[0])
        return context, message, callback

    async def test_requested_season_falls_back_without_other_season_hiding_it(self):
        context, message, _ = await self._search([
            _release("Клиника / Scrubs / Сезон: 1 / WEB-DL 2160p"),
            _release("Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p"),
        ], base="Клиника Сезон: 2", query="Клиника Сезон: 2 4K")
        self.assertEqual(len(context.user_data["srch_results"]), 1)
        self.assertIn("Сезон: 2", context.user_data["srch_results"][0]["title"])
        self.assertIn("Показаны 1080p", message.edit_text.call_args.args[0])

    async def test_picker_preserves_films_then_applies_quality_and_single_recommendation(self):
        context, _, callback = await self._search([
            _release("Dune (2021) WEB-DL 2160p"),
            _release("Dune (2021) WEB-DL 1080p"),
            _release("Dune (1984) WEB-DL 1080p"),
            _release("Dune (1984) WEB-DL 720p"),
        ])
        self.assertEqual(len(context.user_data["srch_results_full"]), 4)
        visible = context.user_data["srch_picker_clusters"]
        index = next(i for i, cluster in enumerate(visible) if cluster["year"] == 1984)
        callback.data = f"srch:cluster:{index}"
        await bot.search_pick_cluster(SimpleNamespace(callback_query=callback), context)
        results = context.user_data["srch_results"]
        self.assertEqual([bot.result_quality(result) for result in results], ["1080p"])
        self.assertIn("Показаны 1080p", callback.edit_message_text.call_args.args[0])
        callback.data = "srch:cluster_back"
        await bot.search_cluster_back(SimpleNamespace(callback_query=callback), context)
        self.assertNotIn("В 2160p ничего не найдено", callback.edit_message_text.call_args.args[0])
        callback.data = "srch:cluster:all"
        await bot.search_pick_cluster(SimpleNamespace(callback_query=callback), context)
        results = context.user_data["srch_results"]
        self.assertEqual([bot.result_quality(result) for result in results], ["2160p", "1080p"])
        self.assertEqual(sum(bool(result.get("recommended")) for result in results), 1)
        callback.data = "srch:res_page:0"
        await bot.search_results_page(SimpleNamespace(callback_query=callback), context)
        self.assertIn("1984", callback.edit_message_text.call_args.args[0])

    async def test_any_and_no_quality_never_show_screeners(self):
        releases = [
            _release("Dune (2021) HDTC 2160p"),
            _release("Dune (2021) WEB-DL 1080p"),
        ]
        for query in ("Dune", "Dune 4K", "Dune 1080p"):
            with self.subTest(query=query):
                context, _, _ = await self._search(releases, query=query)
                self.assertEqual(len(context.user_data["srch_results"]), 1)
                self.assertIn("WEB-DL", context.user_data["srch_results"][0]["title"])
        context = _context()
        message, callback = _message_and_query("srch:no_quality")
        source = SimpleNamespace(search=Mock(return_value=releases))
        with patch.object(bot, "jackett_client", source), patch.object(bot, "rutracker_client", None):
            await bot.search_no_quality(SimpleNamespace(callback_query=callback), context)
        self.assertEqual(len(context.user_data["srch_results"]), 1)

    async def test_only_screeners_are_not_misreported_as_spelling_error(self):
        context, message, _ = await self._search([_release("Dune (2021) CAMRip 2160p")])
        self.assertEqual(context.user_data["srch_results"], [])
        self.assertIn("только экранки", message.edit_text.call_args.args[0])

    async def test_pagination_removes_cached_screeners_and_keeps_download_indices_consistent(self):
        context = _context()
        context.user_data["srch_results"] = [
            {"title": f"Dune (2021) CAMRip 2160p variant {index}", "recommended": True}
            for index in range(bot.SEARCH_PAGE_SIZE)
        ] + [{"title": "Dune (2021) WEB-DL 1080p", "size": "5 GB", "seeders": 10}]
        _, callback = _message_and_query("srch:res_page:1")
        state = await bot.search_results_page(SimpleNamespace(callback_query=callback), context)
        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(context.user_data["srch_results_page"], 0)
        self.assertEqual(len(context.user_data["srch_results"]), 1)
        self.assertTrue(context.user_data["srch_results"][0]["recommended"])
        self.assertNotIn("CAMRip", callback.edit_message_text.call_args.args[0])
        keyboard = callback.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("srch:dl:0", buttons)
        self.assertNotIn("srch:dl:5", buttons)

    async def test_pagination_with_only_cached_screeners_shows_retry_and_close(self):
        context = _context()
        context.user_data["srch_results"] = [{"title": "Dune (2021) CAMRip 2160p"}]
        _, callback = _message_and_query("srch:res_page:0")
        await bot.search_results_page(SimpleNamespace(callback_query=callback), context)
        self.assertEqual(context.user_data["srch_results"], [])
        keyboard = callback.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("srch:retry", buttons)
        self.assertIn("task:close:", buttons)

    async def test_direct_rutracker_checks_season_quality_and_screeners(self):
        releases = [
            _release("Клиника / Scrubs / Сезон: 1 / WEB-DL 2160p"),
            _release("Клиника / Scrubs / Сезон: 2 / HDTS 2160p"),
            _release("Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p"),
            _release("Клиника / Scrubs / Сезон: 2 / WEB-DL 720p"),
        ]
        context = _context("Клиника Сезон: 2")
        context.user_data["srch_search_query"] = "Клиника Сезон: 2 4K"
        _, callback = _message_and_query("srch:direct_rt")
        with patch.object(bot, "rutracker_client", SimpleNamespace(search=Mock(return_value=releases))):
            state = await bot.search_direct_rutracker(SimpleNamespace(callback_query=callback), context)
        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(len(context.user_data["srch_results"]), 1)
        self.assertIn("WEB-DL 1080p", context.user_data["srch_results"][0]["title"])

    async def test_direct_empty_and_only_screeners_do_not_crash(self):
        for releases in ([], [_release("Dune (2021) TS 1080p")]):
            with self.subTest(releases=releases):
                context = _context()
                context.user_data["srch_search_query"] = "Dune 4K"
                _, callback = _message_and_query("srch:direct_rt")
                with patch.object(bot, "rutracker_client", SimpleNamespace(search=Mock(return_value=releases))):
                    state = await bot.search_direct_rutracker(SimpleNamespace(callback_query=callback), context)
                self.assertEqual(state, bot.SEARCH_RESULTS)

    async def test_quick_search_without_current_settings_uses_4k(self):
        context = _context()
        _, callback = _message_and_query()
        store = SimpleNamespace(load_user_search_defaults=Mock(return_value=None))
        with patch.object(bot, "state_store", store), patch.object(bot, "_execute_search", AsyncMock()) as execute:
            await bot.search_quick(SimpleNamespace(callback_query=callback), context)
        self.assertEqual(execute.await_args.args[2], "Dune 4K")
