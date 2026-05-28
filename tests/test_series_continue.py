import unittest

from plex import PlexSeason, PlexShow
from series_continue import (
    SeriesCatchUpCandidate,
    build_series_catch_up_candidates,
    external_guid_id,
    identity_from_plex_show,
    resolve_series_completeness,
)


class PlexSeriesIdentityTests(unittest.TestCase):
    def test_identity_from_plex_show_uses_external_guids(self):
        show = PlexShow(
            title="Belgravia",
            original_title="Belgravia",
            year=2020,
            rating_key="2223",
            seasons={},
            guid="plex://show/abc",
            external_guids=[
                "imdb://tt9642982",
                "tmdb://85862",
                "tvdb://362204",
            ],
        )

        identity = identity_from_plex_show(show)

        self.assertEqual(identity.plex_rating_key, "2223")
        self.assertEqual(identity.plex_guid, "plex://show/abc")
        self.assertEqual(identity.imdb_id, "tt9642982")
        self.assertEqual(identity.tmdb_id, "85862")
        self.assertEqual(identity.tvdb_id, "362204")
        self.assertEqual(identity.title, "Belgravia")
        self.assertEqual(identity.original_title, "Belgravia")
        self.assertEqual(identity.year, 2020)

    def test_identity_falls_back_to_title_year_without_external_guids(self):
        show = PlexShow(
            title="Show X",
            year=2019,
            rating_key="100",
            seasons={},
            guid="plex://show/local-only",
        )

        identity = identity_from_plex_show(show)

        self.assertEqual(identity.plex_rating_key, "100")
        self.assertEqual(identity.plex_guid, "plex://show/local-only")
        self.assertEqual(identity.imdb_id, "")
        self.assertEqual(identity.tmdb_id, "")
        self.assertEqual(identity.tvdb_id, "")
        self.assertEqual(identity.title, "Show X")
        self.assertEqual(identity.year, 2019)

    def test_external_guid_id_supports_thetvdb_alias(self):
        self.assertEqual(external_guid_id(["thetvdb://123"], "thetvdb"), "123")

    def test_candidate_keeps_identity_and_season_summary(self):
        show = PlexShow(title="X", year=2020, rating_key="1", seasons={})
        identity = identity_from_plex_show(show)
        candidate = SeriesCatchUpCandidate(
            identity=identity,
            season_number=2,
            present_count=3,
            present_episode_numbers=(1, 2, 4),
            quality="1080",
        )

        self.assertEqual(candidate.identity.plex_rating_key, "1")
        self.assertEqual(candidate.season_number, 2)
        self.assertEqual(candidate.present_episode_numbers, (1, 2, 4))
        self.assertEqual(candidate.quality, "1080")


class SeriesCatchUpCandidateBuilderTests(unittest.TestCase):
    def _show(self, *, episode_count: int = 8, resolution: str = "1080") -> PlexShow:
        return PlexShow(
            title="The Rookie",
            original_title="The Rookie",
            year=2018,
            rating_key="show-1",
            seasons={
                8: PlexSeason(
                    rating_key="season-8",
                    season_number=8,
                    episode_count=episode_count,
                    resolution=resolution,
                )
            },
        )

    def test_history_partial_with_topic_becomes_candidate(self):
        history = [{
            "event": "download_completed",
            "chat_id": 100,
            "kind": "series",
            "series_query": "The Rookie",
            "season": 8,
            "topic_id": "12345",
            "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "quality": "1080",
            "last_episode_end": 8,
            "total_episodes": 18,
        }]

        candidates = build_series_catch_up_candidates(
            [self._show()],
            history,
            chat_id=100,
            scope="mine",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].season_number, 8)
        self.assertEqual(candidates[0].present_count, 8)
        self.assertEqual(candidates[0].known_total, 18)
        self.assertEqual(candidates[0].topic_id, "12345")
        self.assertEqual(candidates[0].history_chat_ids, (100,))

    def test_history_full_season_is_not_candidate(self):
        history = [{
            "event": "plex_found",
            "chat_id": 100,
            "kind": "series",
            "series_query": "The Rookie",
            "season": 8,
            "topic_id": "12345",
            "total_episodes": 18,
        }]

        candidates = build_series_catch_up_candidates(
            [self._show(episode_count=18)],
            history,
            chat_id=100,
            scope="mine",
        )

        self.assertEqual(candidates, [])

    def test_plex_only_candidate_is_visible_only_in_all_scope_when_total_known(self):
        show = self._show(episode_count=6)

        mine = build_series_catch_up_candidates(
            [show],
            [],
            chat_id=100,
            scope="mine",
            known_totals_by_show={"show-1": {8: 10}},
        )
        all_candidates = build_series_catch_up_candidates(
            [show],
            [],
            chat_id=100,
            scope="all",
            known_totals_by_show={"show-1": {8: 10}},
        )

        self.assertEqual(mine, [])
        self.assertEqual(len(all_candidates), 1)
        self.assertEqual(all_candidates[0].source, "plex")
        self.assertEqual(all_candidates[0].known_total, 10)

    def test_mine_scope_filters_other_users_history(self):
        history = [{
            "event": "download_completed",
            "chat_id": 200,
            "kind": "series",
            "series_query": "The Rookie",
            "season": 8,
            "topic_id": "12345",
            "total_episodes": 18,
        }]

        mine = build_series_catch_up_candidates(
            [self._show()],
            history,
            chat_id=100,
            scope="mine",
        )
        all_candidates = build_series_catch_up_candidates(
            [self._show()],
            history,
            chat_id=100,
            scope="all",
        )

        self.assertEqual(mine, [])
        self.assertEqual(len(all_candidates), 1)
        self.assertEqual(all_candidates[0].history_chat_ids, (200,))

    def test_watch_state_fields_do_not_filter_candidate(self):
        history = [{
            "event": "download_completed",
            "chat_id": 100,
            "kind": "series",
            "series_query": "The Rookie",
            "season": 8,
            "topic_id": "12345",
            "total_episodes": 18,
            "viewedLeafCount": 18,
            "viewCount": 1,
            "lastViewedAt": "1773664171",
        }]

        candidates = build_series_catch_up_candidates(
            [self._show()],
            history,
            chat_id=100,
            scope="mine",
        )

        self.assertEqual(len(candidates), 1)


class SeriesCompletenessResolverTests(unittest.TestCase):
    def _candidate(self, **overrides) -> SeriesCatchUpCandidate:
        identity = identity_from_plex_show(
            PlexShow(title="Show", year=2020, rating_key="show-1", seasons={})
        )
        payload = {
            "identity": identity,
            "season_number": 1,
            "present_count": 6,
        }
        payload.update(overrides)
        return SeriesCatchUpCandidate(**payload)

    def test_gap_uses_present_episode_indexes(self):
        result = resolve_series_completeness(
            self._candidate(present_episode_numbers=(1, 2, 4), present_count=3)
        )

        self.assertEqual(result.confidence, "gap")
        self.assertEqual(result.missing_episode_numbers, (3,))

    def test_known_total_detects_missing_tail(self):
        result = resolve_series_completeness(
            self._candidate(known_total=10, present_count=6)
        )

        self.assertEqual(result.confidence, "exact_total")
        self.assertEqual(result.known_total, 10)
        self.assertEqual(result.missing_episode_numbers, (7, 8, 9, 10))

    def test_known_total_equal_present_without_history_is_unknown(self):
        result = resolve_series_completeness(
            self._candidate(known_total=6, present_count=6)
        )

        self.assertEqual(result.confidence, "unknown")

    def test_history_partial_without_total_is_separate_signal(self):
        result = resolve_series_completeness(
            self._candidate(
                known_total=0,
                present_count=6,
                topic_id="12345",
                history_last_episode_end=6,
            )
        )

        self.assertEqual(result.confidence, "history_partial")
        self.assertEqual(result.known_total, 0)

    def test_watch_state_is_ignored(self):
        candidate = self._candidate(known_total=10, present_count=6)

        plain = resolve_series_completeness(candidate)
        watched = resolve_series_completeness(
            candidate,
            watch_state={"viewedLeafCount": 10, "viewCount": 1},
        )

        self.assertEqual(watched, plain)


if __name__ == "__main__":
    unittest.main()
