import asyncio
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from movie_discovery import _finalize_card, build_cards, evaluate_result, parse_qualities, prepare_card_releases


def release(quality="2160p", *, title=None, score=1, seeders=1, size="8 GB"):
    return {
        "title": title or f"Film (2026) WEB-DL {quality}",
        "movie_title": "Film",
        "year": 2026,
        "quality": quality,
        "score": score,
        "seeders": seeders,
        "size": size,
        "source": "jackett",
        "tracker": "rutracker",
        "topic_id": "1",
        "url": "https://example.test/release",
    }


def card(releases, *, kp_id=1):
    return {"key": f"kp:{kp_id}", "kp_id": kp_id, "title": "Film", "year": 2026, "releases": releases}


class DiscoveryReleaseQualityTests(unittest.TestCase):
    def test_empty_quality_configuration_collects_2160_and_1080(self):
        self.assertEqual(parse_qualities(""), ["2160p", "1080p"])
        self.assertEqual(parse_qualities("1080p"), ["1080p"])

    def test_best_release_prefers_quality_before_seeders_and_source_score(self):
        preferred = release()
        strong_hd = release("1080p", title="Film (2026) BDRemux 1080p", score=5000, seeders=500)
        movie = card([strong_hd, preferred])
        _finalize_card(movie, set())
        self.assertEqual(movie["releases"][0], preferred)
        self.assertEqual(movie["best_quality"], "2160p")
        self.assertEqual(movie["best_seeders"], 1)

    def test_eight_release_limit_retains_best_of_each_available_quality(self):
        uhd = [release(title=f"Film (2026) 2160p version {index}", score=100-index) for index in range(10)]
        hd = release("1080p", score=9999, size="3 GB")
        movie = card([*uhd, hd])
        _finalize_card(movie, set())
        self.assertEqual(len(movie["releases"]), 8)
        self.assertEqual(movie["release_count"], 11)
        self.assertEqual(movie["releases"][0], uhd[0])
        self.assertIn(hd, movie["releases"])

    def test_existing_2160_size_limit_is_unchanged(self):
        result = SimpleNamespace(title="Film (2026) WEB-DL 2160p", size="4.49 GB", seeders=0)
        accepted, reason = evaluate_result(result, source="jackett", allowed_years={2026}, qualities={"2160p"})
        self.assertIsNone(accepted)
        self.assertEqual(reason, "size_too_small")
        result.size = "4.5 GB"
        accepted, _ = evaluate_result(result, source="jackett", allowed_years={2026}, qualities={"2160p"})
        self.assertIsNotNone(accepted)

    def test_fresh_hd_screen_capture_is_rejected(self):
        result = SimpleNamespace(title="Film (2026) HDTC 2160p", size="8 GB", seeders=200)
        accepted, reason = evaluate_result(result, source="jackett", allowed_years={2026}, qualities={"2160p"})
        self.assertIsNone(accepted)
        self.assertEqual(reason, "noise_title")

    def test_saved_screen_release_is_removed_and_best_fields_are_rebuilt(self):
        screen = release(title="Film (2026) HDCAMRip 2160p", score=9999)
        hd = release("1080p", size="3 GB")
        original = card([screen, hd])
        original.update(best_quality="2160p", best_size="8 GB", best_seeders=999, release_count=2)
        prepared = prepare_card_releases(original)
        self.assertEqual(prepared["releases"], [hd])
        self.assertEqual(prepared["best_quality"], "1080p")
        self.assertEqual(prepared["best_size"], "3 GB")
        self.assertEqual(prepared["release_count"], 1)
        self.assertEqual(len(original["releases"]), 2)
        self.assertIsNone(prepare_card_releases(card([screen])))

    def test_cached_and_quiet_hour_cards_hide_screen_only_movies(self):
        screen_only = card([release(title="Film (2026) TSRip 2160p")])
        good = card([release()], kp_id=2)
        self.assertEqual(bot._movie_discovery_confirmed_cards({"cards": [screen_only, good]}), [prepare_card_releases(good)])
        self.assertEqual(bot._movie_notification_pending_cards({"cards": [screen_only, good]}), [prepare_card_releases(good)])

    def test_rebuild_from_saved_releases_cannot_reintroduce_screen_release(self):
        hd = release("1080p", size="3 GB")
        cache = build_cards(
            [release(title="Film (2026) HDCAMRip 2160p"), hd],
            now_text="2026-10-11 12:00", known_fingerprints=set(), limit=30, min_kp_rating=0,
        )
        self.assertEqual(len(cache["cards"]), 1)
        self.assertEqual(cache["cards"][0]["releases"], [hd])


class DiscoveryNotificationQualityTests(unittest.TestCase):
    def test_default_one_tap_choice_is_2160_even_when_1080_score_is_stronger(self):
        movie = card([
            release("1080p", title="Film WEB-DL 1080p 3xDUB Sub", seeders=500, size="3 GB"),
            release(title="Film BDRemux 2160p", seeders=0),
        ])
        with patch.object(bot, "_search_defaults_for_chat", return_value={}):
            selected, notes = bot._movie_notification_pick_release(movie, 100)
        self.assertEqual(selected["quality"], "2160p")
        self.assertEqual(notes, [])

    def test_personal_1080_preference_is_preserved(self):
        movie = card([release(), release("1080p", size="3 GB")])
        with patch.object(bot, "_search_defaults_for_chat", return_value={"quality": "1080p"}):
            selected, _ = bot._movie_notification_pick_release(movie, 100)
        self.assertEqual(selected["quality"], "1080p")

    def test_missing_2160_falls_back_and_push_text_matches_selected_result(self):
        movie = card([release("1080p", size="3 GB")])
        movie.update(best_quality="2160p", best_size="8 GB")
        with patch.object(bot, "_search_defaults_for_chat", return_value={}):
            items = bot._movie_notification_snapshot_items([movie], 100)
        text = bot._format_movie_notification_text([movie], items=items)
        self.assertIn("1080p, 3 GB", text)
        self.assertNotIn("2160p, 8 GB", text)
        self.assertIn("2160p не нашёл", text)

    def test_push_selected_size_and_preference_notes_are_html_escaped(self):
        movie = card([release("1080p", size="3 GB")])
        item = {
            "card": bot._movie_notification_snapshot_card(movie),
            "result": release("1080p", size="3 GB <check>"),
            "notes": ["Original <missing> & fallback"],
        }
        text = bot._format_movie_notification_text([movie], items=[item])
        self.assertIn("3 GB &lt;check&gt;", text)
        self.assertIn("Original &lt;missing&gt; &amp; fallback", text)

    def test_gpt_tiebreak_only_receives_selected_quality_candidates(self):
        movie = card([
            release(title="Film 2160p A"),
            release(title="Film 2160p B"),
            release("1080p", title="Film 1080p C"),
        ])
        with (
            patch.object(bot, "GPT_ENABLED", True),
            patch.object(bot, "_search_defaults_for_chat", return_value={}),
            patch.object(bot, "_movie_notification_release_score", side_effect=lambda result, _: 10000 if "1080" in result["title"] else 1),
            patch.object(bot, "gpt_choose_movie_notification_release", return_value=(1, "same quality", None)) as choose,
            patch.object(bot, "_gpt_record_usage"),
        ):
            selected, _ = asyncio.run(bot._movie_notification_pick_release_for_snapshot(movie, 100))
        self.assertEqual(selected["quality"], "2160p")
        candidates = choose.call_args.kwargs["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(candidate["quality"] == "2160p" for candidate in candidates))

    def test_snapshot_screen_filter_keeps_original_button_indices(self):
        screen = release(title="Film (2026) CAMRip 2160p")
        good = release("1080p", size="3 GB")
        snapshot = {
            "chat_id": 100,
            "created_at": datetime.now(bot.DISPLAY_TIMEZONE).isoformat(),
            "items": [
                {"card": card([screen]), "result": screen},
                {"card": card([good], kp_id=2), "result": good},
            ],
        }
        with patch.object(bot, "_load_movie_discovery_settings", return_value={"movie_notification_snapshots": {"push": snapshot}}):
            loaded = bot._load_movie_notification_snapshot("push", 100)
        self.assertEqual(len(loaded["items"]), 2)
        self.assertIsNone(loaded["items"][0]["result"])
        self.assertEqual(loaded["items"][1]["result"], good)
        self.assertEqual(bot._movie_notification_downloadable_indices(loaded["items"]), [1])
        self.assertEqual(snapshot["items"][0]["result"], screen)

    def test_old_screen_download_button_never_starts_download(self):
        query = SimpleNamespace(
            data="new:dl:abc123def0:0", message=SimpleNamespace(chat=SimpleNamespace(id=100)),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})
        screen = release(title="Film (2026) HDTS 2160p")
        with (
            patch.object(bot, "_is_allowed", return_value=True),
            patch.object(bot, "_movie_notification_download_query", AsyncMock(return_value=query)),
            patch.object(bot, "_load_movie_notification_snapshot", return_value={"items": [{"card": card([screen]), "result": screen}]}),
            patch.object(bot, "_download_and_add", AsyncMock()) as download,
        ):
            state = asyncio.run(bot.movie_new_notification_download(update, context))
        self.assertEqual(state, bot.ConversationHandler.END)
        download.assert_not_awaited()
        self.assertNotIn("srch_results", context.user_data)

    def test_new_release_list_recommends_first_preferred_quality_and_hides_screens(self):
        query = SimpleNamespace(
            data="new:show:0", message=SimpleNamespace(chat=SimpleNamespace(id=100)),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(user_data={})
        movie = card([
            release("1080p", title="Film WEB-DL 1080p 3xDUB Sub", seeders=500),
            release(title="Film 2160p"),
            release(title="Film HDTC 2160p"),
        ])
        with (
            patch.object(bot, "_is_allowed", return_value=True),
            patch.object(bot, "_load_movie_discovery_cache", return_value={"cards": [movie]}),
            patch.object(bot, "_search_defaults_for_chat", return_value={}),
        ):
            asyncio.run(bot.movie_new_show_releases(SimpleNamespace(callback_query=query), context))
        results = context.user_data["srch_results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["quality"], "2160p")
        self.assertTrue(results[0]["recommended"])
        self.assertNotIn("HDTC", query.edit_message_text.call_args.args[0])
