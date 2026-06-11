from __future__ import annotations

import unittest

from series_bulk_planner import (
    STATUS_ALREADY_DOWNLOADING,
    STATUS_ALREADY_IN_PLEX,
    STATUS_EXACT,
    STATUS_GOOD,
    STATUS_MISSING,
    STATUS_NEEDS_DECISION,
    STATUS_PARTIAL,
    VOICE_ANY_FROM_REFERENCE,
    VOICE_REQUIRE_SELECTED,
    VOICE_SINGLE_FROM_REFERENCE,
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

    def test_single_from_reference_chooses_one_voice_across_seasons(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[1, 2],
            profile=_base_profile(
                voice_policy=VOICE_SINGLE_FROM_REFERENCE,
                voices=("LostFilm", "NewStudio"),
            ),
            results=[
                _result("Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / LostFilm / Original"),
                _result("Клиника / Scrubs / Сезон: 1 / WEB-DL 1080p / NewStudio / Original"),
                _result("Клиника / Scrubs / Сезон: 2 / WEB-DL 1080p / LostFilm / Original"),
            ],
        )

        selected_titles = [
            season.selected.result["title"]
            for season in plan.seasons
            if season.selected is not None
        ]
        self.assertEqual(len(selected_titles), 2)
        self.assertTrue(all("LostFilm" in title for title in selected_titles))

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

    def test_preferred_voice_boosts_but_does_not_block_other_reference_voice(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Clinic",
            seasons=[5, 6],
            profile=_base_profile(
                voices=("LostFilm", "NewStudio"),
                preferred_voices=("LostFilm",),
                require_original=False,
            ),
            results=[
                _result("Clinic / Scrubs / S05 / WEB-DL 1080p / NewStudio"),
                _result("Clinic / Scrubs / S06 / WEB-DL 1080p / NewStudio", seeders=500),
                _result("Clinic / Scrubs / S06 / WEB-DL 1080p / LostFilm", seeders=1),
            ],
        )

        season5 = plan.seasons[0]
        season6 = plan.seasons[1]
        self.assertEqual(season5.status, STATUS_EXACT)
        self.assertIsNotNone(season5.selected)
        self.assertIn("NewStudio", season5.selected.result["title"])
        self.assertEqual(season6.status, STATUS_EXACT)
        self.assertIsNotNone(season6.selected)
        self.assertIn("LostFilm", season6.selected.result["title"])
        self.assertTrue(any("preferred voice matched: LostFilm" in reason for reason in season6.selected.reasons))

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

    def test_preferred_quality_available_blocks_other_quality_auto_selection(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[6],
            profile=_base_profile(require_original=True),
            results=[
                _result("Клиника / Scrubs / Сезон: 6 / WEB-DL 720p / LostFilm / Original", seeders=500, size="40 GB"),
                _result("Клиника / Scrubs / Сезон: 6 / WEB-DL 1080p / LostFilm / Original", seeders=1, size="5 GB"),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_EXACT)
        self.assertIsNotNone(season.selected)
        self.assertIn("1080p", season.selected.result["title"])
        self.assertEqual(len(season.candidates), 1)

    def test_missing_preferred_quality_auto_selects_best_other_quality(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[6],
            profile=_base_profile(require_original=True),
            results=[
                _result("Клиника / Scrubs / Сезон: 6 / WEB-DL 720p / LostFilm / Original"),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_GOOD)
        self.assertIsNotNone(season.selected)
        self.assertIn("720p", season.selected.result["title"])
        self.assertIn(
            "preferred quality unavailable: 1080p; selected quality: 720p",
            season.selected.warnings,
        )
        self.assertNotIn("quality does not match search preference", season.selected.hard_failures)

    def test_missing_preferred_quality_still_respects_other_hard_filters(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Клиника",
            seasons=[6],
            profile=_base_profile(require_original=True),
            results=[
                _result("Клиника / Scrubs / Сезон: 6 / WEB-DL 720p / LostFilm"),
            ],
        )

        season = plan.seasons[0]
        self.assertEqual(season.status, STATUS_NEEDS_DECISION)
        self.assertIn(
            "preferred quality unavailable: 1080p; selected quality: 720p",
            season.candidates[0].warnings,
        )
        self.assertIn("original audio not found", season.candidates[0].hard_failures)
        self.assertNotIn("quality does not match search preference", season.candidates[0].hard_failures)

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

    def test_non_downloadable_seed_result_is_not_a_candidate(self) -> None:
        seed = {
            "source": "continue_missing",
            "title": "The Rookie S07 1080p",
            "series": True,
            "quality": "1080p",
        }

        missing_plan = build_series_bulk_plan(
            series_title="The Rookie",
            seasons=[7],
            profile=SeriesBulkProfile(quality="1080p", source="continue_missing"),
            results=[seed],
        )

        self.assertEqual(missing_plan.seasons[0].status, STATUS_MISSING)
        self.assertEqual(missing_plan.seasons[0].candidates, ())

        real = _result("The Rookie S07 1080p")
        real_plan = build_series_bulk_plan(
            series_title="The Rookie",
            seasons=[7],
            profile=SeriesBulkProfile(quality="1080p", source="continue_missing"),
            results=[seed, real],
        )

        self.assertEqual(real_plan.seasons[0].status, STATUS_EXACT)
        self.assertIsNotNone(real_plan.seasons[0].selected)
        self.assertIs(real_plan.seasons[0].selected.result, real)

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

    def test_title_match_handles_possessive_apostrophe(self) -> None:
        plan = build_series_bulk_plan(
            series_title="Clarksons Farm",
            seasons=[5],
            profile=_base_profile(require_original=False),
            results=[
                _result("Clarkson's Farm / S05 / WEB-DL 1080p / LostFilm"),
            ],
        )

        self.assertEqual(plan.seasons[0].status, STATUS_EXACT)
        self.assertIsNotNone(plan.seasons[0].selected)


if __name__ == "__main__":
    unittest.main()
