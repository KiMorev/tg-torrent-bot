"""Tests for partial-series download/notification pickers in bot.py."""
from __future__ import annotations

import asyncio
import copy
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
from subscription_policy import (
    DOWNLOAD_AUTO_EACH_UPDATE, DOWNLOAD_NOTIFY_ONLY,
    DOWNLOAD_ONLY_WHEN_COMPLETE,
    NOTIFY_EACH_UPDATE, NOTIFY_FINAL_ONLY, NOTIFY_SILENT,
)


def _make_query(data: str) -> MagicMock:
    """Build a callback-query-shaped mock that records edit_message_text calls."""
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return q


def _make_context(*, results: list[dict] | None = None) -> MagicMock:
    ctx = MagicMock()
    if results is None:
        results = [{"title": "Test Show", "partial": True}]
    ctx.user_data = {"srch_results": results}
    ctx.bot = MagicMock()
    ctx.bot.send_animation = AsyncMock()
    return ctx


def _make_message_update(*, chat_id: int = 100) -> MagicMock:
    update = MagicMock()
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _jackett_result(title: str, *, url: str = "", seeders: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        topic_url=url or f"https://example.test/{abs(hash(title))}",
        tracker="rutracker",
        size="10 GB",
        seeders=seeders,
        magnet_url="",
        torrent_url="",
    )


def _bulk_plan(*, seasons: list[int], results: list[dict]):
    return bot.build_series_bulk_plan(
        series_title="Клиника",
        seasons=seasons,
        results=results,
        profile=bot.SeriesBulkProfile(
            quality="1080p",
            require_original=True,
            require_subs=True,
        ),
        verified_season_range=True,
    )


def _bulk_profile():
    return bot.SeriesBulkProfile(
        quality="1080p",
        require_original=True,
        require_subs=True,
    )


def _fake_series_bulk_store(initial: dict | None = None):
    jobs = copy.deepcopy(initial or {})
    store = MagicMock()
    store.load_approved_chat_ids.return_value = set()
    store.load_series_bulk_jobs.side_effect = lambda: copy.deepcopy(jobs)

    def save_series_bulk_jobs(updated):
        jobs.clear()
        jobs.update(copy.deepcopy(updated))

    store.save_series_bulk_jobs.side_effect = save_series_bulk_jobs
    return store, jobs


def _series_bulk_job(
    *,
    job_id: str = "bulk_test",
    chat_id: int = 100,
    title: str = "Клиника",
    status: str = "planned",
) -> dict:
    result = {
        "title": f"{title} / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
        "source": "jackett",
        "tracker_name": "rutracker",
        "torrent_url": "https://jackett.local/dl/1",
        "seeders": 20,
        "size": "10 GB",
    }
    profile = _bulk_profile()
    plan = bot.build_series_bulk_plan(
        series_title=title,
        seasons=[1, 2],
        results=[result],
        profile=profile,
        verified_season_range=True,
    )
    return {
        "id": job_id,
        "chat_id": chat_id,
        "series_title": title,
        "created_at": "2026-05-26T10:00:00+03:00",
        "updated_at": "2026-05-26T11:00:00+03:00",
        "status": status,
        "profile": bot._series_bulk_profile_snapshot(profile),
        "result_count": 7,
        "warnings": [],
        "verified_season_range": True,
        "source_result": result,
        "seasons": {
            str(season.season): bot._series_bulk_season_job_entry(season)
            for season in plan.seasons
        },
        "pack_candidates": [],
    }


def _pack_result() -> dict:
    return {
        "title": "Клиника / Scrubs / Сезоны: 1-3 / WEB-DL 1080p / Original / Sub",
        "source": "jackett",
        "tracker_name": "rutracker",
        "torrent_url": "https://jackett.local/dl/pack",
        "seeders": 30,
        "size": "30 GB",
    }


def _fake_series_bulk_and_pending_store(initial: dict | None = None):
    store, jobs = _fake_series_bulk_store(initial)
    pending: dict[str, dict] = {}
    store.load_pending_downloads.side_effect = lambda: copy.deepcopy(pending)

    def save_pending_downloads(updated):
        pending.clear()
        pending.update(copy.deepcopy(updated))

    store.save_pending_downloads.side_effect = save_pending_downloads
    return store, jobs, pending


class SearchDownloadPickTests(unittest.TestCase):
    """search_download_pick — first tap «⬇️ N» on a partial result opens download choices."""

    def test_renders_download_choices(self):
        update = MagicMock(callback_query=_make_query("srch:dl_pick:0"))
        ctx = _make_context()
        asyncio.run(bot.search_download_pick(update, ctx))

        update.callback_query.edit_message_text.assert_awaited_once()
        call = update.callback_query.edit_message_text.await_args
        text = call.args[0]
        kb = call.kwargs.get("reply_markup")
        self.assertIn("Что скачать", text)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertEqual(labels[0], "⬇️ Скачать сейчас + новые серии по мере выхода")
        self.assertIn("⬇️ Скачать только доступные", labels)
        self.assertIn("📦 Скачать, когда сезон завершится", labels)
        self.assertTrue(any("К результатам" in l for l in labels))

    def test_stale_index_returns_error_message(self):
        update = MagicMock(callback_query=_make_query("srch:dl_pick:5"))
        ctx = _make_context(results=[{"title": "X", "partial": True}])
        asyncio.run(bot.search_download_pick(update, ctx))
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Результат недоступен", text)

    def test_escapes_title_in_html_message(self):
        update = MagicMock(callback_query=_make_query("srch:dl_pick:0"))
        ctx = _make_context(results=[{
            "title": "Show <Finale> & S02E01",
            "partial": True,
        }])
        asyncio.run(bot.search_download_pick(update, ctx))

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Show &lt;Finale&gt; &amp; S02E01", text)
        self.assertNotIn("Show <Finale> & S02E01", text)

    def test_full_series_offers_bulk_plan_without_partial_subscription_rows(self):
        update = MagicMock(callback_query=_make_query("srch:dl_pick:0"))
        ctx = _make_context(results=[{
            "title": "Клиника / Scrubs / Сезон: 3 / WEB-DL 1080p",
            "partial": False,
            "series": True,
        }])
        asyncio.run(bot.search_download_pick(update, ctx))

        kb = update.callback_query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("📚 Скачать недостающие сезоны", labels)
        self.assertNotIn("⬇️ Скачать сейчас + новые серии по мере выхода", labels)
        self.assertNotIn("📦 Скачать, когда сезон завершится", labels)

    def test_partial_series_keeps_subscription_rows_and_bulk_plan(self):
        update = MagicMock(callback_query=_make_query("srch:dl_pick:0"))
        ctx = _make_context(results=[{
            "title": "Клиника / Scrubs / Сезон: 3 / Серии: 5 из 8 / WEB-DL 1080p",
            "partial": True,
            "series": True,
            "ep_str": "5/8 эп.",
        }])
        asyncio.run(bot.search_download_pick(update, ctx))

        kb = update.callback_query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("📚 Скачать недостающие сезоны", labels)
        self.assertIn("⬇️ Скачать сейчас + новые серии по мере выхода", labels)
        self.assertIn("📦 Скачать, когда сезон завершится", labels)


class SearchSeriesBulkPlanTests(unittest.TestCase):
    def _open_profile_and_build(self, update, ctx):
        asyncio.run(bot.search_series_bulk_plan(update, ctx))
        update.callback_query.data = "srch:bulk_build"
        return asyncio.run(bot.search_series_bulk_build_plan(update, ctx))

    def test_bulk_plan_opens_profile_settings_before_build(self):
        query = _make_query("srch:bulk_plan:0")
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"

        state = asyncio.run(bot.search_series_bulk_plan(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Что сохраню при подборе", text)
        self.assertIn("Качество: 1080p", text)
        self.assertIn("Original: нужен", text)
        self.assertIn("Субтитры: нужны", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("✅ Собрать план", labels)
        self.assertIn("⚙️ Остальные настройки", labels)
        self.assertIn("⬅️ К выбору", labels)
        self.assertTrue(any("Озвучка: любая из эталона" in label for label in labels))

    def test_bulk_profile_prefers_search_voice_hint_from_reference(self):
        query = _make_query("srch:bulk_plan:0")
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Clinic / Scrubs / S01 / WEB-DL 1080p / LostFilm / NewStudio",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_voice_hints"] = ["LostFilm"]
        ctx.user_data["srch_voice_source"] = "default"

        state = asyncio.run(bot.search_series_bulk_plan(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        profile = ctx.user_data["srch_series_bulk_profile_draft"]
        self.assertEqual(profile.voice_policy, bot.VOICE_ANY_FROM_REFERENCE)
        self.assertEqual(profile.voices, ("LostFilm", "NewStudio"))
        self.assertEqual(profile.preferred_voices, ("LostFilm",))
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("LostFilm", text)

    def test_bulk_profile_keeps_reference_voices_when_voice_hint_is_absent(self):
        query = _make_query("srch:bulk_plan:0")
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Clinic / Scrubs / S01 / WEB-DL 1080p / LostFilm / NewStudio",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_voice_hints"] = ["AlexFilm"]
        ctx.user_data["srch_voice_source"] = "default"

        state = asyncio.run(bot.search_series_bulk_plan(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        profile = ctx.user_data["srch_series_bulk_profile_draft"]
        self.assertEqual(profile.voice_policy, bot.VOICE_ANY_FROM_REFERENCE)
        self.assertEqual(profile.voices, ("LostFilm", "NewStudio"))
        self.assertEqual(profile.preferred_voices, ())

    def test_bulk_profile_voice_accordion_updates_draft(self):
        query = _make_query("srch:bulk_plan:0")
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm / NewStudio",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        asyncio.run(bot.search_series_bulk_plan(update, ctx))

        query.data = "srch:bulk_prof:voice_toggle"
        state = asyncio.run(bot.search_series_bulk_profile_callback(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("☑️ Любая из эталона", labels)
        self.assertIn("⬜ Одна на все сезоны", labels)
        self.assertIn("⬜ Выбрать вручную", labels)

        query.data = "srch:bulk_prof:voice_single"
        state = asyncio.run(bot.search_series_bulk_profile_callback(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        profile = ctx.user_data["srch_series_bulk_profile_draft"]
        self.assertEqual(profile.voice_policy, bot.VOICE_SINGLE_FROM_REFERENCE)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Озвучка: одна на все сезоны", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertNotIn("⬜ Одна на все сезоны", labels)

    def test_bulk_profile_manual_voice_selection_applies_two_voices(self):
        query = _make_query("srch:bulk_plan:0")
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm / NewStudio / AlexFilm",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        asyncio.run(bot.search_series_bulk_plan(update, ctx))

        query.data = "srch:bulk_prof:voice_manual"
        asyncio.run(bot.search_series_bulk_profile_callback(update, ctx))
        query.data = "srch:bulk_prof:voice_pick_0"
        asyncio.run(bot.search_series_bulk_profile_callback(update, ctx))
        query.data = "srch:bulk_prof:voice_pick_1"
        asyncio.run(bot.search_series_bulk_profile_callback(update, ctx))
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("☑️ LostFilm", labels)
        self.assertIn("☑️ NewStudio", labels)
        self.assertIn("💾 Сохранить выбор", labels)
        query.data = "srch:bulk_prof:voice_done"
        state = asyncio.run(bot.search_series_bulk_profile_callback(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        profile = ctx.user_data["srch_series_bulk_profile_draft"]
        self.assertEqual(profile.voice_policy, bot.VOICE_REQUIRE_SELECTED)
        self.assertEqual(profile.voices, ("LostFilm", "NewStudio"))
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Озвучка: выбрано: LostFilm / NewStudio", text)

    def test_bulk_rebuild_returns_to_profile_with_current_settings(self):
        query = _make_query("srch:bulk_rebuild")
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data.update({
            "srch_series_bulk_index": 0,
            "srch_series_bulk_plan": _bulk_plan(seasons=[1], results=results),
            "srch_series_bulk_profile": bot.SeriesBulkProfile(
                quality="any",
                require_original=False,
                require_subs=False,
                voice_policy=bot.VOICE_ANY_RUSSIAN,
            ),
            "srch_series_bulk_results": results,
            "srch_series_bulk_warnings": ("source warning",),
            "srch_series_bulk_resolved": {1: "скачан"},
            "srch_series_bulk_failed": {2: "ошибка"},
            "srch_series_bulk_failed_candidates": {2: 0},
            "srch_series_bulk_review_season": 2,
            "srch_series_bulk_job_id": "old_job",
        })

        state = asyncio.run(bot.search_series_bulk_rebuild(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Что сохраню при подборе", text)
        self.assertIn("Качество: любое", text)
        self.assertIn("Озвучка: любая русская", text)
        self.assertNotIn("srch_series_bulk_plan", ctx.user_data)
        self.assertNotIn("srch_series_bulk_job_id", ctx.user_data)
        self.assertEqual(ctx.user_data["srch_series_bulk_resolved"], {})
        self.assertEqual(ctx.user_data["srch_series_bulk_failed"], {})
        self.assertEqual(ctx.user_data["srch_series_bulk_failed_candidates"], {})

    def test_builds_plan_from_series_result_and_cleans_animation(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [
            {
                "title": "Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / Original / Sub",
                "partial": False,
                "series": True,
                "size": "10 GB",
                "seeders": 20,
                "source": "jackett",
                "tracker_name": "rutracker",
            },
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
                "partial": False,
                "series": True,
                "size": "10 GB",
                "seeders": 20,
                "source": "jackett",
                "tracker_name": "rutracker",
            },
        ]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        gif_msg = MagicMock()
        gif_msg.delete = AsyncMock()
        ctx.bot.send_animation = AsyncMock(return_value=gif_msg)
        kp_client = MagicMock()
        kp_client.search_series_seasons = MagicMock(return_value=2)
        ds = MagicMock()
        ds.list_tasks = MagicMock(return_value=[])
        fake_store, saved_jobs = _fake_series_bulk_store()

        with (
            patch.object(bot, "kinopoisk_client", kp_client),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value={1})),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "state_store", fake_store),
        ):
            state = self._open_profile_and_build(update, ctx)

        self.assertEqual(state, bot.SEARCH_RESULTS)
        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("📚 Скачать недостающие сезоны: Клиника", final_text)
        self.assertIn("Сезон 1 - уже есть в Plex", final_text)
        self.assertIn("Сезон 2 - уверенно: WEB-DL", final_text)
        ctx.bot.send_animation.assert_awaited_once()
        gif_msg.delete.assert_awaited_once()
        kb = query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("⬇️ Скачать уверенные (1)", labels)
        self.assertIn("🔄 Пересобрать план", labels)
        job_id = ctx.user_data["srch_series_bulk_job_id"]
        self.assertIn(job_id, saved_jobs)
        job = saved_jobs[job_id]
        self.assertEqual(job["status"], "planned")
        self.assertEqual(job["series_title"], "Клиника")
        self.assertEqual(job["seasons"]["1"]["plan_status"], bot.STATUS_ALREADY_IN_PLEX)
        self.assertEqual(job["seasons"]["2"]["plan_status"], bot.STATUS_EXACT)
        self.assertEqual(job["seasons"]["2"]["runtime_status"], "pending")

    def test_bulk_plan_marks_good_candidate_as_likely_with_reason(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 0,
            "source": "jackett",
            "tracker_name": "rutracker",
        }
        plan = _bulk_plan(seasons=[1], results=[result])
        text = bot._series_bulk_plan_text(
            plan,
            _bulk_profile(),
            result_count=1,
        )

        self.assertEqual(plan.seasons[0].status, bot.STATUS_GOOD)
        self.assertIn("Сезон 1 - похоже: WEB-DL", text)
        self.assertIn("сидов не видно", text)

    def test_bulk_build_all_seasons_in_plex_finishes_without_saved_job(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
                "partial": False,
                "series": True,
                "size": "10 GB",
                "seeders": 20,
                "source": "jackett",
                "tracker_name": "rutracker",
            },
        ]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.bot.send_animation = AsyncMock(return_value=None)
        fake_store, saved_jobs = _fake_series_bulk_store()

        with (
            patch.object(bot, "kinopoisk_client", MagicMock(search_series_seasons=MagicMock(return_value=2))),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value={1, 2})),
            patch.object(bot, "_series_bulk_downloading_seasons", AsyncMock(return_value=set())),
            patch.object(bot, "_series_bulk_search_once", AsyncMock(return_value=([], []))),
            patch.object(bot, "state_store", fake_store),
        ):
            state = self._open_profile_and_build(update, ctx)

        self.assertEqual(state, bot.ConversationHandler.END)
        self.assertNotIn("srch_series_bulk_job_id", ctx.user_data)
        self.assertEqual(saved_jobs, {})
        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("Все сезоны уже есть в Plex.", final_text)
        self.assertIn("план не сохраняю", final_text)
        self.assertIn("Сезон 1 - уже есть в Plex", final_text)
        self.assertIn("Сезон 2 - уже есть в Plex", final_text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertEqual(labels, [bot.BUTTON_CLOSE])

    def test_bulk_build_cancel_after_season_lookup_stops_without_job(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        gif_msg = MagicMock()
        gif_msg.delete = AsyncMock()
        ctx.bot.send_animation = AsyncMock(return_value=gif_msg)
        asyncio.run(bot.search_series_bulk_plan(update, ctx))
        query.edit_message_text.reset_mock()
        query.data = "srch:bulk_build"
        fake_store, saved_jobs = _fake_series_bulk_store()

        async def known_seasons(_series_query, _results):
            token = ctx.user_data["srch_series_bulk_build_token"]
            ctx.user_data["srch_series_bulk_cancelled_token"] = token
            return [1], True

        with (
            patch.object(bot, "_series_bulk_known_seasons", AsyncMock(side_effect=known_seasons)),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())) as plex,
            patch.object(bot, "_series_bulk_search_once", AsyncMock(return_value=([], []))) as search,
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_build_plan(update, ctx))

        self.assertEqual(state, bot.ConversationHandler.END)
        self.assertNotIn("srch_series_bulk_plan", ctx.user_data)
        self.assertNotIn("srch_series_bulk_job_id", ctx.user_data)
        self.assertEqual(saved_jobs, {})
        plex.assert_not_awaited()
        search.assert_not_awaited()
        ctx.bot.send_animation.assert_awaited_once()
        gif_msg.delete.assert_awaited_once()
        self.assertEqual(query.edit_message_text.await_count, 1)
        self.assertIn("Определяю список сезонов", query.edit_message_text.await_args.args[0])

    def test_bulk_build_shows_long_running_notice(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.bot.send_animation = AsyncMock(return_value=None)
        asyncio.run(bot.search_series_bulk_plan(update, ctx))
        query.edit_message_text.reset_mock()
        query.data = "srch:bulk_build"
        fake_store, _saved_jobs = _fake_series_bulk_store()

        async def known_seasons(_series_query, _results):
            await asyncio.sleep(0.02)
            return [1], True

        with (
            patch.object(bot, "_SERIES_BULK_LONG_NOTICE_SECONDS", 0.001),
            patch.object(bot, "_SERIES_BULK_LONG_NOTICE_INTERVAL_SECONDS", 999),
            patch.object(bot, "_series_bulk_known_seasons", AsyncMock(side_effect=known_seasons)),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())),
            patch.object(bot, "_series_bulk_downloading_seasons", AsyncMock(return_value=set())),
            patch.object(bot, "_series_bulk_search_once", AsyncMock(return_value=([], []))),
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_build_plan(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        notice_calls = [
            call
            for call in query.edit_message_text.await_args_list
            if "Всё ещё собираю план" in call.args[0]
        ]
        self.assertTrue(notice_calls)
        labels = [
            b.text
            for row in notice_calls[0].kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("❌ Отмена", labels)
        self.assertIn("📚 Скачать недостающие сезоны: Клиника", query.edit_message_text.await_args.args[0])

    def test_wide_search_adds_candidates_outside_current_results(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.user_data["srch_jackett_selected"] = {"rutracker"}
        ctx.bot.send_animation = AsyncMock(return_value=None)
        kp_client = MagicMock()
        kp_client.search_series_seasons = MagicMock(return_value=2)
        jackett = MagicMock()
        jackett.search = MagicMock(return_value=[
            _jackett_result("Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / Original / Sub"),
        ])
        ds = MagicMock()
        ds.list_tasks = MagicMock(return_value=[])

        with (
            patch.object(bot, "kinopoisk_client", kp_client),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "rutracker_client", None),
        ):
            self._open_profile_and_build(update, ctx)

        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("Сезон 1 - уверенно: WEB-DL", final_text)
        self.assertIn("Сезон 2 - уверенно: WEB-DL", final_text)
        self.assertIn("Проверено раздач: 2", final_text)
        jackett.search.assert_called_once()
        self.assertEqual(jackett.search.call_args.args[0], "Клиника")

    def test_targeted_search_runs_for_missing_season_after_wide_search(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.user_data["srch_jackett_selected"] = {"rutracker"}
        ctx.bot.send_animation = AsyncMock(return_value=None)
        kp_client = MagicMock()
        kp_client.search_series_seasons = MagicMock(return_value=2)
        jackett = MagicMock()

        def search_side_effect(search_query: str, **_kwargs):
            if search_query == "Клиника Сезон: 2":
                return [_jackett_result(
                    "Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / Original / Sub"
                )]
            return []

        jackett.search = MagicMock(side_effect=search_side_effect)
        ds = MagicMock()
        ds.list_tasks = MagicMock(return_value=[])

        with (
            patch.object(bot, "kinopoisk_client", kp_client),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "rutracker_client", None),
        ):
            self._open_profile_and_build(update, ctx)

        final_text = query.edit_message_text.await_args.args[0]
        called_queries = [call.args[0] for call in jackett.search.call_args_list]
        self.assertEqual(called_queries, ["Клиника", "Клиника Сезон: 2"])
        self.assertIn("Сезон 2 - уверенно: WEB-DL", final_text)

    def test_fetch_limit_supplements_all_needed_seasons_and_keeps_warning_when_targeted_limited(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.user_data["srch_jackett_selected"] = {"rutracker"}
        ctx.bot.send_animation = AsyncMock(return_value=None)
        kp_client = MagicMock()
        kp_client.search_series_seasons = MagicMock(return_value=2)
        jackett = MagicMock()

        def search_side_effect(search_query: str, **_kwargs):
            if search_query == "Клиника Сезон: 1":
                return [_jackett_result(
                    "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub"
                )]
            if search_query == "Клиника Сезон: 2":
                return [_jackett_result(
                    "Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / Original / Sub"
                )]
            return [_jackett_result(
                "Клиника / Scrubs / Сезон: 99 / WEB-DL 1080p / Original / Sub"
            )]

        jackett.search = MagicMock(side_effect=search_side_effect)
        ds = MagicMock()
        ds.list_tasks = MagicMock(return_value=[])

        with (
            patch.object(bot, "JACKETT_FETCH_LIMIT", 1),
            patch.object(bot, "kinopoisk_client", kp_client),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "rutracker_client", None),
        ):
            self._open_profile_and_build(update, ctx)

        final_text = query.edit_message_text.await_args.args[0]
        called_queries = [call.args[0] for call in jackett.search.call_args_list]
        self.assertEqual(called_queries, ["Клиника", "Клиника Сезон: 1", "Клиника Сезон: 2"])
        self.assertIn("План собран не полностью", final_text)
        self.assertIn("Jackett: выдача достигла лимита 1", final_text)
        self.assertIn("Сезон 2 - уверенно: WEB-DL", final_text)

    def test_fetch_limit_warning_is_removed_when_targeted_supplement_is_not_limited(self):
        query = _make_query("srch:bulk_plan:0")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "partial": False,
            "series": True,
            "size": "10 GB",
            "seeders": 20,
            "source": "jackett",
            "tracker_name": "rutracker",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.user_data["srch_jackett_selected"] = {"rutracker"}
        ctx.bot.send_animation = AsyncMock(return_value=None)
        kp_client = MagicMock()
        kp_client.search_series_seasons = MagicMock(return_value=2)
        jackett = MagicMock()

        def search_side_effect(search_query: str, **_kwargs):
            if search_query == "Клиника Сезон: 1":
                return [_jackett_result(
                    "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub"
                )]
            if search_query == "Клиника Сезон: 2":
                return [_jackett_result(
                    "Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / Original / Sub"
                )]
            return [
                _jackett_result("Клиника / Scrubs / Сезон: 98 / WEB-DL 1080p / Original / Sub"),
                _jackett_result("Клиника / Scrubs / Сезон: 99 / WEB-DL 1080p / Original / Sub"),
            ]

        jackett.search = MagicMock(side_effect=search_side_effect)
        ds = MagicMock()
        ds.list_tasks = MagicMock(return_value=[])

        with (
            patch.object(bot, "JACKETT_FETCH_LIMIT", 2),
            patch.object(bot, "kinopoisk_client", kp_client),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value=set())),
            patch.object(bot, "ds_client", ds),
            patch.object(bot, "jackett_client", jackett),
            patch.object(bot, "rutracker_client", None),
        ):
            self._open_profile_and_build(update, ctx)

        final_text = query.edit_message_text.await_args.args[0]
        called_queries = [call.args[0] for call in jackett.search.call_args_list]
        self.assertEqual(called_queries, ["Клиника", "Клиника Сезон: 1", "Клиника Сезон: 2"])
        self.assertNotIn("План собран не полностью", final_text)
        self.assertNotIn("Jackett: выдача достигла лимита 2", final_text)
        self.assertIn("Сезон 2 - уверенно: WEB-DL", final_text)

    def test_bulk_command_lists_saved_plans_for_current_chat(self):
        update = _make_message_update(chat_id=100)
        ctx = _make_context()
        fake_store, _saved_jobs = _fake_series_bulk_store({
            "bulk_test": _series_bulk_job(job_id="bulk_test", chat_id=100),
            "bulk_other": _series_bulk_job(job_id="bulk_other", chat_id=200, title="Чужой"),
        })

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", fake_store),
        ):
            asyncio.run(bot.series_bulk_command(update, ctx))

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Сохранённые планы сезонов", text)
        self.assertIn("Клиника", text)
        self.assertNotIn("Чужой", text)
        kb = update.message.reply_text.await_args.kwargs["reply_markup"]
        buttons = {b.text: b.callback_data for row in kb.inline_keyboard for b in row}
        self.assertEqual(buttons["📚 1. Клиника"], "srch:bulk_open:bulk_test")
        self.assertIn("✖️ Закрыть", buttons)

    def test_bulk_command_hides_cancelled_and_replaced_plans(self):
        update = _make_message_update(chat_id=100)
        ctx = _make_context()
        fake_store, _saved_jobs = _fake_series_bulk_store({
            "bulk_active": _series_bulk_job(job_id="bulk_active", chat_id=100),
            "bulk_cancelled": _series_bulk_job(job_id="bulk_cancelled", chat_id=100, status="cancelled"),
            "bulk_replaced": _series_bulk_job(job_id="bulk_replaced", chat_id=100, status="replaced"),
        })

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", fake_store),
        ):
            asyncio.run(bot.series_bulk_command(update, ctx))

        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Клиника", text)
        kb = update.message.reply_text.await_args.kwargs["reply_markup"]
        buttons = {b.callback_data for row in kb.inline_keyboard for b in row}
        self.assertEqual(buttons, {"srch:bulk_open:bulk_active", "task:close:"})

    def test_search_cancel_marks_active_bulk_job_cancelled(self):
        query = _make_query("srch:cancel")
        query.message = None
        update = MagicMock(callback_query=query)
        ctx = _make_context()
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": _series_bulk_job(job_id="bulk_test", chat_id=100),
        })

        with patch.object(bot, "state_store", fake_store):
            state = asyncio.run(bot.search_cancel(update, ctx))

        self.assertEqual(state, bot.ConversationHandler.END)
        self.assertEqual(saved_jobs["bulk_test"]["status"], "cancelled")
        self.assertNotIn("srch_series_bulk_job_id", ctx.user_data)

    def test_bulk_open_restores_saved_job_context_and_plan_actions(self):
        job = _series_bulk_job()
        query = _make_query("srch:bulk_open:bulk_test")
        query.message = MagicMock()
        query.message.chat = SimpleNamespace(id=100)
        update = MagicMock(callback_query=query)
        update.effective_chat = SimpleNamespace(id=100)
        ctx = _make_context(results=[])
        fake_store, _saved_jobs = _fake_series_bulk_store({"bulk_test": job})

        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", {100}),
            patch.object(bot, "ADMIN_CHAT_IDS", set()),
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_open(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(ctx.user_data["srch_series_bulk_job_id"], "bulk_test")
        restored_plan = ctx.user_data["srch_series_bulk_plan"]
        self.assertEqual(restored_plan.series_title, "Клиника")
        self.assertEqual(ctx.user_data["srch_series_bulk_result_count"], 7)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Открыл сохранённый план", text)
        self.assertIn("Проверено раздач: 7", text)
        self.assertIn("Сезон 1 - уверенно: WEB-DL", text)
        self.assertIn("Сезон 2 - не найдено", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("⬇️ Скачать уверенные (1)", labels)
        self.assertIn("⚙️ Разобрать спорные (1)", labels)

    def test_confirm_screen_lists_only_ready_seasons(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1, 2], results=[result])
        query = _make_query("srch:bulk_confirm")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_confirm(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Будет создано задач: 1", text)
        self.assertIn("Сезон 1 - уверенно: WEB-DL", text)
        self.assertNotIn("Сезон 2 -", text)
        kb = query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("✅ Скачать 1", labels)
        self.assertIn("⬅️ К плану", labels)

    def test_confirm_without_ready_seasons_explains_manual_review(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[2], results=[result])
        query = _make_query("srch:bulk_confirm")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_confirm(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Уверенных сезонов", text)
        self.assertIn("разобрать вручную", text)
        kb = query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("⚙️ Разобрать спорные (1)", labels)

    def test_run_without_ready_seasons_explains_manual_review(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[2], results=[result])
        query = _make_query("srch:bulk_run")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_run(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Уверенных сезонов", text)
        self.assertIn("разобрать вручную", text)

    def test_run_downloads_ready_seasons_and_returns_summary(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        plan = _bulk_plan(seasons=[1, 2], results=[result])
        ctx.user_data["srch_series_bulk_plan"] = plan
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_run")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {"1": {"season": 1}, "2": {"season": 2}},
            },
        })

        with (
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("task_1", "torrent-файл"))) as dl,
            patch.object(bot, "_remember_task_owner") as owner,
            patch.object(bot, "_remember_task_meta") as meta,
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_run(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        dl.assert_awaited_once()
        entry = dl.await_args.args[0]
        self.assertEqual(entry["title"], result["title"])
        owner.assert_called_once_with("task_1", 100)
        meta.assert_called_once()
        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("✅ План обработан", final_text)
        self.assertIn("Добавлено задач: 1", final_text)
        self.assertIn("task_1", final_text)
        self.assertIn("Сезон 1", final_text)
        self.assertIn("Осталось разобрать сезонов: 1", final_text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("⚙️ Разобрать оставшиеся", labels)
        self.assertIn("⬅️ К плану", labels)
        self.assertIn("📚 К списку загрузок", labels)
        job = saved_jobs["bulk_test"]
        self.assertEqual(job["status"], "batch_completed_with_decisions")
        self.assertEqual(job["seasons"]["1"]["runtime_status"], "downloaded")
        self.assertEqual(job["seasons"]["1"]["task_id"], "task_1")
        self.assertEqual(job["seasons"]["1"]["method"], "torrent-файл")

    def test_run_downloads_ignores_duplicate_action_while_running(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=[result])
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_action_running"] = "batch"
        query = _make_query("srch:bulk_run")
        update = MagicMock(callback_query=query)

        with patch.object(bot, "_attempt_pending_download", AsyncMock()) as dl:
            state = asyncio.run(bot.search_series_bulk_run(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        dl.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Уже выполняю скачивание", text)

    def test_run_reports_failed_ready_season(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=[result])
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_run")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs, saved_pending = _fake_series_bulk_and_pending_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {"1": {"season": 1}},
            },
        })

        with (
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", AsyncMock(side_effect=bot.DownloadStationError("no space"))),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_run(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("Добавлено задач: 0", final_text)
        self.assertIn("Требуют решения: 1", final_text)
        self.assertIn("Download Station", final_text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("⚙️ Разобрать ошибки", labels)
        self.assertNotIn("📚 К списку загрузок", labels)
        self.assertIn("Download Station", ctx.user_data["srch_series_bulk_failed"]["1"])
        job = saved_jobs["bulk_test"]
        self.assertEqual(job["status"], "batch_failed")
        self.assertEqual(job["seasons"]["1"]["runtime_status"], "failed")
        self.assertIn("Download Station", job["seasons"]["1"]["error"])
        self.assertEqual(saved_pending, {})

    def test_run_queues_retryable_ready_season(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=[result])
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_run")
        query.message = MagicMock()
        query.message.chat = MagicMock(id=100)
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs, saved_pending = _fake_series_bulk_and_pending_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {"1": {"season": 1}},
            },
        })

        with (
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", AsyncMock(side_effect=bot.JackettError("HTTP 404"))),
            patch.object(bot, "PENDING_DOWNLOADS_ENABLED", True),
            patch.object(bot, "PENDING_DOWNLOADS_INTERVAL_SECONDS", 300),
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_run(update, ctx))

        self.assertEqual(state, bot.ConversationHandler.END)
        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("В очереди на повтор: 1", final_text)
        self.assertIn("Сезон 1", final_text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("📚 К списку загрузок", labels)
        self.assertEqual(len(saved_pending), 1)
        entry_id, entry = next(iter(saved_pending.items()))
        self.assertEqual(entry["title"], result["title"])
        self.assertEqual(entry["series_bulk"]["job_id"], "bulk_test")
        self.assertEqual(entry["series_bulk"]["season"], 1)
        self.assertIn("Jackett не отдал torrent-файл", entry["last_error"])
        self.assertNotIn("404", entry["last_error"])
        self.assertIn("в очереди", ctx.user_data["srch_series_bulk_resolved"]["1"])
        self.assertNotIn("1", ctx.user_data.get("srch_series_bulk_failed", {}))
        job = saved_jobs["bulk_test"]
        self.assertEqual(job["status"], "batch_completed_with_pending")
        self.assertEqual(job["seasons"]["1"]["runtime_status"], "pending_retry")
        self.assertEqual(job["seasons"]["1"]["pending_entry_id"], entry_id)

    def test_failed_ready_season_review_offers_retry(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=[result])
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_failed"] = {"1": "Download Station: no space"}
        ctx.user_data["srch_series_bulk_failed_candidates"] = {"1": 0}
        query = _make_query("srch:bulk_review")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_review(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Сезон 1 - не удалось добавить", text)
        self.assertIn("Download Station: no space", text)
        self.assertIn("Текущий вариант", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("🔄 Попробовать снова", labels)
        self.assertIn("⏭ Пропустить сезон", labels)

    def test_retry_failed_ready_season_clears_error_and_records_task(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=[result])
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_results"] = [result]
        ctx.user_data["srch_series_bulk_failed"] = {"1": "Download Station: no space"}
        ctx.user_data["srch_series_bulk_failed_candidates"] = {"1": 0}
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_retry")
        query.message.chat.id = 100
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "batch_failed",
                "seasons": {
                    "1": {
                        "season": 1,
                        "runtime_status": "failed",
                        "error": "Download Station: no space",
                    },
                },
            },
        })

        with (
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("task_2", "torrent-файл"))),
            patch.object(bot, "_remember_task_owner") as owner,
            patch.object(bot, "_remember_task_meta") as meta,
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_retry(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(ctx.user_data["srch_series_bulk_failed"], {})
        self.assertIn("скачан после повтора", ctx.user_data["srch_series_bulk_resolved"]["1"])
        owner.assert_called_once_with("task_2", 100)
        meta.assert_called_once()
        entry = saved_jobs["bulk_test"]["seasons"]["1"]
        self.assertEqual(entry["runtime_status"], "downloaded")
        self.assertEqual(entry["task_id"], "task_2")
        self.assertNotIn("error", entry)

    def test_plan_keyboard_offers_manual_review_for_disputed_season(self):
        results = [
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
                "source": "jackett",
                "tracker_name": "rutracker",
                "seeders": 20,
                "size": "10 GB",
            },
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / NewStudio",
                "source": "jackett",
                "tracker_name": "rutracker",
                "seeders": 20,
                "size": "10 GB",
            },
        ]
        plan = _bulk_plan(seasons=[1], results=results)

        kb = bot._series_bulk_plan_keyboard(plan, {})
        labels = [b.text for row in kb.inline_keyboard for b in row]

        self.assertIn("⚙️ Разобрать спорные (1)", labels)

    def test_plan_keyboard_offers_pack_list_when_season_pack_found(self):
        plan = _bulk_plan(seasons=[1, 2, 3], results=[_pack_result()])

        kb = bot._series_bulk_plan_keyboard(plan, {})
        labels = [b.text for row in kb.inline_keyboard for b in row]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]

        self.assertIn("📦 Показать паки сезонов (1)", labels)
        self.assertIn("srch:bulk_packs", callbacks)

    def test_pack_list_and_confirm_are_manual_choice_screens(self):
        plan = _bulk_plan(seasons=[1, 2, 3], results=[_pack_result()])
        ctx = _make_context(results=[_pack_result()])
        ctx.user_data["srch_series_bulk_plan"] = plan
        query = _make_query("srch:bulk_packs")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_pack_list(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Паки сезонов", text)
        self.assertIn("не выбираю её автоматически", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("📦 Выбрать пак 1", labels)

        query = _make_query("srch:bulk_pack_confirm:0")
        update = MagicMock(callback_query=query)
        ctx.user_data["srch_series_bulk_plan"] = plan
        state = asyncio.run(bot.search_series_bulk_pack_confirm(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Скачать пак сезонов", text)
        self.assertIn("Скачаю эту раздачу одним торрентом", text)
        self.assertIn("сезоны 1-3", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("✅ Скачать пак", labels)
        self.assertIn("⬅️ К пакам", labels)

    def test_pack_download_marks_covered_seasons_resolved(self):
        pack = _pack_result()
        plan = _bulk_plan(seasons=[1, 2, 3], results=[pack])
        ctx = _make_context(results=[pack])
        ctx.user_data["srch_series_bulk_plan"] = plan
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_results"] = [pack]
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_pack_run:0")
        query.message.chat.id = 100
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {
                    str(season.season): bot._series_bulk_season_job_entry(season)
                    for season in plan.seasons
                },
                "pack_candidates": [pack],
            },
        })

        with (
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("task_pack", "torrent-файл"))) as dl,
            patch.object(bot, "_remember_task_owner") as owner,
            patch.object(bot, "_remember_task_meta") as meta,
            patch.object(bot, "state_store", fake_store),
        ):
            state = asyncio.run(bot.search_series_bulk_pack_run(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        dl.assert_awaited_once()
        self.assertEqual(dl.await_args.args[0]["title"], pack["title"])
        owner.assert_called_once_with("task_pack", 100)
        meta.assert_called_once()
        self.assertEqual(ctx.user_data["srch_series_bulk_resolved"]["1"], "скачан паком: task_pack")
        self.assertEqual(ctx.user_data["srch_series_bulk_resolved"]["2"], "скачан паком: task_pack")
        self.assertEqual(ctx.user_data["srch_series_bulk_resolved"]["3"], "скачан паком: task_pack")
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Пак добавлен: task_pack", text)
        self.assertIn("Сезоны 1-3 пометил", text)
        self.assertIn("Можно скачать после подтверждения: 0", text)
        job = saved_jobs["bulk_test"]
        self.assertEqual(job["status"], "pack_downloaded")
        self.assertEqual(job["seasons"]["1"]["runtime_status"], "pack_downloaded")
        self.assertEqual(job["seasons"]["2"]["runtime_status"], "pack_downloaded")
        self.assertEqual(job["seasons"]["3"]["runtime_status"], "pack_downloaded")
        self.assertEqual(job["pack_downloads"][0]["task_id"], "task_pack")
        self.assertEqual(job["pack_downloads"][0]["season_range"], [1, 3])

    def test_pack_download_ignores_duplicate_action_while_running(self):
        pack = _pack_result()
        plan = _bulk_plan(seasons=[1, 2, 3], results=[pack])
        ctx = _make_context(results=[pack])
        ctx.user_data["srch_series_bulk_plan"] = plan
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_action_running"] = "pack"
        query = _make_query("srch:bulk_pack_run:0")
        update = MagicMock(callback_query=query)

        with patch.object(bot, "_attempt_pending_download", AsyncMock()) as dl:
            state = asyncio.run(bot.search_series_bulk_pack_run(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        dl.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Уже выполняю скачивание", text)

    def test_missing_season_can_be_opened_for_soft_search(self):
        results = [{
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
            "source": "jackett",
            "tracker_name": "rutracker",
            "seeders": 20,
            "size": "10 GB",
        }]
        ctx = _make_context(results=results)
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[2], results=results)
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        query = _make_query("srch:bulk_review")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_review(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Сезон 2 - не найдено", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("🔄 Искать мягче", labels)

    def test_review_screen_lists_disputed_candidates(self):
        results = [
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
                "source": "jackett",
                "tracker_name": "rutracker",
                "seeders": 20,
                "size": "10 GB",
            },
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / NewStudio",
                "source": "jackett",
                "tracker_name": "rutracker",
                "seeders": 20,
                "size": "10 GB",
            },
        ]
        ctx = _make_context(results=results)
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=results)
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        query = _make_query("srch:bulk_review")
        update = MagicMock(callback_query=query)

        state = asyncio.run(bot.search_series_bulk_review(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Сезон 1 - нужно проверить", text)
        self.assertIn("несколько вариантов слишком близки", text)
        self.assertIn("LostFilm", text)
        self.assertIn("NewStudio", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("⬇️ Скачать 1", labels)
        self.assertIn("🔄 Искать мягче", labels)
        self.assertIn("⏭ Пропустить сезон", labels)

    def test_soft_search_adds_manual_candidate_without_changing_profile(self):
        initial = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
            "source": "jackett",
            "tracker_name": "rutracker",
            "seeders": 20,
            "size": "10 GB",
        }
        found = {
            "title": "Клиника / Scrubs / Сезон: 2 / WEBRip 720p / BaibaKo",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/soft",
            "seeders": 12,
            "size": "8 GB",
        }
        profile = _bulk_profile()
        ctx = _make_context(results=[initial])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[2], results=[initial])
        ctx.user_data["srch_series_bulk_profile"] = profile
        ctx.user_data["srch_series_bulk_results"] = [initial]
        query = _make_query("srch:bulk_soft")
        update = MagicMock(callback_query=query)

        async def search_once(search_query, *_args, **_kwargs):
            if search_query.endswith("S02"):
                return [found], []
            return [], []

        with patch.object(bot, "_series_bulk_search_once", AsyncMock(side_effect=search_once)) as search:
            state = asyncio.run(bot.search_series_bulk_soft_search(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        self.assertEqual(search.await_count, 3)
        self.assertIs(ctx.user_data["srch_series_bulk_profile"], profile)
        updated = ctx.user_data["srch_series_bulk_plan"].seasons[0]
        self.assertEqual(updated.status, bot.STATUS_NEEDS_DECISION)
        self.assertEqual(updated.candidates[0].result["title"], found["title"])
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("нашёл варианты шире", text)
        self.assertIn("WEBRip", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("⬇️ Скачать 1", labels)

    def test_bulk_search_rutracker_fallback_has_timeout_warning(self):
        ctx = _make_context(results=[])
        rutracker = MagicMock()

        async def timeout_wait_for(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError

        with (
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "rutracker_client", rutracker),
            patch.object(bot.asyncio, "wait_for", timeout_wait_for),
        ):
            results, warnings = asyncio.run(bot._series_bulk_search_once(ctx, "Клиника"))

        self.assertEqual(results, [])
        self.assertIn("Rutracker: не ответил вовремя", warnings[0])

    def test_bulk_search_warning_hides_raw_tracker_error(self):
        ctx = _make_context(results=[])
        rutracker = MagicMock()
        rutracker.search.side_effect = bot.RutrackerError("HTTP 503 stack trace")

        with (
            patch.object(bot, "jackett_client", None),
            patch.object(bot, "rutracker_client", rutracker),
        ):
            results, warnings = asyncio.run(bot._series_bulk_search_once(ctx, "Клиника"))

        self.assertEqual(results, [])
        self.assertIn("Rutracker: временно недоступен", warnings[0])
        self.assertNotIn("HTTP 503", warnings[0])

    def test_skip_manual_review_marks_season_resolved_and_returns_to_plan(self):
        results = [
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
                "source": "jackett",
                "tracker_name": "rutracker",
                "seeders": 20,
                "size": "10 GB",
            },
            {
                "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / NewStudio",
                "source": "jackett",
                "tracker_name": "rutracker",
                "seeders": 20,
                "size": "10 GB",
            },
        ]
        ctx = _make_context(results=results)
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=results)
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_results"] = results
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_skip")
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {"1": {"season": 1}},
            },
        })

        with patch.object(bot, "state_store", fake_store):
            asyncio.run(bot.search_series_bulk_skip(update, ctx))

        self.assertEqual(ctx.user_data["srch_series_bulk_resolved"], {"1": "пропущен"})
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Решено вручную", text)
        self.assertIn("Сезон 1 - пропущен", text)
        self.assertIn("Нужно решение: 0", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertNotIn("⚙️ Разобрать спорные (1)", labels)
        self.assertEqual(saved_jobs["bulk_test"]["seasons"]["1"]["runtime_status"], "skipped")

    def test_candidate_download_marks_disputed_season_resolved(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / LostFilm",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/1",
            "seeders": 20,
            "size": "10 GB",
        }
        other = {
            "title": "Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / Original / Sub / NewStudio",
            "source": "jackett",
            "tracker_name": "rutracker",
            "torrent_url": "https://jackett.local/dl/2",
            "seeders": 20,
            "size": "10 GB",
        }
        ctx = _make_context(results=[result, other])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[1], results=[result, other])
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_results"] = [result, other]
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_cand_dl:0")
        query.message.chat.id = 100
        update = MagicMock(callback_query=query)
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {"1": {"season": 1}},
            },
        })

        with (
            patch.object(bot, "_check_disk_space_for_download", return_value=None),
            patch.object(bot, "_attempt_pending_download", AsyncMock(return_value=("task_1", "torrent-файл"))) as dl,
            patch.object(bot, "_remember_task_owner") as owner,
            patch.object(bot, "_remember_task_meta") as meta,
            patch.object(bot, "state_store", fake_store),
        ):
            asyncio.run(bot.search_series_bulk_candidate_download(update, ctx))

        dl.assert_awaited_once()
        self.assertEqual(dl.await_args.args[0]["title"], result["title"])
        owner.assert_called_once_with("task_1", 100)
        meta.assert_called_once()
        self.assertIn("скачан вручную", ctx.user_data["srch_series_bulk_resolved"]["1"])
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("добавил задачу task_1", text)
        self.assertIn("Нужно решение: 0", text)
        entry = saved_jobs["bulk_test"]["seasons"]["1"]
        self.assertEqual(entry["runtime_status"], "downloaded")
        self.assertEqual(entry["task_id"], "task_1")
        self.assertIn("скачан вручную", entry["summary"])

    def test_partial_review_offers_download_and_subscription_actions(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 2 / Серии: 1-5 из 8 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "url": "https://tracker.local/topic/2",
            "seeders": 20,
            "size": "10 GB",
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[2], results=[result])
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        query = _make_query("srch:bulk_review")
        update = MagicMock(callback_query=query)

        asyncio.run(bot.search_series_bulk_review(update, ctx))

        text = query.edit_message_text.await_args.args[0]
        self.assertIn("найден неполный сезон", text)
        self.assertIn("5/8", text)
        labels = [
            b.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("⬇️ Скачать доступные серии", labels)
        self.assertIn("⬇️ Скачать доступные + новые по мере выхода", labels)
        self.assertIn("📦 Скачать, когда сезон завершится", labels)
        self.assertIn("🔔 Только уведомлять", labels)
        self.assertIn("🎯 Сообщить о финале", labels)

    def test_partial_finale_action_creates_subscription_and_returns_to_plan(self):
        result = {
            "title": "Клиника / Scrubs / Сезон: 2 / Серии: 1-5 из 8 / WEB-DL 1080p / Original / Sub",
            "source": "jackett",
            "tracker_name": "rutracker",
            "url": "https://tracker.local/topic/2",
            "seeders": 20,
            "size": "10 GB",
        }
        ctx = _make_context(results=[result])
        ctx.user_data["srch_search_query"] = "Клиника 1080p Original Sub"
        ctx.user_data["srch_series_bulk_plan"] = _bulk_plan(seasons=[2], results=[result])
        ctx.user_data["srch_series_bulk_profile"] = _bulk_profile()
        ctx.user_data["srch_series_bulk_results"] = [result]
        ctx.user_data["srch_series_bulk_job_id"] = "bulk_test"
        query = _make_query("srch:bulk_partial:after")
        query.message.chat.id = 100
        update = MagicMock(callback_query=query)
        saved = {}
        fake_store, saved_jobs = _fake_series_bulk_store({
            "bulk_test": {
                "id": "bulk_test",
                "status": "planned",
                "seasons": {"2": {"season": 2}},
            },
        })
        fake_store.load_topic_subscriptions.return_value = {}
        fake_store.save_topic_subscriptions.side_effect = lambda subs: saved.update(subs)

        with patch.object(bot, "state_store", fake_store):
            asyncio.run(bot.search_series_bulk_partial_action(update, ctx))

        self.assertEqual(len(saved), 1)
        sub = next(iter(saved.values()))
        self.assertEqual(sub["query"], "Клиника 1080p Original Sub")
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)
        self.assertEqual(ctx.user_data["srch_series_bulk_resolved"]["2"], "подписка: скачать сезон после финала")
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("скачать сезон после финала", text)
        self.assertIn("Нужно решение: 0", text)
        entry = saved_jobs["bulk_test"]["seasons"]["2"]
        self.assertEqual(entry["runtime_status"], "subscribed")
        self.assertEqual(entry["subscription"]["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)


class SearchSubscribePickTests(unittest.TestCase):
    """search_subscribe_pick — first tap «🔔 N» opens notification-only choices."""

    def test_renders_notification_choices(self):
        update = MagicMock(callback_query=_make_query("srch:sub_pick:0"))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_pick(update, ctx))

        kb = update.callback_query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("🔔 Уведомлять о новых сериях", labels)
        self.assertIn("🎯 Сообщить, когда сезон завершится", labels)
        self.assertTrue(any("К результатам" in l for l in labels))

    def test_escapes_title_in_html_message(self):
        update = MagicMock(callback_query=_make_query("srch:sub_pick:0"))
        ctx = _make_context(results=[{
            "title": "Show <Finale> & S02E01",
            "partial": True,
        }])
        asyncio.run(bot.search_subscribe_pick(update, ctx))

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Show &lt;Finale&gt; &amp; S02E01", text)
        self.assertNotIn("Show <Finale> & S02E01", text)


class SearchSubscribePresetTests(unittest.TestCase):
    """search_subscribe_preset — commit one of the branch options."""

    def _drive_preset(self, code: str):
        update = MagicMock(callback_query=_make_query(f"srch:sub_preset:0:{code}"))
        ctx = _make_context()
        captured = {}

        async def fake_download_and_add(query, context, index, **kw):
            captured["index"] = index
            captured["kwargs"] = kw
            return 0  # ConversationHandler state

        with patch.object(bot, "_download_and_add", side_effect=fake_download_and_add):
            asyncio.run(bot.search_subscribe_preset(update, ctx))
        return captured

    def test_each_preset_downloads_now_and_subscribes_to_updates(self):
        c = self._drive_preset("each")
        self.assertTrue(c["kwargs"]["subscribe"])
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def _drive_subscribe_only(self, code: str):
        update = MagicMock(callback_query=_make_query(f"srch:sub_preset:0:{code}"))
        ctx = _make_context()
        captured = {}

        async def fake_create(query, context, index, **kw):
            captured["index"] = index
            captured["kwargs"] = kw
            return 0

        with (
            patch.object(bot, "_download_and_add", AsyncMock()) as dl,
            patch.object(bot, "_create_subscription_only", side_effect=fake_create),
        ):
            asyncio.run(bot.search_subscribe_preset(update, ctx))
        dl.assert_not_awaited()
        return captured

    def test_after_finale_preset_waits_for_complete_season_without_current_download(self):
        c = self._drive_subscribe_only("after")
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)

    def test_notify_preset_is_notify_only_each_update(self):
        c = self._drive_subscribe_only("notify")
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_NOTIFY_ONLY)

    def test_final_preset_is_notify_only_final(self):
        c = self._drive_subscribe_only("final")
        self.assertEqual(c["kwargs"]["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(c["kwargs"]["download_policy"], DOWNLOAD_NOTIFY_ONLY)

    def test_unknown_preset_returns_error(self):
        update = MagicMock(callback_query=_make_query("srch:sub_preset:0:bogus"))
        ctx = _make_context()
        with patch.object(bot, "_download_and_add", AsyncMock()) as dl:
            asyncio.run(bot.search_subscribe_preset(update, ctx))
        dl.assert_not_awaited()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Неизвестный", text)

    def test_subscribe_only_creates_jackett_subscription_without_downloading(self):
        update = MagicMock(callback_query=_make_query("srch:sub_preset:0:after"))
        update.callback_query.message.chat.id = 100
        ctx = _make_context(results=[{
            "source": "jackett",
            "title": "Show S1E1-8 of 10",
            "url": "https://tracker.local/topic/1",
            "tracker_name": "kinozal",
            "partial": True,
        }])
        ctx.user_data["srch_search_query"] = "Show 1080p"
        saved = {}
        fake_store = MagicMock()
        fake_store.load_topic_subscriptions.return_value = {}
        fake_store.save_topic_subscriptions.side_effect = lambda subs: saved.update(subs)

        with (
            patch.object(bot, "state_store", fake_store),
            patch.object(bot, "_download_and_add", AsyncMock()) as dl,
        ):
            asyncio.run(bot.search_subscribe_preset(update, ctx))

        dl.assert_not_awaited()
        self.assertEqual(len(saved), 1)
        sub = next(iter(saved.values()))
        self.assertEqual(sub["type"], "jackett")
        self.assertEqual(sub["query"], "Show 1080p")
        self.assertEqual(sub["last_episode_end"], 8)
        self.assertEqual(sub["total_episodes"], 10)
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Текущую неполную раздачу не скачиваю", text)
        labels = [
            b.text
            for row in update.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("✖️ Закрыть", labels)


class SearchSubscribeAdvancedFlowTests(unittest.TestCase):
    """Advanced 2-step menu: notify → download → commit."""

    def test_step1_renders_notify_options(self):
        update = MagicMock(callback_query=_make_query("srch:sub_advanced:0"))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_advanced(update, ctx))

        call = update.callback_query.edit_message_text.await_args
        text = call.args[0]
        kb = call.kwargs.get("reply_markup")
        self.assertIn("Шаг 1", text)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        # Three notify choices + back.
        self.assertTrue(any("каждой" in l for l in labels))
        self.assertTrue(any("сезон завершится" in l for l in labels))
        self.assertTrue(any("Не уведомлять" in l for l in labels))

    def test_step1_escapes_title_in_html_message(self):
        update = MagicMock(callback_query=_make_query("srch:sub_advanced:0"))
        ctx = _make_context(results=[{
            "title": "Show <Finale> & S02E01",
            "partial": True,
        }])
        asyncio.run(bot.search_subscribe_advanced(update, ctx))

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Show &lt;Finale&gt; &amp; S02E01", text)
        self.assertNotIn("Show <Finale> & S02E01", text)

    def test_step1_to_step2_carries_notify_choice(self):
        """After picking notify_policy in step 1, step 2 must show the
        downstream choice and remember the upstream pick via user_data."""
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_notify:0:{NOTIFY_FINAL_ONLY}"
        ))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_set_notify(update, ctx))

        self.assertEqual(
            ctx.user_data.get("srch_sub_notify_policy"), NOTIFY_FINAL_ONLY,
        )
        call = update.callback_query.edit_message_text.await_args
        text = call.args[0]
        kb = call.kwargs.get("reply_markup")
        self.assertIn("Шаг 2", text)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        # Step 2 must offer the only_when_complete option.
        self.assertTrue(any("Когда сезон завершится" in l for l in labels))

    def test_step2_escapes_title_in_html_message(self):
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_notify:0:{NOTIFY_FINAL_ONLY}"
        ))
        ctx = _make_context(results=[{
            "title": "Show <Finale> & S02E01",
            "partial": True,
        }])
        asyncio.run(bot.search_subscribe_set_notify(update, ctx))

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Show &lt;Finale&gt; &amp; S02E01", text)
        self.assertNotIn("Show <Finale> & S02E01", text)

    def test_silent_notify_hides_notify_only_download_choice(self):
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_notify:0:{NOTIFY_SILENT}"
        ))
        ctx = _make_context()
        asyncio.run(bot.search_subscribe_set_notify(update, ctx))

        kb = update.callback_query.edit_message_text.await_args.kwargs.get("reply_markup")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertFalse(any("Не скачивать автоматически" in l for l in labels))

    def test_step2_commits_with_both_axes(self):
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_download:0:{DOWNLOAD_ONLY_WHEN_COMPLETE}"
        ))
        ctx = _make_context()
        # Simulate step 1 having stashed the notify choice.
        ctx.user_data["srch_sub_notify_policy"] = NOTIFY_SILENT

        captured = {}

        async def fake_download_and_add(query, context, index, **kw):
            captured.update(kw)
            return 0

        with patch.object(bot, "_download_and_add", side_effect=fake_download_and_add):
            asyncio.run(bot.search_subscribe_set_download(update, ctx))

        self.assertTrue(captured["subscribe"])
        self.assertEqual(captured["notify_policy"], NOTIFY_SILENT)
        self.assertEqual(captured["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)
        # Notify stash should be popped to prevent leakage into next session.
        self.assertNotIn("srch_sub_notify_policy", ctx.user_data)

    def test_step2_rejects_silent_notify_only_pair(self):
        update = MagicMock(callback_query=_make_query(
            f"srch:sub_set_download:0:{DOWNLOAD_NOTIFY_ONLY}"
        ))
        ctx = _make_context()
        ctx.user_data["srch_sub_notify_policy"] = NOTIFY_SILENT

        with patch.object(bot, "_download_and_add", AsyncMock()) as dl:
            asyncio.run(bot.search_subscribe_set_download(update, ctx))

        dl.assert_not_awaited()
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("ничего не делает", text)
        self.assertEqual(ctx.user_data.get("srch_sub_notify_policy"), NOTIFY_SILENT)


class SearchSubscribeBackToResultsTests(unittest.TestCase):
    """«⬅️ К результатам» must re-render the results keyboard."""

    def test_rerenders_when_results_present(self):
        update = MagicMock(callback_query=_make_query("srch:sub_back_results:0"))
        ctx = _make_context(results=[{"title": "Test", "partial": True}])
        ctx.user_data["srch_results_page"] = 0

        with patch.object(bot, "_build_results_text", return_value="🔎 results"):
            asyncio.run(bot.search_subscribe_back_to_results(update, ctx))

        # Edit was called with a keyboard (re-rendered results screen).
        call = update.callback_query.edit_message_text.await_args
        self.assertIsNotNone(call.kwargs.get("reply_markup"))

    def test_preserves_banner_page_and_source_buttons(self):
        update = MagicMock(callback_query=_make_query("srch:sub_back_results:0"))
        results = [{"title": f"Test {i}", "partial": True} for i in range(6)]
        ctx = _make_context(results=results)
        ctx.user_data.update({
            "srch_results_page": 1,
            "srch_search_query": "Test Show 1080p",
            "srch_banner": "⚙️ Показаны сериалы",
            "srch_source": "jackett",
            "srch_cluster_picker_return": True,
        })

        with (
            patch.object(bot, "jackett_client", object()),
            patch.object(bot, "rutracker_client", object()),
            patch.object(bot, "_build_results_text", return_value="🔎 results") as build_text,
        ):
            asyncio.run(bot.search_subscribe_back_to_results(update, ctx))

        build_text.assert_called_once_with(
            results,
            "Test Show 1080p",
            1,
            banner="⚙️ Показаны сериалы",
        )
        call = update.callback_query.edit_message_text.await_args
        labels = [
            button.text
            for row in call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("🔄 Сменить трекеры", labels)
        self.assertIn("🔗 Rutracker напрямую", labels)
        self.assertIn("⬅️ К вариантам", labels)

    def test_preserves_movie_discovery_back_button(self):
        update = MagicMock(callback_query=_make_query("srch:sub_back_results:0"))
        ctx = _make_context(results=[{"title": "Movie release", "partial": True}])
        ctx.user_data.update({
            "srch_search_query": "Movie 2026",
            "srch_banner": "🎬 Раздачи по выбранной новинке",
            "srch_source": "movie_discovery",
        })

        with patch.object(bot, "_build_results_text", return_value="🔎 results"):
            asyncio.run(bot.search_subscribe_back_to_results(update, ctx))

        call = update.callback_query.edit_message_text.await_args
        labels = [
            button.text
            for row in call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("🎬 ← Новинки", labels)

    def test_missing_results_graceful_error(self):
        update = MagicMock(callback_query=_make_query("srch:sub_back_results:0"))
        ctx = _make_context(results=[])
        asyncio.run(bot.search_subscribe_back_to_results(update, ctx))
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Результаты потеряны", text)


if __name__ == "__main__":
    unittest.main()
