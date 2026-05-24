"""Tests for R.2 — Plex pre-existence по сезонам.

Covers:
* PlexClient.get_show_seasons_lite — selective resolution fetching
* _plex_ensure_show_seasons_lite — smart cache with top-up for missing focus
* _plex_other_seasons_context + _format_other_seasons_context — context render
* _plex_series_confirm_text — context block injected into confirm dialog
* _plex_confirm_keyboard(show_upgrade=True) — upgrade button added
* plex_upgrade_download — handler logs old rating_key, downloads new version
* _maybe_prewarm_plex_for_results — fires pre-warm for partial-season results
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
from plex import (
    PlexClient, PlexSeason, PlexShow, check_before_download_season,
)


# ─── PlexClient.get_show_seasons_lite ──────────────────────────────────


class GetShowSeasonsLiteTests(unittest.TestCase):
    """Selective resolution fetching: fetch episodes ONLY for the listed seasons."""

    def _client(self) -> PlexClient:
        return PlexClient("http://plex.local:32400", "tok")

    def _fake_seasons_xml(self) -> "ElementTree.Element":  # noqa: F821
        from xml.etree import ElementTree
        root = ElementTree.Element("MediaContainer")
        for n, ep in [(1, 8), (2, 10), (3, 12)]:
            d = ElementTree.SubElement(root, "Directory")
            d.set("index", str(n))
            d.set("ratingKey", f"key{n}")
            d.set("leafCount", str(ep))
        return root

    def test_all_seasons_fetched_when_filter_is_none(self):
        c = self._client()
        with (
            patch.object(c, "_get", return_value=self._fake_seasons_xml()),
            patch.object(c, "_fetch_season_episode_files",
                         return_value=([], "1080")) as fetch_files,
        ):
            seasons = c.get_show_seasons_lite("show1", fetch_resolution_for=None)
        # 3 seasons → 3 file-fetch calls.
        self.assertEqual(fetch_files.call_count, 3)
        self.assertEqual(seasons[1].resolution, "1080")
        self.assertEqual(seasons[2].resolution, "1080")

    def test_empty_list_skips_all_file_fetches(self):
        c = self._client()
        with (
            patch.object(c, "_get", return_value=self._fake_seasons_xml()),
            patch.object(c, "_fetch_season_episode_files") as fetch_files,
        ):
            seasons = c.get_show_seasons_lite("show1", fetch_resolution_for=[])
        # Zero per-season fetches — just the seasons-list call.
        fetch_files.assert_not_called()
        self.assertEqual(set(seasons.keys()), {1, 2, 3})
        # Episode counts populated from leafCount; resolution empty.
        self.assertEqual(seasons[2].episode_count, 10)
        self.assertEqual(seasons[2].resolution, "")

    def test_focus_list_fetches_only_listed_seasons(self):
        c = self._client()

        # Different resolution per season so we can verify the right one
        # ran through the file-fetch call.
        def fake_fetch(season_key):
            return ([], {"key2": "2160"}.get(season_key, "1080"))

        with (
            patch.object(c, "_get", return_value=self._fake_seasons_xml()),
            patch.object(c, "_fetch_season_episode_files",
                         side_effect=fake_fetch) as fetch_files,
        ):
            seasons = c.get_show_seasons_lite("show1", fetch_resolution_for=[2])
        # Only one fetch call (for season 2's key).
        fetch_files.assert_called_once_with("key2")
        self.assertEqual(seasons[2].resolution, "2160")
        # Other seasons populated with metadata only, no resolution.
        self.assertEqual(seasons[1].resolution, "")
        self.assertEqual(seasons[3].resolution, "")

    def test_empty_key_returns_empty(self):
        c = self._client()
        self.assertEqual(c.get_show_seasons_lite("", fetch_resolution_for=[1]), {})


# ─── _plex_ensure_show_seasons_lite ────────────────────────────────────


class EnsureShowSeasonsLiteTests(unittest.IsolatedAsyncioTestCase):
    """The bot-level wrapper that adds caching + top-up semantics."""

    def _show(self, *, with_focus: bool = False, focus_num: int = 2) -> PlexShow:
        # If with_focus, season already has a resolution; otherwise empty.
        seasons = {
            1: PlexSeason("k1", 1, 8, [], ""),
            focus_num: PlexSeason(
                f"k{focus_num}", focus_num, 10, [],
                "1080" if with_focus else "",
            ),
        }
        return PlexShow("Test Show", 2024, "showkey", seasons=seasons)

    async def test_returns_cached_when_focus_already_has_resolution(self):
        show = self._show(with_focus=True)
        fake_client = MagicMock()
        with patch.object(bot, "plex_client", fake_client):
            res = await bot._plex_ensure_show_seasons_lite(show, focus_season=2)
        self.assertEqual(res[2].resolution, "1080")
        fake_client.get_show_seasons_lite.assert_not_called()

    async def test_returns_cached_when_focus_is_none(self):
        show = self._show(with_focus=False)
        fake_client = MagicMock()
        with patch.object(bot, "plex_client", fake_client):
            res = await bot._plex_ensure_show_seasons_lite(show, focus_season=None)
        # No top-up: focus_season None means «context only».
        self.assertEqual(res, show.seasons)
        fake_client.get_show_seasons_lite.assert_not_called()

    async def test_top_up_fires_when_focus_season_missing_resolution(self):
        """Cache hit on focus key, but resolution is empty → 1 extra request."""
        show = self._show(with_focus=False)
        fake_client = MagicMock()
        # Top-up returns the same season key, this time with a resolution.
        fake_client.get_show_seasons_lite.return_value = {
            2: PlexSeason("k2", 2, 10, [], "2160"),
        }
        with patch.object(bot, "plex_client", fake_client):
            res = await bot._plex_ensure_show_seasons_lite(show, focus_season=2)
        fake_client.get_show_seasons_lite.assert_called_once_with(
            "showkey", fetch_resolution_for=[2]
        )
        # The top-up merged into the show's cached dict.
        self.assertEqual(res[2].resolution, "2160")

    async def test_cold_cache_fetches_focus_only(self):
        show = PlexShow("Test Show", 2024, "showkey", seasons={})
        fake_client = MagicMock()
        fake_client.get_show_seasons_lite.return_value = {
            1: PlexSeason("k1", 1, 8, [], ""),
            2: PlexSeason("k2", 2, 10, [], "1080"),
            3: PlexSeason("k3", 3, 12, [], ""),
        }
        with patch.object(bot, "plex_client", fake_client):
            res = await bot._plex_ensure_show_seasons_lite(show, focus_season=2)
        fake_client.get_show_seasons_lite.assert_called_once_with(
            "showkey", fetch_resolution_for=[2]
        )
        self.assertEqual(res[2].resolution, "1080")
        # The cache was populated for future calls.
        self.assertEqual(show.seasons[2].resolution, "1080")

    async def test_top_up_failure_keeps_existing_cache(self):
        """Network failure during top-up shouldn't break the helper —
        existing cache is returned so the confirm dialog still renders."""
        show = self._show(with_focus=False)
        fake_client = MagicMock()
        fake_client.get_show_seasons_lite.side_effect = RuntimeError("boom")
        with patch.object(bot, "plex_client", fake_client):
            res = await bot._plex_ensure_show_seasons_lite(show, focus_season=2)
        self.assertIn(2, res)
        # Resolution still empty (top-up failed).
        self.assertEqual(res[2].resolution, "")

    async def test_returns_empty_when_no_plex_client_and_cold_cache(self):
        show = PlexShow("X", 2024, "key", seasons={})
        with patch.object(bot, "plex_client", None):
            res = await bot._plex_ensure_show_seasons_lite(show, focus_season=2)
        self.assertEqual(res, {})


# ─── _plex_other_seasons_context + _format_other_seasons_context ───────


class FormatOtherSeasonsContextTests(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(bot._format_other_seasons_context([]), "")

    def test_single_season_with_count_and_resolution(self):
        s = PlexSeason("k1", 1, 8, [], "1080")
        out = bot._format_other_seasons_context([s])
        self.assertIn("S1", out)
        self.assertIn("8 эп.", out)
        self.assertIn("1080", out)

    def test_multiple_seasons_separated_by_commas(self):
        seasons = [
            PlexSeason("k1", 1, 8, [], ""),
            PlexSeason("k2", 2, 10, [], "1080"),
            PlexSeason("k3", 3, 12, [], ""),
        ]
        out = bot._format_other_seasons_context(seasons)
        self.assertIn("S1 (8 эп.)", out)
        self.assertIn("S2 (10 эп., 1080)", out)
        self.assertIn("S3 (12 эп.)", out)
        # Commas-and-spaces separator.
        self.assertIn(", ", out)
        # Leading icon + label.
        self.assertTrue(out.startswith("✅ В Plex уже есть:"))


class PlexOtherSeasonsContextTests(unittest.TestCase):
    """_plex_other_seasons_context picks every season EXCEPT the focus one
    and sorts by season number."""

    def test_excludes_focus_season(self):
        show = PlexShow("X", 2024, "key", seasons={
            1: PlexSeason("k1", 1, 8, [], ""),
            2: PlexSeason("k2", 2, 10, [], "1080"),
            3: PlexSeason("k3", 3, 12, [], ""),
        })
        others = bot._plex_other_seasons_context(show, focus_season=2)
        nums = [s.season_number for s in others]
        self.assertEqual(nums, [1, 3])

    def test_sorted_by_season_number(self):
        show = PlexShow("X", 2024, "key", seasons={
            3: PlexSeason("k3", 3, 0, [], ""),
            1: PlexSeason("k1", 1, 0, [], ""),
            5: PlexSeason("k5", 5, 0, [], ""),
        })
        others = bot._plex_other_seasons_context(show, focus_season=3)
        self.assertEqual([s.season_number for s in others], [1, 5])


# ─── _plex_series_confirm_text — context block injection ───────────────


class PlexSeriesConfirmTextTests(unittest.TestCase):
    """Verify the R.2 enhancement: context block above the warning."""

    def _check(self, *, action: str, focus_res: str = "1080",
               other_seasons: list[tuple[int, int, str]] | None = None,
               ):
        """Build a PlexSeriesCheckResult with a show that has other seasons."""
        from plex import PlexSeriesCheckResult
        focus = PlexSeason("k_focus", 2, 10, [], focus_res)
        seasons = {2: focus}
        if other_seasons:
            for n, ep, res in other_seasons:
                seasons[n] = PlexSeason(f"k{n}", n, ep, [], res)
        show = PlexShow("Test Show", 2024, "showkey", seasons=seasons)
        return PlexSeriesCheckResult(show=show, season=focus, action=action)

    def test_warn_same_includes_context_when_other_seasons_exist(self):
        check = self._check(action="warn_same", other_seasons=[
            (1, 8, ""), (3, 12, "1080"),
        ])
        text = bot._plex_series_confirm_text(check, "Show.S02.1080p", "1080")
        self.assertIn("уже есть в Plex", text)
        self.assertIn("В Plex уже есть:", text)
        self.assertIn("S1", text)
        self.assertIn("S3", text)
        # Focus season (S2) NOT in the context line — it's the one being warned about.
        # Check it appears in the warning, not in the «уже есть» line.
        context_line = next(l for l in text.split("\n") if "уже есть:" in l)
        self.assertNotIn("S2", context_line)

    def test_offer_upgrade_uses_replace_prompt(self):
        check = self._check(action="offer_upgrade", focus_res="720")
        text = bot._plex_series_confirm_text(check, "Show.S02.1080p", "1080")
        # Status line mentions the existing/requested quality pair.
        self.assertIn("720", text)
        # Prompt asks about replacement, not just «всё равно».
        self.assertIn("Заменить", text)

    def test_no_other_seasons_omits_context_block(self):
        """Single-season-in-Plex case: no context line emitted."""
        check = self._check(action="warn_same", other_seasons=None)
        text = bot._plex_series_confirm_text(check, "Show.S02.1080p", "1080")
        self.assertNotIn("В Plex уже есть:", text)


# ─── _plex_confirm_keyboard show_upgrade flag ──────────────────────────


class PlexConfirmKeyboardUpgradeTests(unittest.TestCase):
    def test_default_has_two_buttons(self):
        from keyboards import _plex_confirm_keyboard
        kb = _plex_confirm_keyboard()
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("⬇️ Скачать всё равно", labels)
        self.assertIn("❌ Отмена", labels)
        self.assertNotIn("🔼 Заменить версией получше", labels)

    def test_show_upgrade_adds_replacement_button(self):
        from keyboards import _plex_confirm_keyboard
        kb = _plex_confirm_keyboard(show_upgrade=True)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("🔼 Заменить версией получше", labels)
        self.assertIn("plex:upgrade", callbacks)
        # Standard buttons still there.
        self.assertIn("❌ Отмена", labels)


# ─── plex_upgrade_download handler ─────────────────────────────────────


class PlexUpgradeDownloadHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_logs_old_key_and_dispatches_download(self):
        from telegram.ext import ContextTypes

        query = MagicMock()
        query.data = "plex:upgrade"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        ctx = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        ctx.user_data = {
            "plex_pending": {
                "type": "search",
                "index": 0,
                "subscribe": True,
                "notify_policy": "final_only",
                "download_policy": "only_when_complete",
                "plex_old_season_key": "season-key-42",
                "plex_action": "offer_upgrade",
            },
        }

        captured = {}

        async def fake_download_and_add(q, c, index, **kw):
            captured["index"] = index
            captured["kwargs"] = kw
            return 0

        update = MagicMock(callback_query=query)
        with patch.object(bot, "_download_and_add", side_effect=fake_download_and_add):
            await bot.plex_upgrade_download(update, ctx)

        # Old key was pulled from pending; download was dispatched with policies.
        self.assertEqual(captured["index"], 0)
        self.assertEqual(captured["kwargs"]["notify_policy"], "final_only")
        self.assertEqual(captured["kwargs"]["download_policy"], "only_when_complete")
        self.assertTrue(captured["kwargs"]["_skip_plex_check"])
        # pending was consumed.
        self.assertNotIn("plex_pending", ctx.user_data)

    async def test_upgrade_with_no_pending_shows_error(self):
        query = MagicMock()
        query.data = "plex:upgrade"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.user_data = {}
        update = MagicMock(callback_query=query)

        with patch.object(bot, "_download_and_add", AsyncMock()) as dl:
            await bot.plex_upgrade_download(update, ctx)

        dl.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Данные потеряны", text)


# ─── pre-warm scheduling ───────────────────────────────────────────────


class PreWarmTests(unittest.IsolatedAsyncioTestCase):
    async def test_prewarm_fires_for_first_partial_result(self):
        ctx = MagicMock()
        ctx.user_data = {}
        results = [
            {"title": "Movie 2024 1080p", "partial": False},          # skip
            {"title": "Show.S02E01-05.of.10.1080p", "partial": True}, # winner
            {"title": "Show.S03.partial.1080p", "partial": True},     # ignored
        ]
        # We patch _schedule_plex_prewarm to observe the call rather than
        # actually firing an asyncio task (which would race with assertions).
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_schedule_plex_prewarm") as sched,
        ):
            bot._maybe_prewarm_plex_for_results(ctx, 100, results)
        sched.assert_called_once()
        args = sched.call_args.args
        # ctx, chat_id, series_query, season_num
        self.assertEqual(args[1], 100)
        # series_query should not be empty, season_num should be 2
        self.assertTrue(args[2])
        self.assertEqual(args[3], 2)

    async def test_prewarm_skipped_when_plex_disabled(self):
        ctx = MagicMock()
        with (
            patch.object(bot, "PLEX_ENABLED", False),
            patch.object(bot, "_schedule_plex_prewarm") as sched,
        ):
            bot._maybe_prewarm_plex_for_results(
                ctx, 100, [{"title": "Show.S02 1080p", "partial": True}],
            )
        sched.assert_not_called()

    async def test_prewarm_skipped_when_chat_id_missing(self):
        ctx = MagicMock()
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_schedule_plex_prewarm") as sched,
        ):
            bot._maybe_prewarm_plex_for_results(
                ctx, None, [{"title": "Show.S02 1080p", "partial": True}],
            )
        sched.assert_not_called()

    async def test_prewarm_skipped_when_no_partial_results(self):
        ctx = MagicMock()
        with (
            patch.object(bot, "PLEX_ENABLED", True),
            patch.object(bot, "_schedule_plex_prewarm") as sched,
        ):
            bot._maybe_prewarm_plex_for_results(ctx, 100, [
                {"title": "Movie 2024 1080p", "partial": False},
                {"title": "Other Show", "partial": False},
            ])
        sched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
