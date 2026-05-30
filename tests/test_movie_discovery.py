import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from movie_discovery import (
    _KP_CACHE_TTL_FOUND_DAYS,
    _KP_CACHE_TTL_JITTER_MAX_DAYS,
    _KP_MAX_STALE_REFRESH_PER_RUN,
    build_cards,
    discovery_years,
    evaluate_result,
    extract_alt_title,
    fingerprint,
    is_recent_published_at,
    normalize_movie_title,
    parse_published_at,
    prune_kp_cache,
    prune_seen_fingerprints,
    prune_tracker_data,
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

    def test_sports_events_are_rejected(self) -> None:
        cases = [
            # Latin abbreviations (existing)
            "NBA Playoffs (2026) WEB-DL 1080p",
            "WWE Raw 11 05 (2026) HDTV 1080p",
            "Чемпионат Польши (2026) WEB-DL 1080p",
            "Чемпионат Испании (2026) 1080p",
            "UEFA Champions League (2026) WEB-DL 1080p",
            # Cyrillic abbreviations (new)
            "КХЛ 25 (2026) WEB-DL 1080p",
            "НБА (2026) 1080p",
            "РПЛ (2026) WEB-DL 1080p",
            # Cup patterns (new)
            "Кубок Италии (2026) WEB-DL 1080p",
            "Кубок Саудовской Аравии (2026) 1080p",
            "Кубок Стэнли (2026) WEB-DL 1080p",
            # Countries not previously covered (new)
            "Чемпионат Саудовской Аравии (2026) 1080p",
            "Чемпионат Бразилии (2026) WEB-DL 1080p",
            # Wrestling shows without date (new)
            "AEW Dynamite (2026) HDTV 1080p",
            "AEW Dynamite 13 05 (2026) HDTV 1080p",
            # Russian cup/league names (new)
            "Лига чемпионов (2026) WEB-DL 1080p",
            "Лига Европы (2026) WEB-DL 1080p",
        ]
        for title in cases:
            with self.subTest(title=title):
                self.assertIsNone(
                    release_from_result(
                        SimpleNamespace(title=title, size="3 GB", seeders=20, category=""),
                        source="jackett",
                        allowed_years={2026, 2025},
                        qualities={"1080p"},
                    ),
                    msg=f"Expected {title!r} to be rejected as sports event",
                )

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
            votes=None,
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

    def test_film_not_found_in_kp_is_dropped_when_filter_active(self) -> None:
        """KP enricher: missing KP match must drop the card when min_kp_rating > 0."""
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

        self.assertEqual(len(cache["cards"]), 0,
                         "No KP data + active filter must drop the card")

    def test_film_without_kp_data_dropped_when_filter_active(self) -> None:
        """When min_kp_rating > 0, releases with no KP match must be filtered out."""
        releases = [self._make_release("КХЛ 25 (2026) WEB-DL 1080p")]
        # Simulate KP returning None (no match found)
        kp = SimpleNamespace(search_movie=lambda title, year, **kw: None)

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=6.0,
            kinopoisk_client=kp,
        )

        self.assertEqual(len(cache["cards"]), 0,
                         "Release with no KP data must be dropped when min_kp_rating > 0")

    def test_film_without_kp_data_allowed_when_filter_zero(self) -> None:
        """When min_kp_rating=0, releases with no KP match should still pass through."""
        releases = [self._make_release("Нечто (2026) WEB-DL 1080p")]
        kp = SimpleNamespace(search_movie=lambda title, year, **kw: None)

        cache = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=kp,
        )

        self.assertEqual(len(cache["cards"]), 1,
                         "Release with no KP data must be allowed when min_kp_rating=0")

    def test_film_with_low_kp_rating_is_dropped(self) -> None:
        releases = [self._make_release("Тест (2026) WEB-DL 1080p")]
        match = SimpleNamespace(kp_id=1, title="Тест", url="", year=2026, rating=4.5, genres=[], votes=None)
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
            return SimpleNamespace(kp_id=1, title=title, url="", year=year, rating=8.5, genres=[], votes=None)

        def _kp_bad(title, year, **kw):
            return SimpleNamespace(kp_id=2, title=title, url="", year=year, rating=6.0, genres=[], votes=None)

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


class CardScoringTests(unittest.TestCase):
    """Unit tests for _compute_card_score behaviour."""

    def _make_card(self, *, rating=None, year=2026, seeders=100, quality="1080p", title="Film WEB-DL 1080p") -> dict:
        from movie_discovery import _finalize_card
        card = {
            "title": "Film",
            "year": year,
            "releases": [{
                "title": title,
                "score": 300,
                "quality": quality,
                "seeders": seeders,
                "tracker": "rutracker",
                "url": "https://rutracker.org/1",
                "size_gb": 5.0,
                "size": 5 * 1024 ** 3,
            }],
        }
        if rating is not None:
            card["rating"] = rating
        _finalize_card(card, set())
        return card

    def test_rated_7_0_outscores_unrated(self) -> None:
        """A film with rating 7.0 must rank above an unrated film at equal tech/pop."""
        from movie_discovery import _compute_card_score
        rated = self._make_card(rating=7.0)
        rated["rating"] = 7.0
        unrated = self._make_card()  # no rating → neutral 0.35
        self.assertGreater(
            _compute_card_score(rated, 2026),
            _compute_card_score(unrated, 2026),
        )

    def test_unrated_conservative_neutral_is_035(self) -> None:
        """Neutral rating_score for unknown films must be exactly 0.35."""
        from movie_discovery import _compute_card_score, _WEIGHT_RATING
        # Build two identical cards; one rated to the 0.35-equivalent, one unrated
        card = self._make_card()
        score_unrated = _compute_card_score(card, 2026)
        # Replace rating with the equivalent of 0.35 * 4.5 + 5.0 = 6.575
        card["rating"] = 6.575
        score_neutral_equiv = _compute_card_score(card, 2026)
        self.assertAlmostEqual(score_unrated, score_neutral_equiv, places=2)

    def test_no_new_bonus_in_score(self) -> None:
        """is_new flag must not affect the final score (new_bonus removed)."""
        from movie_discovery import _compute_card_score
        card_new = self._make_card()
        card_new["is_new"] = True
        card_old = self._make_card()
        card_old["is_new"] = False
        self.assertEqual(
            _compute_card_score(card_new, 2026),
            _compute_card_score(card_old, 2026),
        )

    def test_score_bounded_0_to_1(self) -> None:
        """Score must stay within [0, 1] for any realistic input."""
        from movie_discovery import _compute_card_score
        perfect = self._make_card(rating=9.5, seeders=500, quality="2160p", title="Film BDRemux 2160p")
        perfect["rating"] = 9.5
        worst = self._make_card(rating=5.0, seeders=0, quality="720p", title="Film HDRip 720p")
        worst["rating"] = 5.0
        self.assertLessEqual(_compute_card_score(perfect, 2026), 1.0)
        self.assertGreaterEqual(_compute_card_score(worst, 2026), 0.0)


class PruneKpCacheTests(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    def _found(self, days_ago: int) -> dict:
        ts = (self._now() - timedelta(days=days_ago)).isoformat()
        return {"kp_id": 1, "title": "Film", "cached_at": ts}

    def _miss(self, days_ago: int) -> dict:
        ts = (self._now() - timedelta(days=days_ago)).isoformat()
        return {"kp_id": None, "cached_at": ts}

    def test_empty_cache_returns_empty(self) -> None:
        self.assertEqual(prune_kp_cache({}, now=self._now()), {})

    def test_fresh_found_entry_is_kept(self) -> None:
        cache = {"film|2026": self._found(days_ago=5)}
        self.assertIn("film|2026", prune_kp_cache(cache, now=self._now()))

    def test_stale_found_entry_is_removed(self) -> None:
        # TTL for found = 14 days; 15 days old → expired
        cache = {"film|2026": self._found(days_ago=15)}
        self.assertNotIn("film|2026", prune_kp_cache(cache, now=self._now()))

    def test_fresh_miss_entry_is_kept(self) -> None:
        # TTL for miss = 3 days; 2 days old → fresh
        cache = {"miss|2026": self._miss(days_ago=2)}
        self.assertIn("miss|2026", prune_kp_cache(cache, now=self._now()))

    def test_stale_miss_entry_is_removed(self) -> None:
        # TTL for miss = 3 days; 4 days old → expired
        cache = {"miss|2026": self._miss(days_ago=4)}
        self.assertNotIn("miss|2026", prune_kp_cache(cache, now=self._now()))

    def test_malformed_cached_at_is_dropped(self) -> None:
        cache = {"bad|2026": {"kp_id": 1, "cached_at": "not-a-date"}}
        self.assertNotIn("bad|2026", prune_kp_cache(cache, now=self._now()))

    def test_non_dict_entry_is_dropped(self) -> None:
        cache = {"bad": "string-value"}  # type: ignore[dict-item]
        self.assertNotIn("bad", prune_kp_cache(cache, now=self._now()))

    def test_excess_entries_trimmed_to_max(self) -> None:
        cache = {
            f"film{i}|2026": self._found(days_ago=i)
            for i in range(1, 11)  # 10 entries, all within TTL
        }
        self.assertEqual(len(prune_kp_cache(cache, now=self._now(), max_entries=5)), 5)

    def test_trimming_keeps_newest_entries(self) -> None:
        cache = {
            "old|2026": self._found(days_ago=10),
            "new|2026": self._found(days_ago=1),
        }
        result = prune_kp_cache(cache, now=self._now(), max_entries=1)
        self.assertIn("new|2026", result)
        self.assertNotIn("old|2026", result)

    def test_found_and_miss_ttls_differ(self) -> None:
        """A 4-day-old found entry (TTL=14) is kept; a 4-day-old miss (TTL=3) is removed."""
        cache = {
            "found|2026": self._found(days_ago=4),
            "miss|2026": self._miss(days_ago=4),
        }
        result = prune_kp_cache(cache, now=self._now())
        self.assertIn("found|2026", result)
        self.assertNotIn("miss|2026", result)


class BuildCardsKpCacheTests(unittest.TestCase):
    def _make_release(self, title: str = "Тест (2026) WEB-DL 1080p") -> dict:
        return {
            "source": "jackett",
            "title": title,
            "movie_title": "Тест",
            "alt_title": "",
            "year": 2026,
            "quality": "1080p",
            "size": "3 GB",
            "size_gb": 3.0,
            "seeders": 50,
            "tracker": "a",
            "topic_id": "",
            "topic_url": "https://example.com/a",
            "url": "",
            "magnet_url": None,
            "torrent_url": None,
            "published_at": "",
            "score": 500,
        }

    def test_cache_hit_skips_api_call(self) -> None:
        """A fresh kp_cache entry must prevent any call to search_movie."""
        releases = [self._make_release()]
        call_count = {"n": 0}

        def spy(title, year, **kw):
            call_count["n"] += 1
            return None

        kp_cache = {
            "тест|2026": {
                "kp_id": 99,
                "title": "Тест (KP)",
                "year": 2026,
                "rating": 7.8,
                "genres": ["драма"],
                "countries": ["USA", "Canada"],
                "url": "https://www.kinopoisk.ru/film/99/",
                "poster_url": "https://img.example/poster.jpg",
                "poster_preview_url": "https://img.example/poster-preview.jpg",
                "cached_at": "2026-05-12T11:00:00+00:00",
            }
        }

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=spy),
            kp_cache=kp_cache,
        )

        self.assertEqual(call_count["n"], 0, "search_movie must not be called on cache hit")
        self.assertEqual(result["cards"][0]["kp_id"], 99)
        self.assertAlmostEqual(result["cards"][0]["rating"], 7.8)
        self.assertEqual(result["cards"][0]["countries"], ["USA", "Canada"])
        self.assertEqual(result["cards"][0]["poster_url"], "https://img.example/poster.jpg")
        self.assertEqual(result["cards"][0]["poster_preview_url"], "https://img.example/poster-preview.jpg")

    def test_legacy_cache_without_countries_refreshes_metadata(self) -> None:
        releases = [self._make_release()]
        call_count = {"n": 0}
        match = SimpleNamespace(
            kp_id=99,
            title="Тест (KP)",
            url="https://www.kinopoisk.ru/film/99/",
            year=2026,
            rating=7.8,
            genres=[],
            countries=["USA"],
            votes=None,
        )

        def spy(title, year, **kw):
            call_count["n"] += 1
            return match

        kp_cache = {
            "тест|2026": {
                "kp_id": 99,
                "title": "Тест (KP)",
                "year": 2026,
                "rating": 7.8,
                "genres": [],
                "url": "https://www.kinopoisk.ru/film/99/",
                "cached_at": "2026-05-12T11:00:00+00:00",
            }
        }

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=spy),
            kp_cache=kp_cache,
        )

        self.assertEqual(call_count["n"], 1)
        self.assertEqual(result["cards"][0]["countries"], ["USA"])

    def test_legacy_cache_without_countries_keeps_stale_card_on_refresh_miss(self) -> None:
        releases = [self._make_release()]
        kp_cache = {
            "тест|2026": {
                "kp_id": 99,
                "title": "Тест (KP)",
                "year": 2026,
                "rating": 7.8,
                "genres": [],
                "url": "https://www.kinopoisk.ru/film/99/",
                "cached_at": "2026-05-12T11:00:00+00:00",
            }
        }

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=7.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda title, year, **kw: None),
            kp_cache=kp_cache,
        )

        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["kp_id"], 99)
        self.assertEqual(result["cards"][0]["rating"], 7.8)

    def test_cache_miss_calls_api_and_stores_result(self) -> None:
        """On a cache miss the API is called and the result is stored in the returned cache."""
        releases = [self._make_release()]
        match = SimpleNamespace(kp_id=42, title="Тест", url="", year=2026, rating=8.0, genres=[], votes=None)
        kp = SimpleNamespace(search_movie=lambda title, year, **kw: match)

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=kp,
            kp_cache={},
        )

        returned = result["kp_cache"]
        self.assertTrue(
            any(isinstance(e, dict) and e.get("kp_id") == 42 for e in returned.values()),
            "API result must be persisted in kp_cache",
        )

    def test_cache_miss_stores_poster_urls_on_card_and_cache(self) -> None:
        releases = [self._make_release()]
        match = SimpleNamespace(
            kp_id=42,
            title="Тест",
            url="",
            year=2026,
            rating=8.0,
            genres=[],
            countries=["USA", "Canada"],
            votes=None,
            poster_url="https://img.example/poster.jpg",
            poster_preview_url="https://img.example/poster-preview.jpg",
        )

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda title, year, **kw: match),
            kp_cache={},
        )

        card = result["cards"][0]
        entry = next(iter(result["kp_cache"].values()))
        self.assertEqual(card["countries"], ["USA", "Canada"])
        self.assertEqual(entry["countries"], ["USA", "Canada"])
        self.assertEqual(card["poster_url"], "https://img.example/poster.jpg")
        self.assertEqual(card["poster_preview_url"], "https://img.example/poster-preview.jpg")
        self.assertEqual(entry["poster_url"], "https://img.example/poster.jpg")
        self.assertEqual(entry["poster_preview_url"], "https://img.example/poster-preview.jpg")

    def test_cached_miss_skips_api_call(self) -> None:
        """A cached 'not found' entry (kp_id=None) must suppress further API calls."""
        releases = [self._make_release()]
        call_count = {"n": 0}

        def spy(title, year, **kw):
            call_count["n"] += 1
            return None

        kp_cache = {
            "тест|2026": {"kp_id": None, "cached_at": "2026-05-12T11:00:00+00:00"},
        }

        build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=spy),
            kp_cache=kp_cache,
        )

        self.assertEqual(call_count["n"], 0, "search_movie must not be called for a cached miss")

    def test_returned_cache_is_independent_copy(self) -> None:
        """The kp_cache dict returned in the result must contain the new entry."""
        releases = [self._make_release()]
        match = SimpleNamespace(kp_id=7, title="X", url="", year=2026, rating=7.0, genres=[], votes=None)
        initial_cache: dict = {}

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda *a, **kw: match),
            kp_cache=initial_cache,
        )

        self.assertGreater(len(result["kp_cache"]), 0)

    def test_ttl_jitter_stored_in_found_entry(self) -> None:
        """A newly stored found entry must include ttl_days in the valid jitter range."""
        releases = [self._make_release()]
        match = SimpleNamespace(kp_id=5, title="Тест", url="", year=2026, rating=7.0, genres=[], votes=None)

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda *a, **kw: match),
            kp_cache={},
        )

        entry = next(iter(result["kp_cache"].values()))
        self.assertIn("ttl_days", entry)
        min_ttl = _KP_CACHE_TTL_FOUND_DAYS
        max_ttl = _KP_CACHE_TTL_FOUND_DAYS + _KP_CACHE_TTL_JITTER_MAX_DAYS
        self.assertGreaterEqual(entry["ttl_days"], min_ttl)
        self.assertLessEqual(entry["ttl_days"], max_ttl)

    def test_miss_entry_has_no_ttl_days(self) -> None:
        """A 'not found' entry must NOT include ttl_days (jitter only for found entries)."""
        releases = [self._make_release()]

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda *a, **kw: None),
            kp_cache={},
        )

        entry = next(iter(result["kp_cache"].values()))
        self.assertNotIn("ttl_days", entry)

    def test_kp_searches_counter_increments_on_cache_miss(self) -> None:
        """kp_searches in the result must count actual search_movie() calls made."""
        releases = [
            self._make_release("Фильм А (2026) WEB-DL 1080p"),
            self._make_release("Фильм Б (2026) WEB-DL 1080p"),
        ]
        # Adjust movie_title to create two distinct cards
        releases[0]["movie_title"] = "Фильм А"
        releases[1]["movie_title"] = "Фильм Б"
        releases[1]["topic_url"] = "https://example.com/b"

        match = SimpleNamespace(kp_id=1, title="A", url="", year=2026, rating=7.0, genres=[], votes=None)

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda *a, **kw: match),
            kp_cache={},
        )

        self.assertEqual(result["kp_searches"], 2, "one search per distinct card on cache miss")

    def test_kp_searches_zero_on_cache_hit(self) -> None:
        """kp_searches must be 0 when all cards are served from a fresh cache."""
        releases = [self._make_release()]

        kp_cache = {
            "тест|2026": {
                "kp_id": 99,
                "title": "Тест",
                "year": 2026,
                "rating": 7.0,
                "genres": [],
                "countries": [],
                "url": "",
                "cached_at": "2026-05-12T11:00:00+00:00",
            }
        }

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=lambda *a, **kw: None),
            kp_cache=kp_cache,
        )

        self.assertEqual(result["kp_searches"], 0)

    def test_stale_cap_limits_api_calls_and_uses_stale_data(self) -> None:
        """When max_stale_refresh=1, only the first stale entry triggers an API call;
        the second stale entry falls back to its stale cached value."""
        stale_ts = "2000-01-01T00:00:00+00:00"  # guaranteed stale
        releases = [
            self._make_release("Фильм А (2026) WEB-DL 1080p"),
            self._make_release("Фильм Б (2026) WEB-DL 1080p"),
        ]
        releases[0]["movie_title"] = "Фильм А"
        releases[1]["movie_title"] = "Фильм Б"
        releases[1]["topic_url"] = "https://example.com/b"

        kp_cache = {
            "фильм а|2026": {
                "kp_id": 10, "title": "Фильм А", "year": 2026,
                "rating": 7.0, "genres": [], "url": "", "cached_at": stale_ts,
            },
            "фильм б|2026": {
                "kp_id": 20, "title": "Фильм Б", "year": 2026,
                "rating": 8.0, "genres": [], "url": "", "cached_at": stale_ts,
            },
        }
        call_count = {"n": 0}

        def refreshed_match(title, year, **kw):
            call_count["n"] += 1
            return SimpleNamespace(kp_id=99, title=title, url="", year=year, rating=9.0, genres=[], votes=None)

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=refreshed_match),
            kp_cache=kp_cache,
            max_stale_refresh=1,
        )

        # Exactly one API call should have been made
        self.assertEqual(call_count["n"], 1, "only one stale entry should be refreshed")
        self.assertEqual(result["kp_searches"], 1)

        # Both cards should still appear (stale fallback used for the second)
        self.assertEqual(len(result["cards"]), 2)

    def test_max_stale_refresh_none_refreshes_all(self) -> None:
        """When max_stale_refresh=None, all stale entries are refreshed."""
        stale_ts = "2000-01-01T00:00:00+00:00"
        releases = [
            self._make_release("Фильм А (2026) WEB-DL 1080p"),
            self._make_release("Фильм Б (2026) WEB-DL 1080p"),
        ]
        releases[0]["movie_title"] = "Фильм А"
        releases[1]["movie_title"] = "Фильм Б"
        releases[1]["topic_url"] = "https://example.com/b"

        kp_cache = {
            "фильм а|2026": {"kp_id": 1, "title": "А", "year": 2026, "rating": 7.0, "genres": [], "url": "", "cached_at": stale_ts},
            "фильм б|2026": {"kp_id": 2, "title": "Б", "year": 2026, "rating": 7.0, "genres": [], "url": "", "cached_at": stale_ts},
        }
        call_count = {"n": 0}

        def spy(title, year, **kw):
            call_count["n"] += 1
            return SimpleNamespace(kp_id=99, title=title, url="", year=year, rating=9.0, genres=[], votes=None)

        result = build_cards(
            releases,
            now_text="2026-05-12 12:00",
            known_fingerprints=set(),
            limit=20,
            min_kp_rating=0.0,
            kinopoisk_client=SimpleNamespace(search_movie=spy),
            kp_cache=kp_cache,
            max_stale_refresh=None,
        )

        self.assertEqual(call_count["n"], 2, "all stale entries should be refreshed when cap is None")
        self.assertEqual(result["kp_searches"], 2)


class PruneKpCachePerEntryTtlTests(unittest.TestCase):
    """prune_kp_cache must respect per-entry ttl_days when present."""

    def _now(self) -> datetime:
        return datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    def test_per_entry_ttl_days_extends_expiry(self) -> None:
        """An entry with ttl_days=21 that is 20 days old should still be kept."""
        ts = (self._now() - timedelta(days=20)).isoformat()
        cache = {
            "film|2026": {
                "kp_id": 1, "title": "Film", "cached_at": ts, "ttl_days": 21,
            }
        }
        result = prune_kp_cache(cache, now=self._now())
        self.assertIn("film|2026", result)

    def test_per_entry_ttl_days_expires_correctly(self) -> None:
        """An entry with ttl_days=21 that is 22 days old must be expired."""
        ts = (self._now() - timedelta(days=22)).isoformat()
        cache = {
            "film|2026": {
                "kp_id": 1, "title": "Film", "cached_at": ts, "ttl_days": 21,
            }
        }
        result = prune_kp_cache(cache, now=self._now())
        self.assertNotIn("film|2026", result)

    def test_no_ttl_days_falls_back_to_global_constant(self) -> None:
        """An entry without ttl_days uses the global _KP_CACHE_TTL_FOUND_DAYS constant."""
        # 13 days old, global TTL = 14 → should be kept
        ts = (self._now() - timedelta(days=13)).isoformat()
        cache = {"film|2026": {"kp_id": 1, "title": "Film", "cached_at": ts}}
        self.assertIn("film|2026", prune_kp_cache(cache, now=self._now()))

        # 15 days old → should be removed
        ts_stale = (self._now() - timedelta(days=15)).isoformat()
        cache_stale = {"film|2026": {"kp_id": 1, "title": "Film", "cached_at": ts_stale}}
        self.assertNotIn("film|2026", prune_kp_cache(cache_stale, now=self._now()))


class PruneTrackerDataTests(unittest.TestCase):
    def _make_release(self, tracker: str, source: str = "jackett", topic_id: str = "1") -> dict:
        return {
            "source": source,
            "tracker": tracker,
            "topic_id": topic_id,
            "topic_url": "",
            "title": f"Film 2026 1080p web-dl",
            "size": "10 GB",
            "size_gb": 10.0,
            "seeders": 50,
            "quality": "1080p",
            "movie_title": "Film",
            "alt_title": "",
            "year": 2026,
            "url": "",
            "magnet_url": None,
            "torrent_url": None,
            "published_at": "",
            "score": 100,
        }

    def _make_card(self, releases: list[dict]) -> dict:
        return {"title": "Film", "year": 2026, "releases": releases, "key": "2026:film"}

    def test_empty_removed_ids_returns_unchanged(self) -> None:
        r = self._make_release("kinozal")
        card = self._make_card([r])
        fps = {fingerprint(r): "2026-01-01"}
        cards_out, fps_out = prune_tracker_data([card], fps, set())
        self.assertEqual(len(cards_out), 1)
        self.assertEqual(fps_out, fps)

    def test_removes_releases_from_deleted_tracker(self) -> None:
        r1 = self._make_release("kinozal", topic_id="1")
        r2 = self._make_release("rutracker", source="rutracker", topic_id="2")
        card = self._make_card([r1, r2])
        fps = {fingerprint(r1): "t1", fingerprint(r2): "t2"}
        cards_out, fps_out = prune_tracker_data([card], fps, {"kinozal"})
        self.assertEqual(len(cards_out), 1)
        self.assertEqual(len(cards_out[0]["releases"]), 1)
        self.assertEqual(cards_out[0]["releases"][0]["tracker"], "rutracker")
        self.assertNotIn(fingerprint(r1), fps_out)
        self.assertIn(fingerprint(r2), fps_out)

    def test_card_without_remaining_releases_is_dropped(self) -> None:
        r = self._make_release("kinozal")
        card = self._make_card([r])
        fps = {fingerprint(r): "ts"}
        cards_out, fps_out = prune_tracker_data([card], fps, {"kinozal"})
        self.assertEqual(cards_out, [])
        self.assertEqual(fps_out, {})

    def test_fingerprints_pruned_by_tracker_field(self) -> None:
        r = self._make_release("torrenty", topic_id="5")
        fps = {fingerprint(r): "ts"}
        _, fps_out = prune_tracker_data([], fps, {"torrenty"})
        self.assertEqual(fps_out, {})

    def test_fingerprints_from_other_trackers_kept(self) -> None:
        r = self._make_release("kinozal", topic_id="3")
        fps = {fingerprint(r): "ts"}
        _, fps_out = prune_tracker_data([], fps, {"torrenty"})
        self.assertEqual(fps_out, fps)


if __name__ == "__main__":
    unittest.main()
