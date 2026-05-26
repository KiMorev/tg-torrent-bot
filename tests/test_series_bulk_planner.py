from __future__ import annotations

import unittest

from series_bulk_planner import (
    STATUS_ALREADY_DOWNLOADING,
    STATUS_ALREADY_IN_PLEX,
    STATUS_EXACT,
    STATUS_MISSING,
    STATUS_NEEDS_DECISION,
    STATUS_PARTIAL,
    VOICE_ANY_FROM_REFERENCE,
    VOICE_REQUIRE_SELECTED,
    SeriesBulkProfile,
    build_series_bulk_plan,
    release_profile_from_title,
)


def _result(title: str, *, seeders: int = 50, size: str = "10 GB") -> dict:
    return {
        "title": title,
        "seeders": seeders,
        "size": size,
        "source": "jackett",
        "tracker_name": "rutracker",
        "url": "https://example.test/topic",
    }


def _base_profile(**overrides) -> SeriesBulkProfile:
    values = {
        "quality": "1080p",
        "require_original": True,
        "require_subs": False,
        "voice_policy": VOICE_ANY_FROM_REFERENCE,
        "voices": ("LostFilm", "NewStudio", "Кравец"),
        "release_type": "WEB-DL",
        "tracker": "rutracker",
        "source": "jackett",
    }
    values.update(overrides)
    return SeriesBulkProfile(**values)


class ReleaseProfileTests(unittest.TestCase):
    def test_extracts_release_traits_from_title(self) -> None:
        profile = release_profile_from_title(
            "Клиника / Scrubs / Сезон: 4 / WEB-DL 1080p / LostFilm / Original / Sub"
        )

        self.assertEqual(profile.quality, "1080p")
        self.assertEqual(profile.release_type, "WEB-DL")
        self.assertIn("LostFilm", profile.voices)
        self.assertTrue(profile.has_original)
        self.assertTrue(profile.has_subs)


class SeriesBulkPlannerTests(unittest.TestCase):
    def test_selects_exact_candidate_when_hard_filters_and_soft_traits_match(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[4],
            profile=_base_profile(),
            results=[
                _result("Клиника / Scrubs / Сезон: 4 / WEB-DL 1080p / LostFilm / Original"),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_EXACT)
        self.assertIsNotNone(season.selected)
        self.assertIn("quality matches 1080p", season.selected.reasons)

    def test_any_from_reference_does_not_require_every_reference_voice(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[5],
            profile=_base_profile(voices=("LostFilm", "NewStudio", "Кравец")),
            results=[
                _result("Клиника / Scrubs / Сезон: 5 / WEB-DL 1080p / LostFilm / Original"),
            ],
        )

        self.assertEqual(plan.seasons[0].status, STATUS_EXACT)
        self.assertIsNotNone(plan.seasons[0].selected)

    def test_require_selected_voice_blocks_auto_selection_when_voice_is_missing(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[5],
            profile=_base_profile(
                voice_policy=VOICE_REQUIRE_SELECTED,
                voices=("Кравец",),
            ),
            results=[
                _result("Клиника / Scrubs / Сезон: 5 / WEB-DL 1080p / LostFilm / Original"),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_NEEDS_DECISION)
        self.assertIsNone(season.selected)
        self.assertIn("selected voice not found", season.candidates[0].hard_failures)

    def test_original_and_subs_from_search_are_hard_filters(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[6],
            profile=_base_profile(require_original=True, require_subs=True),
            results=[
                _result("Клиника / Scrubs / Сезон: 6 / WEB-DL 1080p / LostFilm / Sub"),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_NEEDS_DECISION)
        self.assertIn("original audio not found", season.candidates[0].hard_failures)

    def test_plex_and_downloading_seasons_are_skipped_before_candidate_selection(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[1, 2],
            profile=_base_profile(),
            plex_seasons=[1],
            downloading_seasons=[2],
            results=[
                _result("Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / LostFilm / Original"),
                _result("Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / LostFilm / Original"),
            ],
        )

        self.assertEqual(plan.seasons[0].status, STATUS_ALREADY_IN_PLEX)
        self.assertEqual(plan.seasons[1].status, STATUS_ALREADY_DOWNLOADING)

    def test_partial_season_is_not_auto_selected(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[7],
            profile=_base_profile(),
            results=[
                _result(
                    "Клиника / Scrubs / Сезон: 7 / Серии: 1-5 из 8 / "
                    "WEB-DL 1080p / LostFilm / Original"
                ),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_PARTIAL)
        self.assertIsNone(season.selected)
        self.assertEqual(season.candidates[0].episode_progress, (5, 8))

    def test_missing_season_gets_missing_status(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[8],
            profile=_base_profile(),
            results=[],
        )

        self.assertEqual(plan.seasons[0].status, STATUS_MISSING)

    def test_season_pack_is_collected_but_not_auto_selected_for_single_season(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[1],
            profile=_base_profile(),
            results=[
                _result("Клиника / Scrubs / Сезоны: 1-3 / WEB-DL 1080p / LostFilm / Original"),
            ],
        )

        self.assertEqual(plan.seasons[0].status, STATUS_MISSING)
        self.assertEqual(len(plan.pack_candidates), 1)


if __name__ == "__main__":
    unittest.main()
