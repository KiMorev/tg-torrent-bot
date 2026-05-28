import unittest

from plex import PlexShow
from series_continue import (
    SeriesCatchUpCandidate,
    external_guid_id,
    identity_from_plex_show,
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


if __name__ == "__main__":
    unittest.main()
