import unittest
from datetime import datetime
from types import SimpleNamespace

from movie_discovery import (
    build_cards,
    discovery_years,
    evaluate_result,
    is_recent_published_at,
    parse_published_at,
    release_from_result,
)


class MovieDiscoveryTests(unittest.TestCase):
    def test_discovery_years_include_only_current_and_previous_year(self) -> None:
        self.assertEqual(discovery_years(datetime(2026, 5, 12)), {2026, 2025})

    def test_published_at_parses_iso_and_rfc_dates(self) -> None:
        self.assertIsNotNone(parse_published_at("2026-05-10T12:30:00Z"))
        self.assertIsNotNone(parse_published_at("Sun, 10 May 2026 12:30:00 GMT"))
        self.assertIsNone(parse_published_at(""))
        self.assertIsNone(parse_published_at("not a date"))

    def test_recent_published_at_respects_max_age_window(self) -> None:
        now = datetime.fromisoformat("2026-05-12T12:00:00+00:00")

        self.assertTrue(is_recent_published_at("2026-05-01T12:00:00Z", now=now, max_age_days=32))
        self.assertFalse(is_recent_published_at("2026-03-01T12:00:00Z", now=now, max_age_days=32))
        self.assertFalse(is_recent_published_at("", now=now, max_age_days=32))

    def test_accepts_good_recent_movie_release(self) -> None:
        result = SimpleNamespace(
            title="Хороший фильм / Good Movie (2026) WEB-DL 1080p",
            size="3.2 GB",
            seeders=42,
            tracker="rutracker",
            topic_id="123",
        )

        release = release_from_result(
            result,
            source="rutracker",
            allowed_years={2026, 2025},
            qualities={"1080p", "2160p"},
        )

        self.assertIsNotNone(release)
        self.assertEqual(release["year"], 2026)
        self.assertEqual(release["quality"], "1080p")

    def test_current_year_gets_recency_boost_over_previous_year(self) -> None:
        current, reason_current = evaluate_result(
            SimpleNamespace(title="Фильм (2026) WEB-DL 1080p", size="3 GB", seeders=10),
            source="jackett",
            allowed_years={2026, 2025},
            qualities={"1080p"},
        )
        previous, reason_previous = evaluate_result(
            SimpleNamespace(title="Фильм (2025) WEB-DL 1080p", size="3 GB", seeders=10),
            source="jackett",
            allowed_years={2026, 2025},
            qualities={"1080p"},
        )

        self.assertEqual(reason_current, "accepted")
        self.assertEqual(reason_previous, "accepted")
        self.assertGreater(current["score"], previous["score"])

    def test_rejects_old_series_adult_bad_quality_and_too_small_4k(self) -> None:
        cases = [
            SimpleNamespace(title="Фильм (2024) WEB-DL 1080p", size="4 GB", category=""),
            SimpleNamespace(title="Сериал Сезон 1 (2026) WEB-DL 1080p", size="10 GB", category=""),
            SimpleNamespace(title="Adult movie 18+ (2026) WEB-DL 1080p", size="3 GB", category=""),
            SimpleNamespace(title="Фильм (2026) TS 1080p", size="3 GB", category=""),
            SimpleNamespace(title="Фильм (2026) WEB-DL 2160p", size="2 GB", category=""),
        ]

        for result in cases:
            with self.subTest(title=result.title):
                self.assertIsNone(
                    release_from_result(
                        result,
                        source="jackett",
                        allowed_years={2026, 2025},
                        qualities={"1080p", "2160p"},
                    )
                )

    def test_cards_with_same_kinopoisk_match_are_merged(self) -> None:
        releases = [
            {
                "source": "jackett",
                "title": "Bride! (2026) WEB-DL 1080p",
                "movie_title": "Bride",
                "year": 2026,
                "quality": "1080p",
                "size": "3 GB",
                "seeders": 20,
                "tracker": "a",
                "topic_id": "",
                "topic_url": "https://example.com/a",
                "score": 500,
            },
            {
                "source": "jackett",
                "title": "Невеста! (2026) WEB-DL 1080p",
                "movie_title": "Невеста",
                "year": 2026,
                "quality": "1080p",
                "size": "4 GB",
                "seeders": 30,
                "tracker": "b",
                "topic_id": "",
                "topic_url": "https://example.com/b",
                "score": 600,
            },
        ]
        match = SimpleNamespace(
            kp_id=123,
            title="Невеста!",
            url="https://www.kinopoisk.ru/film/123/",
            year=2026,
            rating=7.1,
            genres=["ужасы"],
        )
        kp = SimpleNamespace(search_movie=lambda title, year: match)

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=6.0,
            kinopoisk_client=kp,
        )

        self.assertEqual(len(cache["cards"]), 1)
        self.assertEqual(cache["cards"][0]["title"], "Невеста!")
        self.assertEqual(cache["cards"][0]["release_count"], 2)


if __name__ == "__main__":
    unittest.main()
