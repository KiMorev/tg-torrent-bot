"""Tests for partial-series download/notification pickers in bot.py."""
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
        self.assertIn("⬇️ Скачать сейчас", labels)
        self.assertIn("⬇️ Скачать сейчас + новые серии по мере выхода", labels)
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

        with (
            patch.object(bot, "kinopoisk_client", kp_client),
            patch.object(bot, "_get_plex_seasons_for_series", AsyncMock(return_value={1})),
            patch.object(bot, "ds_client", ds),
        ):
            state = asyncio.run(bot.search_series_bulk_plan(update, ctx))

        self.assertEqual(state, bot.SEARCH_RESULTS)
        final_text = query.edit_message_text.await_args.args[0]
        self.assertIn("📚 Скачать недостающие сезоны: Клиника", final_text)
        self.assertIn("Сезон 1 - уже есть в Plex", final_text)
        self.assertIn("Сезон 2 - WEB-DL", final_text)
        ctx.bot.send_animation.assert_awaited_once()
        gif_msg.delete.assert_awaited_once()


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
