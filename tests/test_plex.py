"""Unit tests for plex.py."""

import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

import requests

from plex import (
    PlexClient,
    PlexMovie,
    PlexShow,
    PlexSeason,
    PlexCheckResult,
    PlexSeriesCheckResult,
    PlexAPIError,
    PlexAuthError,
    PlexTimeoutError,
    PlexConnectionError,
    PlexParseError,
    _normalise_resolution,
    _parse_video,
    _parse_show,
    check_before_download,
    check_before_download_season,
    compare_quality,
    is_unmatched,
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


def _make_client(show_section_id: str | None = None) -> PlexClient:
    return PlexClient("http://192.168.1.103:32400", "testtoken",
                      show_section_id=show_section_id)


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

    def test_finds_section_by_title(self):
        client = _make_client()
        xml = (
            '<MediaContainer>'
            '  <Directory type="movie" key="1" title="Movies"/>'
            '  <Directory type="movie" key="9" title="YouTube"/>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get", return_value=_mock_response(xml)):
            self.assertEqual(client.find_section_by_title("youtube"), "9")

    def test_get_section_videos_reads_generic_video_section(self):
        client = _make_client()
        xml = (
            '<MediaContainer>'
            '  <Video title="Clip [yt-abcdefghijk]" year="2026" ratingKey="99" addedAt="100">'
            '    <Media videoResolution="720"><Part file="/volume1/youtube/Clip [yt-abcdefghijk].mp4"/></Media>'
            '  </Video>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get", return_value=_mock_response(xml)):
            videos = client.get_section_videos("9")

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0].rating_key, "99")
        self.assertIn("abcdefghijk", videos[0].file_paths[0])

    def test_refresh_section_accepts_empty_success_response(self):
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 204
        resp.ok = True
        resp.content = b""
        with patch.object(client._session, "get", return_value=resp) as mock_get:
            self.assertTrue(client.refresh_section("9"))

        self.assertIn("/library/sections/9/refresh", mock_get.call_args.args[0])


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


# ---------------------------------------------------------------------------
# TV-section parsing and lookups
# ---------------------------------------------------------------------------

class ParseShowTests(unittest.TestCase):
    def test_parse_show_extracts_title_year_rating_key(self):
        xml = '<Directory title="Schitt\'s Creek" year="2015" ratingKey="42"/>'
        show = _parse_show(_xml(xml))
        self.assertEqual(show.title, "Schitt's Creek")
        self.assertEqual(show.year, 2015)
        self.assertEqual(show.rating_key, "42")
        self.assertEqual(show.seasons, {})

    def test_parse_show_handles_missing_year(self):
        xml = '<Directory title="X" ratingKey="1"/>'
        show = _parse_show(_xml(xml))
        self.assertEqual(show.year, 0)

    def test_parse_show_extracts_original_title_and_external_guids(self):
        xml = (
            '<Directory title="Белгравия" originalTitle="Belgravia" year="2020" '
            'ratingKey="2223" guid="plex://show/abc">'
            '<Guid id="imdb://tt9642982"/>'
            '<Guid id="tmdb://85862"/>'
            '<Guid id="tvdb://362204"/>'
            '</Directory>'
        )
        show = _parse_show(_xml(xml))
        self.assertEqual(show.original_title, "Belgravia")
        self.assertEqual(show.guid, "plex://show/abc")
        self.assertEqual(show.external_guids, [
            "imdb://tt9642982",
            "tmdb://85862",
            "tvdb://362204",
        ])


class PlexClientFindShowSectionTests(unittest.TestCase):
    def test_returns_first_show_section(self):
        client = _make_client()
        xml = (
            '<MediaContainer>'
            '<Directory type="movie" key="1" title="Movies"/>'
            '<Directory type="show" key="2" title="TV Shows"/>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get", return_value=_mock_response(xml)):
            self.assertEqual(client.find_show_section(), "2")

    def test_returns_empty_when_no_show_section(self):
        client = _make_client()
        xml = '<MediaContainer><Directory type="movie" key="1"/></MediaContainer>'
        with patch.object(client._session, "get", return_value=_mock_response(xml)):
            self.assertEqual(client.find_show_section(), "")


class PlexClientGetAllShowsTests(unittest.TestCase):
    def test_returns_shows_with_empty_seasons(self):
        """get_all_shows must NOT eagerly fetch seasons; they're loaded lazily."""
        client = _make_client(show_section_id="2")
        xml = (
            '<MediaContainer>'
            '<Directory title="Schitt\'s Creek" year="2015" ratingKey="100"/>'
            '<Directory title="The Wire" year="2002" ratingKey="200"/>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get", return_value=_mock_response(xml)) as mock_get:
            shows = client.get_all_shows()
        self.assertEqual(len(shows), 2)
        self.assertEqual({s.title for s in shows}, {"Schitt's Creek", "The Wire"})
        # All shows must start with empty seasons (lazy loading)
        for show in shows:
            self.assertEqual(show.seasons, {})
        # Only 1 HTTP call — no per-show season fetches
        self.assertEqual(mock_get.call_count, 1)

    def test_returns_empty_when_no_section(self):
        client = _make_client()
        # find_show_section returns "" → no further calls
        empty_sections_xml = '<MediaContainer><Directory type="movie" key="1"/></MediaContainer>'
        with patch.object(client._session, "get",
                          return_value=_mock_response(empty_sections_xml)):
            self.assertEqual(client.get_all_shows(), [])


class PlexClientGetShowDetailsTests(unittest.TestCase):
    def test_returns_show_with_external_guids(self):
        client = _make_client()
        xml = (
            '<MediaContainer>'
            '<Directory title="Белгравия" originalTitle="Belgravia" year="2020" '
            'ratingKey="2223" guid="plex://show/abc">'
            '<Guid id="imdb://tt9642982"/>'
            '<Guid id="tmdb://85862"/>'
            '<Guid id="tvdb://362204"/>'
            '</Directory>'
            '</MediaContainer>'
        )
        with patch.object(client._session, "get", return_value=_mock_response(xml)) as mock_get:
            show = client.get_show_details("2223")

        self.assertIsNotNone(show)
        self.assertEqual(show.title, "Белгравия")
        self.assertEqual(show.original_title, "Belgravia")
        self.assertEqual(show.external_guids, [
            "imdb://tt9642982",
            "tmdb://85862",
            "tvdb://362204",
        ])
        self.assertEqual(mock_get.call_args.kwargs["params"], {"includeGuids": 1})

    def test_returns_none_for_empty_rating_key(self):
        client = _make_client()
        self.assertIsNone(client.get_show_details(""))


class PlexClientGetShowSeasonsTests(unittest.TestCase):
    def test_returns_seasons_keyed_by_number_with_episode_files(self):
        client = _make_client()
        # First call: show's children (seasons), then one call per season for episodes
        seasons_xml = (
            '<MediaContainer>'
            '<Directory ratingKey="11" index="1" leafCount="10" title="Season 1"/>'
            '<Directory ratingKey="12" index="2" leafCount="8" title="Season 2"/>'
            '</MediaContainer>'
        )
        season1_episodes = (
            '<MediaContainer>'
            '<Video><Media videoResolution="1080">'
            '<Part file="/video/Show/S01/E01.mkv"/>'
            '</Media></Video>'
            '<Video><Media videoResolution="1080">'
            '<Part file="/video/Show/S01/E02.mkv"/>'
            '</Media></Video>'
            '</MediaContainer>'
        )
        season2_episodes = (
            '<MediaContainer>'
            '<Video><Media videoResolution="2160">'
            '<Part file="/video/Show/S02/E01.mkv"/>'
            '</Media></Video>'
            '</MediaContainer>'
        )
        responses = [
            _mock_response(seasons_xml),
            _mock_response(season1_episodes),
            _mock_response(season2_episodes),
        ]
        with patch.object(client._session, "get", side_effect=responses):
            seasons = client.get_show_seasons("100")
        self.assertEqual(set(seasons.keys()), {1, 2})
        self.assertEqual(seasons[1].episode_count, 10)
        self.assertEqual(seasons[1].resolution, "1080")
        self.assertEqual(len(seasons[1].file_paths), 2)
        self.assertEqual(seasons[2].resolution, "4k")

    def test_skips_specials_season_zero(self):
        client = _make_client()
        seasons_xml = (
            '<MediaContainer>'
            '<Directory ratingKey="10" index="0" leafCount="3" title="Specials"/>'
            '<Directory ratingKey="11" index="1" leafCount="10" title="Season 1"/>'
            '</MediaContainer>'
        )
        # Episodes for season 1 — needed because season 0 is filtered before fetch
        season1_episodes = '<MediaContainer><Video><Media><Part file="/x.mkv"/></Media></Video></MediaContainer>'
        responses = [_mock_response(seasons_xml), _mock_response(season1_episodes)]
        with patch.object(client._session, "get", side_effect=responses):
            seasons = client.get_show_seasons("100")
        self.assertNotIn(0, seasons)
        self.assertIn(1, seasons)

    def test_returns_empty_for_empty_rating_key(self):
        client = _make_client()
        self.assertEqual(client.get_show_seasons(""), {})


class CheckBeforeDownloadSeasonTests(unittest.TestCase):
    def _show_season(self, resolution: str) -> tuple[PlexShow, PlexSeason]:
        show = PlexShow(title="X", year=2020, rating_key="1", seasons={})
        season = PlexSeason(rating_key="2", season_number=3,
                            episode_count=10, file_paths=[], resolution=resolution)
        return show, season

    def test_same_quality_warns_same(self):
        show, season = self._show_season("1080")
        result = check_before_download_season(show, season, "1080")
        self.assertEqual(result.action, "warn_same")
        self.assertIs(result.show, show)
        self.assertIs(result.season, season)

    def test_plex_has_worse_offers_upgrade(self):
        show, season = self._show_season("720")
        result = check_before_download_season(show, season, "1080")
        self.assertEqual(result.action, "offer_upgrade")

    def test_plex_has_better_warns_better(self):
        show, season = self._show_season("4k")
        result = check_before_download_season(show, season, "1080")
        self.assertEqual(result.action, "warn_better")


# ---------------------------------------------------------------------------
# is_unmatched + guid parsing
# ---------------------------------------------------------------------------

class IsUnmatchedTests(unittest.TestCase):
    """Verify the boolean detector used by the admin 'unmatched in Plex' feature."""

    def _movie(self, guid: str) -> PlexMovie:
        return PlexMovie(title="X", year=2024, rating_key="1",
                         resolution="1080", added_at=0, file_paths=[], guid=guid)

    def _show(self, guid: str) -> PlexShow:
        return PlexShow(title="X", year=2024, rating_key="2", seasons={}, guid=guid)

    def test_matched_when_guid_is_plex_scheme(self):
        self.assertFalse(is_unmatched(self._movie("plex://movie/5d77688385ac6700")))
        self.assertFalse(is_unmatched(self._show("plex://show/5d77688385ac6700")))

    def test_matched_when_guid_is_kp_or_kinopoisk(self):
        self.assertFalse(is_unmatched(self._movie("kp://12345")))
        self.assertFalse(is_unmatched(self._movie("kinopoisk://12345")))

    def test_matched_when_guid_is_other_agent(self):
        self.assertFalse(is_unmatched(self._movie("imdb://tt1234567")))
        self.assertFalse(is_unmatched(self._show("thetvdb://9999")))

    def test_unmatched_when_guid_starts_local(self):
        self.assertTrue(is_unmatched(self._movie("local://12345")))
        self.assertTrue(is_unmatched(self._show("LOCAL://abc")))  # case-insensitive

    def test_unmatched_when_guid_is_empty(self):
        self.assertTrue(is_unmatched(self._movie("")))
        self.assertTrue(is_unmatched(self._show("")))


class ParseGuidTests(unittest.TestCase):
    """Ensure the new guid field is populated from XML by both _parse_video and _parse_show."""

    def test_parse_video_extracts_guid_attribute(self):
        xml = (
            '<Video title="Dune" year="2021" ratingKey="10" '
            'guid="plex://movie/5d77688385ac6700" addedAt="1700000000">'
            '<Media videoResolution="1080">'
            '<Part file="/movies/Dune.mkv"/>'
            '</Media></Video>'
        )
        movie = _parse_video(_xml(xml))
        self.assertEqual(movie.guid, "plex://movie/5d77688385ac6700")

    def test_parse_video_defaults_empty_when_no_guid(self):
        xml = (
            '<Video title="X" year="2020" ratingKey="11">'
            '<Media videoResolution="720"><Part file="/x.mkv"/></Media></Video>'
        )
        movie = _parse_video(_xml(xml))
        self.assertEqual(movie.guid, "")

    def test_parse_show_extracts_guid_attribute(self):
        xml = '<Directory title="X" year="2020" ratingKey="99" guid="plex://show/abc"/>'
        show = _parse_show(_xml(xml))
        self.assertEqual(show.guid, "plex://show/abc")

    def test_parse_show_local_guid_means_unmatched(self):
        xml = '<Directory title="X" ratingKey="99" guid="local://99"/>'
        show = _parse_show(_xml(xml))
        self.assertTrue(is_unmatched(show))


if __name__ == "__main__":
    unittest.main()
