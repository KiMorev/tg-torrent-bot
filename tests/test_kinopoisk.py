import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kinopoisk import KinopoiskClient, extract_kp_id


class FakeResponse:
    def __init__(self, json_data) -> None:
        self._json_data = json_data

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if isinstance(self._json_data, BaseException):
            raise self._json_data
        return self._json_data


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers = {}

    def get(self, *args, **kwargs) -> FakeResponse:
        return self.response


class KinopoiskClientTests(unittest.TestCase):
    def test_get_director_ignores_malformed_staff_response(self) -> None:
        client = KinopoiskClient("secret")
        client._session = FakeSession(FakeResponse({"bad": "shape"}))

        self.assertEqual(client._get_director(123), "")

    def test_get_director_skips_malformed_people(self) -> None:
        client = KinopoiskClient("secret")
        client._session = FakeSession(FakeResponse([
            "bad",
            {"professionKey": "ACTOR", "nameRu": "Actor"},
            {"professionKey": "DIRECTOR", "nameRu": "Director One"},
            {"professionKey": "DIRECTOR", "nameEn": "Director Two"},
            {"professionKey": "DIRECTOR", "nameRu": "Director Three"},
        ]))

        self.assertEqual(client._get_director(123), "Director One, Director Two")


class ExtractKpIdTests(unittest.TestCase):
    def test_plain_numeric_slug(self) -> None:
        self.assertEqual(extract_kp_id("https://www.kinopoisk.ru/film/12345/"), 12345)

    def test_title_dash_id_slug(self) -> None:
        self.assertEqual(extract_kp_id("https://www.kinopoisk.ru/film/interstellar-77044/"), 77044)

    def test_series_path(self) -> None:
        self.assertEqual(extract_kp_id("https://www.kinopoisk.ru/series/67890/"), 67890)

    def test_kp_ru_short_domain(self) -> None:
        self.assertEqual(extract_kp_id("https://kp.ru/film/12345/"), 12345)

    def test_show_path(self) -> None:
        self.assertEqual(extract_kp_id("https://www.kinopoisk.ru/show/999/"), 999)

    def test_url_embedded_in_text(self) -> None:
        self.assertEqual(
            extract_kp_id("Смотри: https://www.kinopoisk.ru/film/77044/ — отличный фильм"),
            77044,
        )

    def test_unrelated_url_returns_none(self) -> None:
        self.assertIsNone(extract_kp_id("https://example.com/film/12345/"))

    def test_plain_text_returns_none(self) -> None:
        self.assertIsNone(extract_kp_id("просто текст без ссылки"))


class SearchMovieAltTitleTests(unittest.TestCase):
    """Tests for the alt_title-first search logic in KinopoiskClient.search_movie."""

    def _film_response(
        self,
        name_ru: str = "Фильм",
        name_en: str = "Film",
        year: int = 2026,
        film_id: int = 1,
    ) -> dict:
        return {
            "films": [{
                "filmId": film_id,
                "nameRu": name_ru,
                "nameEn": name_en,
                "type": "FILM",
                "year": str(year),
                "rating": "7.5",
                "genres": [],
            }]
        }

    def _empty_response(self) -> dict:
        return {"films": []}

    def _make_client(self, get_fn) -> KinopoiskClient:
        client = KinopoiskClient("test-key")
        client._session = SimpleNamespace(get=get_fn, headers={})
        return client

    @patch("kinopoisk.time.sleep")
    def test_alt_title_searched_first_on_hit(self, _sleep) -> None:
        """When alt_title yields a result the Russian title must not be queried."""
        searched: list[str] = []

        def fake_get(url, params=None, timeout=None):
            searched.append((params or {}).get("keyword", ""))
            return FakeResponse(self._film_response(name_ru="Вершина", name_en="Apex"))

        client = self._make_client(fake_get)
        result = client.search_movie("Вершина", year=2026, alt_title="Apex")

        self.assertEqual(len(searched), 1, "Only one HTTP call expected (alt_title hit)")
        self.assertIn("Apex", searched[0])
        self.assertIsNotNone(result)

    @patch("kinopoisk.time.sleep")
    def test_falls_back_to_title_when_alt_title_misses(self, _sleep) -> None:
        """When alt_title returns no results the Russian title is tried next."""
        searched: list[str] = []

        def fake_get(url, params=None, timeout=None):
            kw = (params or {}).get("keyword", "")
            searched.append(kw)
            if "Apex" in kw:
                return FakeResponse(self._empty_response())
            return FakeResponse(self._film_response(name_ru="Вершина", year=2026))

        client = self._make_client(fake_get)
        result = client.search_movie("Вершина", year=2026, alt_title="Apex")

        self.assertEqual(len(searched), 2, "Expected two HTTP calls: alt_title miss + title retry")
        self.assertIn("Apex", searched[0])
        self.assertIn("Вершина", searched[1])
        self.assertIsNotNone(result)
        self.assertEqual(result.title_ru, "Вершина")

    @patch("kinopoisk.time.sleep")
    def test_no_alt_title_searches_only_main_title(self, _sleep) -> None:
        """Without alt_title exactly one HTTP call is made using the main title."""
        searched: list[str] = []

        def fake_get(url, params=None, timeout=None):
            searched.append((params or {}).get("keyword", ""))
            return FakeResponse(self._film_response())

        client = self._make_client(fake_get)
        client.search_movie("Фильм", year=2026)

        self.assertEqual(len(searched), 1)
        self.assertIn("Фильм", searched[0])

    @patch("kinopoisk.time.sleep")
    def test_year_mismatch_skips_result(self, _sleep) -> None:
        """Items whose year differs by more than one from the requested year are skipped."""

        def fake_get(url, params=None, timeout=None):
            return FakeResponse(self._film_response(year=2020))  # far off

        client = self._make_client(fake_get)
        result = client.search_movie("Фильм", year=2026)

        self.assertIsNone(result, "Year-mismatched result must be rejected")

    @patch("kinopoisk.time.sleep")
    def test_entry_without_year_is_skipped_when_year_known(self, _sleep) -> None:
        """KP entries that have no year must be skipped when we know the expected year."""

        def fake_get(url, params=None, timeout=None):
            return FakeResponse({"films": [{
                "filmId": 1,
                "nameRu": "Фильм",
                "nameEn": "",
                "type": "FILM",
                "year": None,  # announced / coming-soon entry
                "rating": "7.5",
                "genres": [],
            }]})

        client = self._make_client(fake_get)
        result = client.search_movie("Фильм", year=2026)

        self.assertIsNone(result, "Entry without year must be skipped when year is known")

    @patch("kinopoisk.time.sleep")
    def test_exact_year_preferred_over_close_year(self, _sleep) -> None:
        """When API returns both a ±1 and an exact-year match, the exact one wins."""

        def _film(name_ru, film_id, year):
            return {"filmId": film_id, "nameRu": name_ru, "nameEn": "", "type": "FILM",
                    "year": str(year), "rating": "7.5", "genres": []}

        def fake_get(url, params=None, timeout=None):
            return FakeResponse({"films": [
                _film("Буратино (старый)", 10, 2025),
                _film("Буратино (новый)", 20, 2026),
            ]})

        client = self._make_client(fake_get)
        result = client.search_movie("Буратино", year=2026)

        self.assertIsNotNone(result)
        self.assertEqual(result.kp_id, 20, "Exact-year entry must be preferred over ±1 entry")
        self.assertEqual(result.year, 2026)

    @patch("kinopoisk.time.sleep")
    def test_close_year_used_as_fallback_when_no_exact_match(self, _sleep) -> None:
        """±1 match is accepted when there is no exact-year result."""

        def fake_get(url, params=None, timeout=None):
            return FakeResponse({"films": [
                {"filmId": 99, "nameRu": "Фильм Фест", "nameEn": "", "type": "FILM",
                 "year": "2025", "rating": "7.5", "genres": []},
            ]})

        client = self._make_client(fake_get)
        result = client.search_movie("Фильм Фест", year=2026)

        self.assertIsNotNone(result)
        self.assertEqual(result.kp_id, 99)
        self.assertEqual(result.year, 2025)

    @patch("kinopoisk.time.sleep")
    def test_search_movie_keeps_poster_urls(self, _sleep) -> None:
        """KP search results include poster URLs used by /new notifications."""

        def fake_get(url, params=None, timeout=None):
            payload = self._film_response()
            payload["films"][0]["posterUrl"] = "https://img.example/poster.jpg"
            payload["films"][0]["posterUrlPreview"] = "https://img.example/poster-preview.jpg"
            return FakeResponse(payload)

        client = self._make_client(fake_get)
        result = client.search_movie("Фильм", year=2026)

        self.assertIsNotNone(result)
        self.assertEqual(result.poster_url, "https://img.example/poster.jpg")
        self.assertEqual(result.poster_preview_url, "https://img.example/poster-preview.jpg")


if __name__ == "__main__":
    unittest.main()
