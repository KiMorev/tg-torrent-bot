"""Unit tests for plex.py."""

import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

import requests

from plex import (
    PlexClient,
    PlexMovie,
    PlexCheckResult,
    PlexAPIError,
    PlexAuthError,
    PlexTimeoutError,
    PlexConnectionError,
    PlexParseError,
    _normalise_resolution,
    _parse_video,
    check_before_download,
    compare_quality,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _xml(text: str) -> ElementTree.Element:
    return ElementTree.fromstring(text)


def _mock_response(xml_text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = xml_text.encode()
    resp.raise_for_status = MagicMock()
    return resp


def _make_client() -> PlexClient:
    return PlexClient("http://192.168.1.103:32400", "testtoken")


# ---------------------------------------------------------------------------
# Resolution normalisation
# ---------------------------------------------------------------------------

class ResolutionNormalisationTests(unittest.TestCase):
    def test_4k_variants(self):
        for raw in ("4k", "2160", "uhd", "4K", "UHD"):
            self.assertEqual(_normalise_resolution(raw), "4k", raw)

    def test_1080_variants(self):
        for raw in ("1080", "1080p", "1080i"):
            self.assertEqual(_normalise_resolution(raw), "1080", raw)

    def test_720_variants(self):
        for raw in ("720", "720p"):
            self.assertEqual(_normalise_resolution(raw), "720", raw)

    def test_sd(self):
        self.assertEqual(_normalise_resolution("sd"), "sd")

    def test_unknown_returns_empty(self):
        self.assertEqual(_normalise_resolution(""), "")
        self.assertEqual(_normalise_resolution("unknown"), "")


# ---------------------------------------------------------------------------
# Quality comparison
# ---------------------------------------------------------------------------

class QualityComparisonTests(unittest.TestCase):
    def test_same_resolution(self):
        self.assertEqual(compare_quality("1080", "1080"), "same")

    def test_better(self):
        self.assertEqual(compare_quality("4k", "1080"), "better")
        self.assertEqual(compare_quality("1080", "720"), "better")

    def test_worse(self):
        self.assertEqual(compare_quality("720", "1080"), "worse")

    def test_unknown_vs_unknown(self):
        self.assertEqual(compare_quality("", ""), "same")

    def test_known_better_than_unknown(self):
        self.assertEqual(compare_quality("720", ""), "better")


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

class ParseVideoTests(unittest.TestCase):
    def _video_xml(
        self,
        title: str = "Dune",
        year: int = 2021,
        rating_key: str = "42",
        added_at: int = 1700000000,
        resolution: str = "1080",
        file_path: str = "/media/Dune.mkv",
    ) -> ElementTree.Element:
        return _xml(
            f'<Video title="{title}" year="{year}" ratingKey="{rating_key}"'
            f' addedAt="{added_at}">'
            f'  <Media videoResolution="{resolution}">'
            f'    <Part file="{file_path}"/>'
            f'  </Media>'
            f'</Video>'
        )

    def test_basic_fields(self):
        movie = _parse_video(self._video_xml())
        self.assertEqual(movie.title, "Dune")
        self.assertEqual(movie.year, 2021)
        self.assertEqual(movie.rating_key, "42")
        self.assertEqual(movie.added_at, 1700000000)
        self.assertEqual(movie.resolution, "1080")
        self.assertEqual(movie.file_paths, ["/media/Dune.mkv"])

    def test_4k_resolution_normalised(self):
        movie = _parse_video(self._video_xml(resolution="4k"))
        self.assertEqual(movie.resolution, "4k")

    def test_multiple_parts(self):
        elem = _xml(
            '<Video title="X" year="2020" ratingKey="1" addedAt="0">'
            '  <Media videoResolution="1080">'
            '    <Part file="/a/file1.mkv"/>'
            '    <Part file="/a/file2.mkv"/>'
            '  </Media>'
            '</Video>'
        )
        movie = _parse_video(elem)
        self.assertEqual(movie.file_paths, ["/a/file1.mkv", "/a/file2.mkv"])

    def test_missing_year_defaults_to_zero(self):
        elem = _xml('<Video title="X" ratingKey="1" addedAt="0"/>')
        movie = _parse_video(elem)
        self.assertEqual(movie.year, 0)


# ---------------------------------------------------------------------------
# PlexClient — is_healthy
# ---------------------------------------------------------------------------

class PlexClientHealthTests(unittest.TestCase):
    def test_healthy_when_server_responds(self):
        client = _make_client()
        identity_xml = (
            '<MediaContainer machineIdentifier="abc123" version="1.0"/>'
        )
        with patch.object(client._session, "get",
                          return_value=_mock_response(identity_xml)):
            self.assertTrue(client.is_healthy())

    def test_unhealthy_on_exception(self):
        client = _make_client()
        with patch.object(client._session, "get",
                          side_effect=Exception("connection refused")):
            self.assertFalse(client.is_healthy())


# ---------------------------------------------------------------------------
# PlexClient — error classification (_get behavior)
# ---------------------------------------------------------------------------

class PlexClientErrorClassificationTests(unittest.TestCase):
    """Verify _get raises specific PlexAPIError subclasses for each failure mode."""

    def test_http_401_raises_plex_auth_error(self):
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 401
        resp.ok = False
        with patch.object(client._session, "get", return_value=resp):
            with self.assertRaises(PlexAuthError) as ctx:
                client._get("/identity")
        self.assertEqual(ctx.exception.error_kind, "auth")

    def test_timeout_raises_plex_timeout_error(self):
        client = _make_client()
        with patch.object(client._session, "get",
                          side_effect=requests.Timeout("timed out")):
            with self.assertRaises(PlexTimeoutError) as ctx:
                client._get("/identity")
        self.assertEqual(ctx.exception.error_kind, "timeout")

    def test_connection_error_raises_plex_connection_error(self):
        client = _make_client()
        with patch.object(client._session, "get",
                          side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(PlexConnectionError) as ctx:
                client._get("/identity")
        self.assertEqual(ctx.exception.error_kind, "network")

    def test_malformed_xml_raises_plex_parse_error(self):
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.content = b"<html>this is not xml</html"  # malformed
        with patch.object(client._session, "get", return_value=resp):
            with self.assertRaises(PlexParseError) as ctx:
                client._get("/identity")
        self.assertEqual(ctx.exception.error_kind, "xml")

    def test_non_2xx_http_raises_generic_plex_api_error(self):
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 503
        resp.ok = False
        with patch.object(client._session, "get", return_value=resp):
            with self.assertRaises(PlexAPIError) as ctx:
                client._get("/identity")
        # 503 is not 401, so falls into generic "http" category
        self.assertEqual(ctx.exception.error_kind, "http")
        self.assertNotIsInstance(ctx.exception, PlexAuthError)


# ---------------------------------------------------------------------------
# PlexClient — get_machine_id
# ---------------------------------------------------------------------------

class PlexClientMachineIdTests(unittest.TestCase):
    def test_returns_machine_identifier(self):
        client = _make_client()
        xml = '<MediaContainer machineIdentifier="deadbeef" version="1.0"/>'
        with patch.object(client._session, "get",
                          return_value=_mock_response(xml)):
            self.assertEqual(client.get_machine_id(), "deadbeef")

    def test_cached_after_first_call(self):
        client = _make_client()
        xml = '<MediaContainer machineIdentifier="deadbeef" version="1.0"/>'
        mock_get = MagicMock(return_value=_mock_response(xml))
        with patch.object(client._session, "get", mock_get):
            client.get_machine_id()
            client.get_machine_id()
        self.assertEqual(mock_get.call_count, 1)


# ---------------------------------------------------------------------------
# PlexClient — find_movie_section
# ---------------------------------------------------------------------------

class PlexClientSectionTests(unittest.TestCase):
    def test_finds_movie_section(self):
        client = _make_client()
        xml = (
            '<MediaContainer>'
            '  <Directory type="show" key="2" title="TV Shows"/>'
            '  <Directory type="movie" key="1" title="Movies"/>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get",
                          return_value=_mock_response(xml)):
            self.assertEqual(client.find_movie_section(), "1")

    def test_returns_empty_when_no_movie_section(self):
        client = _make_client()
        xml = (
            '<MediaContainer>'
            '  <Directory type="show" key="2" title="TV Shows"/>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get",
                          return_value=_mock_response(xml)):
            self.assertEqual(client.find_movie_section(), "")


# ---------------------------------------------------------------------------
# PlexClient — get_all_movies
# ---------------------------------------------------------------------------

class PlexClientGetAllMoviesTests(unittest.TestCase):
    def _sections_xml(self) -> str:
        return (
            '<MediaContainer>'
            '  <Directory type="movie" key="1" title="Movies"/>'
            '</MediaContainer>'
        )

    def _library_xml(self) -> str:
        return (
            '<MediaContainer>'
            '  <Video title="Dune" year="2021" ratingKey="10" addedAt="100">'
            '    <Media videoResolution="1080"><Part file="/dune.mkv"/></Media>'
            '  </Video>'
            '  <Video title="Interstellar" year="2014" ratingKey="11" addedAt="200">'
            '    <Media videoResolution="1080"><Part file="/interstellar.mkv"/></Media>'
            '  </Video>'
            '</MediaContainer>'
        )

    def test_returns_all_movies(self):
        client = _make_client()
        responses = [
            _mock_response(self._sections_xml()),
            _mock_response(self._library_xml()),
        ]
        with patch.object(client._session, "get", side_effect=responses):
            movies = client.get_all_movies()
        self.assertEqual(len(movies), 2)
        titles = {m.title for m in movies}
        self.assertIn("Dune", titles)
        self.assertIn("Interstellar", titles)

    def test_returns_empty_when_no_section(self):
        client = _make_client()
        xml = '<MediaContainer></MediaContainer>'
        with patch.object(client._session, "get",
                          return_value=_mock_response(xml)):
            self.assertEqual(client.get_all_movies(), [])


# ---------------------------------------------------------------------------
# PlexClient — find_movie
# ---------------------------------------------------------------------------

class PlexClientFindMovieTests(unittest.TestCase):
    def _sections_xml(self) -> str:
        return (
            '<MediaContainer>'
            '  <Directory type="movie" key="1" title="Movies"/>'
            '</MediaContainer>'
        )

    def test_finds_exact_year_match(self):
        client = _make_client()
        search_xml = (
            '<MediaContainer>'
            '  <Video title="Dune" year="2021" ratingKey="10" addedAt="0">'
            '    <Media videoResolution="1080"><Part file="/dune.mkv"/></Media>'
            '  </Video>'
            '</MediaContainer>'
        )
        responses = [
            _mock_response(self._sections_xml()),
            _mock_response(search_xml),
        ]
        with patch.object(client._session, "get", side_effect=responses):
            movie = client.find_movie("Dune", 2021)
        self.assertIsNotNone(movie)
        self.assertEqual(movie.title, "Dune")

    def test_tolerates_year_off_by_one(self):
        client = _make_client()
        search_xml = (
            '<MediaContainer>'
            '  <Video title="Dune" year="2021" ratingKey="10" addedAt="0">'
            '    <Media videoResolution="1080"><Part file="/dune.mkv"/></Media>'
            '  </Video>'
            '</MediaContainer>'
        )
        responses = [
            _mock_response(self._sections_xml()),
            _mock_response(search_xml),
        ]
        with patch.object(client._session, "get", side_effect=responses):
            movie = client.find_movie("Dune", 2022)  # ±1 допуск
        self.assertIsNotNone(movie)

    def test_returns_none_when_year_mismatch(self):
        client = _make_client()
        search_xml = (
            '<MediaContainer>'
            '  <Video title="Dune" year="2021" ratingKey="10" addedAt="0">'
            '    <Media videoResolution="1080"><Part file="/dune.mkv"/></Media>'
            '  </Video>'
            '</MediaContainer>'
        )
        responses = [
            _mock_response(self._sections_xml()),
            _mock_response(search_xml),
        ]
        with patch.object(client._session, "get", side_effect=responses):
            movie = client.find_movie("Dune", 2018)  # слишком далеко
        self.assertIsNone(movie)

    def test_returns_none_when_no_results(self):
        client = _make_client()
        search_xml = '<MediaContainer></MediaContainer>'
        responses = [
            _mock_response(self._sections_xml()),
            _mock_response(search_xml),
        ]
        with patch.object(client._session, "get", side_effect=responses):
            movie = client.find_movie("UnknownFilm", 2021)
        self.assertIsNone(movie)

    def test_returns_none_on_request_error(self):
        client = _make_client()
        client._section_id = "1"  # skip section fetch
        with patch.object(client._session, "get",
                          side_effect=Exception("timeout")):
            movie = client.find_movie("Dune", 2021)
        self.assertIsNone(movie)


# ---------------------------------------------------------------------------
# check_before_download
# ---------------------------------------------------------------------------

class CheckBeforeDownloadTests(unittest.TestCase):
    def _movie(self, resolution: str = "1080") -> PlexMovie:
        return PlexMovie(
            title="Dune", year=2021, rating_key="10",
            resolution=resolution, added_at=0, file_paths=[],
        )

    def test_same_quality_warns_same(self):
        result = check_before_download(self._movie("1080"), "1080")
        self.assertEqual(result.action, "warn_same")

    def test_plex_has_better_warns_better(self):
        result = check_before_download(self._movie("4k"), "1080")
        self.assertEqual(result.action, "warn_better")

    def test_plex_has_worse_offers_upgrade(self):
        result = check_before_download(self._movie("720"), "1080")
        self.assertEqual(result.action, "offer_upgrade")

    def test_unknown_requested_resolution_warns_same(self):
        result = check_before_download(self._movie("1080"), "")
        self.assertIn(result.action, ("warn_same", "warn_better"))

    def test_result_contains_plex_movie(self):
        movie = self._movie("1080")
        result = check_before_download(movie, "1080")
        self.assertIs(result.plex_movie, movie)


if __name__ == "__main__":
    unittest.main()
