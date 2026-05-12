import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from movie_discovery import (
    build_cards,
    discovery_years,
    evaluate_result,
    extract_alt_title,
    is_recent_published_at,
    normalize_movie_title,
    parse_published_at,
    prune_seen_fingerprints,
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

    def test_extract_alt_title_bilingual(self) -> None:
        self.assertEqual(extract_alt_title("Невеста! / The Bride! [2026, BDRip 1080p]"), "The Bride!")

    def test_extract_alt_title_three_parts_picks_non_cyrillic(self) -> None:
        self.assertEqual(extract_alt_title("На вершине / Вершина / Apex [2026, WEB-DL 1080p]"), "Apex")

    def test_extract_alt_title_monolingual_returns_empty(self) -> None:
        self.assertEqual(extract_alt_title("Хейтер [2026, WEB-DL 1080p]"), "")
        self.assertEqual(extract_alt_title("Project Hail Mary [2026, WEB-DL 1080p]"), "")

    def test_extract_alt_title_stored_in_release(self) -> None:
        result = SimpleNamespace(
            title="Проект «Конец света» / Project Hail Mary (2026) WEB-DL 1080p",
            size="12 GB",
            seeders=700,
            tracker="rutracker",
            topic_id="1",
        )
        release = release_from_result(
            result,
            source="rutracker",
            allowed_years={2026, 2025},
            qualities={"1080p", "2160p"},
        )
        self.assertIsNotNone(release)
        self.assertEqual(release["alt_title"], "Project Hail Mary")

    def test_leading_bracket_year_prefix_is_stripped(self) -> None:
        self.assertEqual(normalize_movie_title("[2026, WEB-DL 1080p] Фильм"), "Фильм")

    def test_audio_only_title_is_rejected(self) -> None:
        result = SimpleNamespace(
            title="[2026, WEB-DL 1080p] Original Rus",
            size="3 GB",
            seeders=10,
            tracker="jackett",
        )
        _, reason = evaluate_result(
            result,
            source="jackett",
            allowed_years={2026, 2025},
            qualities={"1080p", "2160p"},
        )
        self.assertEqual(reason, "no_movie_title")

    def test_normal_titles_unchanged(self) -> None:
        self.assertEqual(normalize_movie_title("Project Hail Mary [2026, WEB-DL 1080p]"), "Project Hail Mary")

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
        kp = SimpleNamespace(search_movie=lambda title, year, **kw: match)

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


class PruneSeenFingerprintsTests(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    def test_empty_dict_returns_empty(self) -> None:
        self.assertEqual(prune_seen_fingerprints({}, now=self._now()), {})

    def test_entries_within_ttl_are_kept(self) -> None:
        seen = {"fp1": "2026-05-01 10:00", "fp2": "2026-04-15 10:00"}
        result = prune_seen_fingerprints(seen, now=self._now(), ttl_days=60)
        self.assertIn("fp1", result)
        self.assertIn("fp2", result)

    def test_entries_older_than_ttl_are_removed(self) -> None:
        seen = {"old": "2026-01-01 10:00", "fresh": "2026-05-10 10:00"}
        result = prune_seen_fingerprints(seen, now=self._now(), ttl_days=30)
        self.assertNotIn("old", result)
        self.assertIn("fresh", result)

    def test_entries_without_timestamp_are_kept_but_deprioritised(self) -> None:
        seen = {"no_ts": ""}
        result = prune_seen_fingerprints(seen, now=self._now(), ttl_days=30)
        self.assertIn("no_ts", result)

    def test_excess_entries_trimmed_to_max(self) -> None:
        seen = {f"fp{i}": f"2026-05-{i:02d} 10:00" for i in range(1, 11)}
        result = prune_seen_fingerprints(seen, now=self._now(), ttl_days=60, max_entries=5)
        self.assertEqual(len(result), 5)

    def test_trimming_keeps_newest_entries(self) -> None:
        seen = {
            "old": "2026-05-01 10:00",
            "new": "2026-05-11 10:00",
        }
        result = prune_seen_fingerprints(seen, now=self._now(), ttl_days=60, max_entries=1)
        self.assertIn("new", result)
        self.assertNotIn("old", result)


class BuildCardsEnricherTests(unittest.TestCase):
    def _make_release(self, title: str, tracker: str = "a", topic_url: str = "") -> dict:
        return {
            "source": "jackett",
            "title": title,
            "movie_title": "Тест",
            "year": 2026,
            "quality": "1080p",
            "size": "3 GB",
            "size_gb": 3.0,
            "seeders": 50,
            "tracker": tracker,
            "topic_id": "",
            "topic_url": topic_url or f"https://example.com/{tracker}",
            "url": "",
            "magnet_url": None,
            "torrent_url": None,
            "published_at": "",
            "score": 500,
        }

    def test_film_not_found_in_kp_is_kept_in_cards(self) -> None:
        """KP enricher: missing KP match must not drop the card."""
        releases = [self._make_release("Тест (2026) WEB-DL 1080p")]
        kp = SimpleNamespace(search_movie=lambda title, year, **kw: None)

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=6.0,
            kinopoisk_client=kp,
        )

        self.assertEqual(len(cache["cards"]), 1)
        self.assertIsNone(cache["cards"][0].get("rating"))

    def test_film_with_low_kp_rating_is_dropped(self) -> None:
        releases = [self._make_release("Тест (2026) WEB-DL 1080p")]
        match = SimpleNamespace(kp_id=1, title="Тест", url="", year=2026, rating=4.5, genres=[])
        kp = SimpleNamespace(search_movie=lambda title, year, **kw: match)

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=6.0,
            kinopoisk_client=kp,
        )

        self.assertEqual(len(cache["cards"]), 0)

    def test_known_fingerprints_as_dict_is_accepted(self) -> None:
        """build_cards must accept dict[str, str] for known_fingerprints."""
        releases = [self._make_release("Тест (2026) WEB-DL 1080p")]
        known: dict[str, str] = {"some|old|fp|||title|3 GB": "2026-01-01 10:00"}

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=known,
            limit=20,
            min_kp_rating=0.0,
        )

        self.assertIsInstance(cache["seen_fingerprints"], dict)

    def test_seen_fingerprints_returned_as_dict_with_timestamps(self) -> None:
        releases = [self._make_release("Тест (2026) WEB-DL 1080p", topic_url="https://x.com/1")]

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
        )

        seen = cache["seen_fingerprints"]
        self.assertIsInstance(seen, dict)
        # Every value must be a non-empty timestamp string
        for fp, ts in seen.items():
            with self.subTest(fp=fp):
                self.assertIsInstance(ts, str)
                self.assertTrue(ts, msg="timestamp should not be empty for new fingerprints")

    def test_legacy_set_known_fingerprints_still_works(self) -> None:
        """Backward compat: passing set[str] must not raise."""
        releases = [self._make_release("Тест (2026) WEB-DL 1080p")]

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints={"some_old_fp"},
            limit=20,
            min_kp_rating=0.0,
        )

        self.assertIsInstance(cache["seen_fingerprints"], dict)
        self.assertEqual(len(cache["cards"]), 1)

    def test_higher_kp_rating_produces_higher_card_score(self) -> None:
        """Normalised scoring: KP rating must be the dominant signal."""
        releases_good = [self._make_release("Тест (2026) WEB-DL 1080p", tracker="good")]
        releases_bad = [self._make_release("Тест (2026) WEB-DL 1080p", tracker="bad", topic_url="https://b.com/1")]

        def _kp_good(title, year, **kw):
            return SimpleNamespace(kp_id=1, title=title, url="", year=year, rating=8.5, genres=[])

        def _kp_bad(title, year, **kw):
            return SimpleNamespace(kp_id=2, title=title, url="", year=year, rating=6.0, genres=[])

        cache_good = build_cards(
            releases_good,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=_kp_good),
        )
        cache_bad = build_cards(
            releases_bad,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=_kp_bad),
        )

        score_good = cache_good["cards"][0]["score"]
        score_bad = cache_bad["cards"][0]["score"]
        self.assertGreater(score_good, score_bad)


if __name__ == "__main__":
    unittest.main()
