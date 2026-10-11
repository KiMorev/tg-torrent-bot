"""Resolution-first selection and source exclusions across season workflows."""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "999")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

import bot
from jackett_subscriptions import select_jackett_subscription_candidate
from series_bulk_planner import (
    STATUS_GOOD, STATUS_MISSING, STATUS_NEEDS_DECISION,
    VOICE_REQUIRE_SELECTED, VOICE_SINGLE_FROM_REFERENCE,
    SeriesBulkProfile, build_series_bulk_plan,
)


def release(quality="1080p", *, source="WEB-DL", voice="LostFilm", season="S01", seeders=1):
    title = f"Scrubs {season} {source} {quality} {voice}"
    return {
        "title": title, "source": "jackett", "tracker_name": "rutracker",
        "url": f"https://example.test/{title}", "seeders": seeders, "size": "10 GB",
    }


def plan(results, **settings):
    return build_series_bulk_plan(
        series_title="Scrubs", seasons=[1], results=results,
        profile=SeriesBulkProfile(**settings),
    )


def candidate(quality="4k"):
    return bot.SeriesCatchUpCandidate(
        identity=bot.PlexSeriesIdentity(title="Scrubs", original_title="Scrubs", plex_rating_key="show", plex_guid="plex://show/scrubs"),
        season_number=1, present_count=2, known_total=8, quality=quality,
    )


class SeriesQualitySelectionTests(unittest.TestCase):
    def test_screen_2160_does_not_block_clean_1080_fallback(self):
        result = plan([release("2160p", source="HDTS"), release()], quality="2160p")
        season = result.seasons[0]
        self.assertEqual(season.status, STATUS_GOOD)
        self.assertIn("1080p", season.selected.result["title"])
        self.assertTrue(all("HDTS" not in item.result["title"] for item in season.candidates))
        self.assertIn("preferred quality unavailable: 2160p; selected quality: 1080p", season.selected.warnings)

    def test_missing_2160_prefers_1080_over_720_secondary_bonuses(self):
        lower = release("720p", source="BDRemux", seeders=500)
        lower["size"] = "80 GB"
        season = plan([lower, release()], quality="2160p", release_type="BDRemux").seasons[0]
        self.assertIn("1080p", season.selected.result["title"])

    def test_any_quality_selects_higher_resolution_without_cross_quality_dispute(self):
        lower = release(source="BDRemux", seeders=500)
        season = plan([lower, release("2160p")], quality="any", release_type="BDRemux").seasons[0]
        self.assertEqual(season.status, STATUS_GOOD)
        self.assertIn("2160p", season.selected.result["title"])

    def test_explicit_1080_is_preserved_when_2160_exists(self):
        season = plan([release("2160p", seeders=500), release()], quality="1080p").seasons[0]
        self.assertIn("1080p", season.selected.result["title"])

    def test_metadata_quality_uses_same_bucket_for_availability_and_sorting(self):
        higher = release("")
        higher["quality"] = "2160p"
        season = plan([release(), higher], quality="2160p").seasons[0]
        self.assertIs(season.selected.result, higher)
        self.assertEqual(season.selected.release.quality, "2160p")

    def test_close_candidates_of_same_top_quality_still_require_decision(self):
        season = plan([release("2160p"), release("2160p", voice="NewStudio"), release()], quality="any").seasons[0]
        self.assertEqual(season.status, STATUS_NEEDS_DECISION)
        self.assertIsNone(season.selected)

    def test_2160_missing_required_original_allows_valid_1080(self):
        higher = release("2160p")
        lower = release()
        lower["title"] += " Original"
        season = plan([higher, lower], quality="2160p", require_original=True).seasons[0]
        self.assertIs(season.selected.result, lower)

    def test_2160_wrong_required_voice_allows_valid_1080(self):
        season = plan(
            [release("2160p", voice="NewStudio"), release()],
            quality="2160p", voice_policy=VOICE_REQUIRE_SELECTED, voices=("LostFilm",),
        ).seasons[0]
        self.assertIn("1080p", season.selected.result["title"])

    def test_screen_only_any_and_soft_profiles_are_missing(self):
        for profile in [SeriesBulkProfile(quality="any"), bot._series_bulk_soft_profile(SeriesBulkProfile(quality="2160p"))]:
            with self.subTest(profile=profile):
                result = build_series_bulk_plan(series_title="Scrubs", seasons=[1], results=[release(source="HDTC")], profile=profile)
                self.assertEqual(result.seasons[0].status, STATUS_MISSING)

    def test_pack_candidates_exclude_screen_sources_and_sort_quality_first(self):
        result = plan([
            release(season="S01-S03", seeders=500),
            release("2160p", season="S01-S03"),
            release("2160p", source="CAMRip", season="S01-S03"),
        ], quality="any")
        self.assertEqual(len(result.pack_candidates), 2)
        self.assertIn("2160p", result.pack_candidates[0]["title"])

    def test_single_voice_uses_quality_before_secondary_score_on_equal_coverage(self):
        results = [
            release("720p", source="BDRemux", season=f"S0{s}", seeders=500)
            for s in [1, 2]
        ] + [release("2160p", voice="NewStudio", season=f"S0{s}") for s in [1, 2]]
        result = build_series_bulk_plan(
            series_title="Scrubs", seasons=[1, 2], results=results,
            profile=SeriesBulkProfile(quality="any", voice_policy=VOICE_SINGLE_FROM_REFERENCE,
                                      voices=("LostFilm", "NewStudio"), release_type="BDRemux"),
        )
        self.assertTrue(all("NewStudio" in season.selected.result["title"] for season in result.seasons))

    def test_single_voice_preserves_coverage_before_quality(self):
        result = build_series_bulk_plan(
            series_title="Scrubs", seasons=[1, 2],
            results=[release(season="S01"), release(season="S02"), release("2160p", voice="NewStudio")],
            profile=SeriesBulkProfile(quality="any", voice_policy=VOICE_SINGLE_FROM_REFERENCE,
                                      voices=("LostFilm", "NewStudio")),
        )
        self.assertTrue(all("LostFilm" in season.selected.result["title"] for season in result.seasons))


class SavedSeasonPolicyTests(unittest.TestCase):
    def test_stale_screen_download_is_blocked_before_external_clients(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        context = SimpleNamespace(user_data={"srch_results": [release(source="HDTC")]})
        with (
            patch.object(bot, "ds_client", MagicMock()) as ds,
            patch.object(bot, "jackett_client", MagicMock()) as jackett,
            patch.object(bot, "rutracker_client", MagicMock()) as tracker,
        ):
            self.assertEqual(asyncio.run(bot._download_and_add(query, context, 0)), bot.SEARCH_RESULTS)
        self.assertEqual(ds.mock_calls, [])
        self.assertEqual(jackett.mock_calls, [])
        self.assertEqual(tracker.mock_calls, [])
        self.assertNotIn("srch_picked", context.user_data)

    def test_restore_drops_screen_selected_without_selecting_clean_alternative(self):
        clean = release()
        screen = release("2160p", source="HDTS")
        job = {
            "series_title": "Scrubs", "pack_candidates": [release(source="CAMRip", season="S01-S03")],
            "seasons": {"1": {"plan_status": STATUS_GOOD, "runtime_status": "failed", "result": screen,
                               "selected": {"result": screen}, "candidates": [{"result": screen}, {"result": clean}]}},
        }
        result = bot._series_bulk_plan_from_job(job)
        season = result.seasons[0]
        self.assertIsNone(season.selected)
        self.assertEqual(season.status, STATUS_NEEDS_DECISION)
        self.assertEqual([item.result["title"] for item in season.candidates], [clean["title"]])
        self.assertEqual(result.pack_candidates, ())
        self.assertEqual(bot._series_bulk_context_maps_from_job(job)[1:], ({}, {}))

    def test_restore_preserves_existing_selected_1080(self):
        chosen = release()
        job = {"series_title": "Scrubs", "seasons": {"1": {
            "plan_status": STATUS_GOOD, "selected": {"result": chosen},
            "candidates": [{"result": release("2160p")}, {"result": chosen}],
        }}}
        self.assertEqual(bot._series_bulk_plan_from_job(job).seasons[0].selected.result["title"], chosen["title"])

    def test_new_jackett_subscription_uses_selected_fallback_quality(self):
        context = SimpleNamespace(user_data={"srch_search_query": "Scrubs S01 2160p Original Sub"})
        store = MagicMock()
        store.load_topic_subscriptions.return_value = {}
        with patch.object(bot, "state_store", store):
            _key, sub = bot._save_subscription_for_result(
                context, release(), chat_id=100,
                notify_policy="each_update", download_policy="auto_each_update",
            )
        self.assertEqual(sub["query"], "Scrubs S01 1080p Original Sub")
        self.assertEqual(sub["quality"], "1080p")


class SubscriptionQualityPolicyTests(unittest.TestCase):
    def _progress_release(self, quality="1080p", source="WEB-DL"):
        result = release(quality, source=source)
        result["title"] += " Серии: 1-4 из 8"
        return result

    def test_new_preference_selects_2160_before_1080_and_allows_1080_fallback(self):
        lower = self._progress_release(source="BDRemux")
        higher = self._progress_release("2160p")
        sub = {"quality": "4k", "quality_is_preference": True}
        chosen, _ = bot._plex_season_subscription_select_result(sub, candidate(), [lower, higher])
        self.assertIs(chosen, higher)
        chosen, _ = bot._plex_season_subscription_select_result(sub, candidate(), [lower])
        self.assertIs(chosen, lower)

    def test_existing_fixed_1080_subscription_cannot_upgrade_automatically(self):
        lower = self._progress_release()
        higher = self._progress_release("2160p")
        sub = {"quality": "1080"}
        chosen, _ = bot._plex_season_subscription_select_result(sub, candidate("1080"), [higher, lower])
        self.assertIs(chosen, lower)
        chosen, uncertain = bot._plex_season_subscription_select_result(sub, candidate("1080"), [higher])
        self.assertIsNone(chosen)
        self.assertEqual(uncertain, [])

    def test_unknown_live_quality_preserves_stored_preference(self):
        lower = self._progress_release()
        chosen, _ = bot._plex_season_subscription_select_result(
            {"quality": "1080p", "quality_is_preference": True},
            candidate(""), [self._progress_release("2160p"), lower],
        )
        self.assertIs(chosen, lower)

    def test_uncertain_options_are_quality_first_and_exclude_screen_sources(self):
        lower, higher = release(), release("2160p")
        chosen, uncertain = bot._plex_season_subscription_select_result(
            {"quality_is_preference": True}, candidate(), [lower, release("2160p", source="HDTC"), higher],
        )
        self.assertIsNone(chosen)
        self.assertEqual(uncertain, [higher, lower])

    def test_known_plex_quality_turns_new_preference_into_fixed_quality(self):
        sub = {"quality": "4k", "quality_is_preference": True}
        bot._plex_season_subscription_update_from_candidate(sub, candidate("1080"))
        self.assertEqual(sub["quality"], "1080")
        self.assertFalse(sub["quality_is_preference"])

    def test_new_missing_unknown_season_uses_default_and_marks_preference(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        context = SimpleNamespace(user_data={})
        missing = bot.SeriesMissingSeasonCandidate(identity=candidate().identity, season_number=1, episode_count=8)
        with (
            patch.object(bot, "_chat_id_from_query", return_value=100),
            patch.object(bot, "_search_settings_for_chat", return_value={"quality": "4K"}),
            patch.object(bot, "_save_plex_season_subscription", return_value=("sub", {})) as save,
            patch.object(bot, "_series_continue_season_subscription_map_for_chat", return_value={}),
            patch.object(bot, "_series_continue_missing_detail_keyboard", return_value=None),
        ):
            asyncio.run(bot._series_continue_subscribe_missing_season(query, context, missing, scope="missing", index=0, page=0, group={}))
        self.assertEqual(save.call_args.args[0].quality, "4k")
        self.assertTrue(save.call_args.kwargs["quality_is_preference"])

    def test_resubscribing_unknown_season_does_not_replace_existing_fixed_quality(self):
        store = MagicMock()
        store.load_topic_subscriptions.return_value = {
            bot._plex_season_subscription_key(100, candidate()): {"quality": "1080"},
        }
        with patch.object(bot, "state_store", store):
            _key, sub = bot._save_plex_season_subscription(candidate(), chat_id=100, quality_is_preference=True)
        self.assertEqual(sub["quality"], "1080")
        self.assertFalse(sub.get("quality_is_preference", False))

    def test_jackett_selector_excludes_screen_in_current_and_legacy_schemas(self):
        result = SimpleNamespace(title="Scrubs S01 HDTS 2160p", tracker="rutracker", topic_url="same", seeders=100)
        for sub in [{"version": 2, "topic_url": "same"}, {"seen_titles": []}]:
            with self.subTest(sub=sub):
                self.assertIsNone(select_jackett_subscription_candidate(sub, [result]))

    def test_jackett_existing_fixed_quality_cannot_upgrade_automatically(self):
        result = SimpleNamespace(title="Scrubs S01 2160p", tracker="rutracker", topic_url="same", seeders=100)
        self.assertIsNone(select_jackett_subscription_candidate({"version": 2, "quality": "1080p", "topic_url": "same"}, [result]))

    def test_blocked_pending_notification_does_not_advance_subscription_progress(self):
        for deliver, key in [(bot._deliver_jackett_subscription_pending_notification, "jackett:test"),
                             (bot._deliver_rutracker_subscription_notification, "123")]:
            with self.subTest(deliver=deliver):
                pending = {"title": "Scrubs S01 HDTS 2160p", "new_episode_end": 4, "complete": False}
                sub = {"chat_id": 100, "last_episode_end": 2, "pending_notification": pending}
                app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
                self.assertTrue(asyncio.run(deliver(app, {key: sub}, key, sub, pending)))
                self.assertEqual(sub["last_episode_end"], 2)
                self.assertNotIn("pending_notification", sub)
                app.bot.send_message.assert_not_awaited()


class PendingScreenPolicyTests(unittest.TestCase):
    def test_queued_screen_cannot_reach_download_clients(self):
        client = MagicMock()
        with patch.object(bot, "ds_client", client), patch.object(bot, "_temp_path") as temp_path:
            with self.assertRaisesRegex(RuntimeError, "Экранные копии"):
                asyncio.run(bot._attempt_pending_download({"title": "Film CAM 2026 2160p", "magnet_url": "magnet:?test"}))
        client.create_magnet.assert_not_called()
        temp_path.assert_not_called()

    def test_old_queued_screen_is_dropped_without_replacing_the_release(self):
        pending = {"entry": {"title": "Film CAMRip 2160p", "chat_id": 100}}
        attempt = AsyncMock()
        with (
            patch.object(bot, "_pending_downloads_enabled", return_value=True),
            patch.object(bot, "_load_pending_downloads", return_value=pending),
            patch.object(bot, "_save_pending_downloads") as save,
            patch.object(bot, "_notify_pending_dropped", AsyncMock(return_value=True)),
            patch.object(bot, "_series_bulk_record_pending_retry_result") as record,
            patch.object(bot, "_attempt_pending_download", attempt),
        ):
            asyncio.run(bot._run_pending_downloads_once(SimpleNamespace()))
        attempt.assert_not_awaited()
        self.assertEqual(pending, {})
        save.assert_called_once_with({})
        self.assertEqual(record.call_args.args[1], "pending_failed")

    def test_failed_block_notice_retries_notice_and_preserves_already_started_task(self):
        pending = {"entry": {"title": "Film HDTS 2160p", "chat_id": 100}}
        attempt = AsyncMock()
        notify_success = AsyncMock(return_value=True)
        with (
            patch.object(bot, "_pending_downloads_enabled", return_value=True),
            patch.object(bot, "_load_pending_downloads", return_value=pending),
            patch.object(bot, "_save_pending_downloads"),
            patch.object(bot, "_notify_pending_dropped", AsyncMock(return_value=False)),
            patch.object(bot, "_notify_pending_success", notify_success),
            patch.object(bot, "_series_bulk_record_pending_retry_result"),
            patch.object(bot, "_attempt_pending_download", attempt),
        ):
            asyncio.run(bot._run_pending_downloads_once(SimpleNamespace()))
            self.assertEqual(pending["entry"]["notification_pending"], "drop")
            asyncio.run(bot._run_pending_downloads_once(SimpleNamespace()))
            pending["entry"].update(notification_pending="success", task_id="started-task")
            asyncio.run(bot._run_pending_downloads_once(SimpleNamespace()))
        attempt.assert_not_awaited()
        notify_success.assert_awaited_once()
        self.assertEqual(notify_success.await_args.args[2], "started-task")

    def test_block_notice_explains_policy_and_has_retry_and_close(self):
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot, "_background_chat_id", return_value=100):
            asyncio.run(bot._notify_pending_dropped(app, {"title": "Film CAMRip 2160p", "chat_id": 100}))
        sent = app.bot.send_message.await_args.kwargs
        self.assertIn("не запущена", sent["text"])
        self.assertNotIn("попыток", sent["text"])
        callbacks = [button.callback_data for row in sent["reply_markup"].inline_keyboard for button in row]
        self.assertIn("srch:retry", callbacks)
        self.assertIn("task:close:", callbacks)
