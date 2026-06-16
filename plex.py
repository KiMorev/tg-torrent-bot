"""Plex Media Server client.

Thin synchronous wrapper around the Plex HTTP API intended to be called via
``asyncio.to_thread``.  No external dependencies beyond ``requests``.

Typical usage::

    client = PlexClient("http://192.168.1.103:32400", "mytoken")
    movies = client.get_all_movies()
    match  = client.find_movie("Dune", 2021)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import requests

logger = logging.getLogger("tg_torrent_drop")

# Seconds before an HTTP request to Plex times out.
_REQUEST_TIMEOUT = 10

# Plex section types
_SECTION_TYPE_MOVIE = "movie"
_SECTION_TYPE_SHOW = "show"

# Resolution ranking for quality comparison (higher = better)
_RESOLUTION_RANK: dict[str, int] = {
    "4k":   4,
    "1080": 3,
    "720":  2,
    "480":  1,
    "sd":   0,
    "":     -1,
}


class PlexAPIError(Exception):
    """Base class for all Plex API errors. Carries an *error_kind* string
    for diagnostics classification (``"auth"``, ``"timeout"``, ``"network"``,
    ``"xml"``, ``"http"``, ``"other"``).
    """

    error_kind: str = "other"

    def __init__(self, message: str = "", error_kind: str | None = None) -> None:
        super().__init__(message)
        if error_kind is not None:
            self.error_kind = error_kind


class PlexAuthError(PlexAPIError):
    """Plex returned HTTP 401 — token is invalid or revoked."""
    error_kind = "auth"


class PlexTimeoutError(PlexAPIError):
    """Plex did not respond within the request timeout."""
    error_kind = "timeout"


class PlexConnectionError(PlexAPIError):
    """Could not establish a connection to Plex (DNS, refused, network down)."""
    error_kind = "network"


class PlexParseError(PlexAPIError):
    """Plex returned a body that could not be parsed as XML (often HTML error page)."""
    error_kind = "xml"


@dataclass
class PlexSection:
    key: str
    title: str
    type: str


@dataclass
class PlexMovie:
    title: str
    year: int
    rating_key: str
    resolution: str        # "4k", "1080", "720", "480", "sd", ""
    added_at: int          # Unix timestamp (addedAt field from Plex)
    file_paths: list[str] = field(default_factory=list)  # Media[].Part[].file
    guid: str = ""         # "plex://movie/...", "kp://N", "local://..." (unmatched)


@dataclass
class PlexCheckResult:
    """Result of a pre-download Plex duplicate check."""
    plex_movie: PlexMovie
    # warn_same    — same or equivalent quality already in Plex
    # warn_better  — Plex already has better quality than requested
    # offer_upgrade — Plex has lower quality; downloading would be an upgrade
    action: str  # Literal["warn_same", "warn_better", "offer_upgrade"]


@dataclass
class PlexSeason:
    """A single season of a TV show in the Plex library."""
    rating_key: str
    season_number: int
    episode_count: int                        # leafCount from Plex
    file_paths: list[str] = field(default_factory=list)   # all episode .file paths
    resolution: str = ""                      # best across episodes ("4k"/"1080"/…)


@dataclass
class PlexShow:
    """A TV show in the Plex library. ``seasons`` is lazily populated via
    :meth:`PlexClient.get_show_seasons` — empty by default to avoid eagerly
    fetching every show's children during the cache refresh."""
    title: str
    year: int
    rating_key: str
    seasons: dict[int, PlexSeason] = field(default_factory=dict)
    guid: str = ""         # "plex://show/...", "kp://N", "local://..." (unmatched)
    original_title: str = ""
    external_guids: list[str] = field(default_factory=list)  # e.g. imdb://..., tmdb://..., tvdb://...


@dataclass
class PlexSeriesCheckResult:
    """Result of a pre-download Plex duplicate check for a TV season."""
    show: PlexShow
    season: PlexSeason
    action: str  # Literal["warn_same", "warn_better", "offer_upgrade"]


def _normalise_resolution(raw: str) -> str:
    """Normalise Plex videoResolution value to a canonical string."""
    r = (raw or "").strip().lower()
    if r in ("4k", "2160", "2160p", "uhd"):
        return "4k"
    if r in ("1080", "1080p", "1080i"):
        return "1080"
    if r in ("720", "720p"):
        return "720"
    if r in ("480", "480p", "576", "576p"):
        return "480"
    if r == "sd":
        return "sd"
    return ""


def compare_quality(have: str, want: str) -> str:
    """Compare two normalised resolution strings.

    Returns:
        "same"    — equal or both unknown
        "better"  — *have* is better than *want*
        "worse"   — *have* is worse than *want*
    """
    r_have = _RESOLUTION_RANK.get(have, -1)
    r_want = _RESOLUTION_RANK.get(want, -1)
    if r_have == r_want:
        return "same"
    return "better" if r_have > r_want else "worse"


class PlexClient:
    """Synchronous Plex HTTP API client."""

    def __init__(
        self,
        url: str,
        token: str,
        movie_section_id: str | None = None,
        show_section_id: str | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._section_id: str | None = movie_section_id
        self._show_section_id: str | None = show_section_id
        self._machine_id: str | None = None
        self._session = requests.Session()
        self._session.headers.update({
            "X-Plex-Token": token,
            "Accept": "application/xml",
        })

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> ElementTree.Element:
        url = f"{self._base}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        except requests.Timeout as exc:
            raise PlexTimeoutError(f"Timeout connecting to {path}") from exc
        except requests.ConnectionError as exc:
            raise PlexConnectionError(f"Connection failed: {exc}") from exc
        except requests.RequestException as exc:
            raise PlexAPIError(f"Request failed: {exc}", error_kind="other") from exc

        if resp.status_code == 401:
            raise PlexAuthError("Invalid Plex token (HTTP 401)")
        if not resp.ok:
            raise PlexAPIError(
                f"HTTP {resp.status_code} from {path}",
                error_kind="http",
            )

        try:
            return ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as exc:
            # Plex returned non-XML (HTML error page, truncated body, etc.)
            raise PlexParseError(f"Malformed response from {path}: {exc}") from exc

    def _get_ok(self, path: str, **params: Any) -> bool:
        url = f"{self._base}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        except requests.Timeout as exc:
            raise PlexTimeoutError(f"Timeout connecting to {path}") from exc
        except requests.ConnectionError as exc:
            raise PlexConnectionError(f"Connection failed: {exc}") from exc
        except requests.RequestException as exc:
            raise PlexAPIError(f"Request failed: {exc}", error_kind="other") from exc

        if resp.status_code == 401:
            raise PlexAuthError("Invalid Plex token (HTTP 401)")
        if not resp.ok:
            raise PlexAPIError(
                f"HTTP {resp.status_code} from {path}",
                error_kind="http",
            )
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """Return True if the Plex server responds to /identity."""
        try:
            self._get("/identity")
            return True
        except Exception as exc:
            logger.debug("Plex health check failed: %s", exc)
            return False

    def get_machine_id(self) -> str:
        """Return the server's machineIdentifier (cached after first call)."""
        if self._machine_id:
            return self._machine_id
        root = self._get("/identity")
        self._machine_id = root.get("machineIdentifier", "")
        return self._machine_id

    def find_movie_section(self) -> str:
        """Auto-detect the first movie library section ID."""
        root = self._get("/library/sections")
        for directory in root.findall("Directory"):
            if directory.get("type") == _SECTION_TYPE_MOVIE:
                return directory.get("key", "")
        return ""

    def list_sections(self) -> list[PlexSection]:
        root = self._get("/library/sections")
        sections: list[PlexSection] = []
        for directory in root.findall("Directory"):
            key = directory.get("key", "")
            if not key:
                continue
            sections.append(PlexSection(
                key=key,
                title=directory.get("title", ""),
                type=directory.get("type", ""),
            ))
        return sections

    def find_section_by_title(self, title: str) -> str:
        wanted = str(title or "").strip().casefold()
        if not wanted:
            return ""
        for section in self.list_sections():
            if section.title.strip().casefold() == wanted:
                return section.key
        return ""

    def refresh_section(self, section_id: str) -> bool:
        if not section_id:
            return False
        return self._get_ok(f"/library/sections/{section_id}/refresh")

    def get_section_videos(self, section_id: str) -> list[PlexMovie]:
        if not section_id:
            return []
        root = self._get(f"/library/sections/{section_id}/all")
        return [_parse_video(v) for v in root.findall("Video")]

    def _ensure_section(self) -> str:
        if not self._section_id:
            self._section_id = self.find_movie_section()
        return self._section_id or ""

    def get_all_movies(self) -> list[PlexMovie]:
        """Fetch all movies from the movie library section."""
        section = self._ensure_section()
        if not section:
            logger.warning("Plex: no movie section found")
            return []
        root = self._get(f"/library/sections/{section}/all", type=1)
        return [_parse_video(v) for v in root.findall("Video")]

    def find_movie(self, title: str, year: int) -> PlexMovie | None:
        """Search the movie section for *title* + *year*.

        Uses the Plex search endpoint; returns the first result whose year
        matches (±1 year tolerance).
        """
        section = self._ensure_section()
        if not section:
            return None
        try:
            root = self._get(
                f"/library/sections/{section}/search",
                title=title,
                type=1,
            )
        except Exception as exc:
            logger.debug("Plex search failed for %r: %s", title, exc)
            return None

        for video in root.findall("Video"):
            plex_year = int(video.get("year") or 0)
            if abs(plex_year - year) <= 1:
                return _parse_video(video)
        return None

    # ------------------------------------------------------------------
    # TV shows (Plex section type="show")
    # ------------------------------------------------------------------

    def find_show_section(self) -> str:
        """Auto-detect the first TV show library section ID. Empty string if none."""
        root = self._get("/library/sections")
        for directory in root.findall("Directory"):
            if directory.get("type") == _SECTION_TYPE_SHOW:
                return directory.get("key", "")
        return ""

    def _ensure_show_section(self) -> str:
        if not self._show_section_id:
            self._show_section_id = self.find_show_section()
        return self._show_section_id or ""

    def get_all_shows(self) -> list[PlexShow]:
        """Fetch all TV shows from the show library section.

        Returns shows with empty ``seasons`` dicts — call :meth:`get_show_seasons`
        when you actually need to inspect a specific show's seasons. This keeps
        the initial refresh cheap (1 HTTP call) regardless of how many seasons
        each show has.
        """
        section = self._ensure_show_section()
        if not section:
            logger.debug("Plex: no show section found — skipping show refresh")
            return []
        root = self._get(f"/library/sections/{section}/all", type=2)
        return [_parse_show(d) for d in root.findall("Directory")]

    def get_show_details(self, show_rating_key: str) -> PlexShow | None:
        """Fetch detailed show metadata, including external Guid children.

        Plex Series agent usually exposes a ``plex://show/...`` primary guid and
        separate ``Guid`` children such as ``imdb://...``, ``tmdb://...`` and
        ``tvdb://...`` when called with ``includeGuids=1``.
        """
        if not show_rating_key:
            return None
        try:
            root = self._get(f"/library/metadata/{show_rating_key}", includeGuids=1)
        except Exception as exc:
            logger.debug("Plex get_show_details failed for %s: %s", show_rating_key, exc)
            return None
        directory = root.find("Directory")
        if directory is None:
            directory = root.find(".//Directory")
        if directory is None:
            return None
        return _parse_show(directory)

    def get_show_seasons(self, show_rating_key: str) -> dict[int, PlexSeason]:
        """Fetch all seasons of a show plus the file paths of their episodes.

        Returns a dict keyed by season number (1, 2, …). Each season carries:
          • ``rating_key`` for deep-link building
          • ``episode_count`` from Plex's leafCount
          • ``file_paths`` — every episode's media file path
          • ``resolution`` — best resolution seen across episodes

        Episode listing is one HTTP call per season (Plex doesn't support
        nested expansion). Specials (season_number == 0) are skipped.

        Equivalent to ``get_show_seasons_lite(key, fetch_resolution_for=None)``
        with ``None`` meaning «fetch for every season». Kept for callers that
        need the full picture (polling, post-download lookup).
        """
        return self.get_show_seasons_lite(show_rating_key, fetch_resolution_for=None)

    def get_show_seasons_lite(
        self,
        show_rating_key: str,
        *,
        fetch_resolution_for: list[int] | None,
    ) -> dict[int, PlexSeason]:
        """Like :meth:`get_show_seasons` but with selective resolution fetching.

        ``fetch_resolution_for``:
          • ``None`` (default in legacy callers) — fetch episode files +
            resolution for EVERY season. Equivalent to the original method.
          • ``[]`` — skip every per-season fetch. Returns seasons with empty
            ``file_paths`` and ``resolution=""``. One HTTP call total.
          • ``[2, 5]`` — only fetch episode files for the listed seasons.
            Other seasons carry just rating_key + episode_count.

        Used by R.2 pre-check: when showing «other seasons» as context, we
        only need (season_num, episode_count) which is already in the
        list-of-seasons response. Resolution is only needed for the season
        the user is actually checking → fetch one episode-children call
        instead of N.
        """
        if not show_rating_key:
            return {}
        try:
            root = self._get(f"/library/metadata/{show_rating_key}/children")
        except Exception as exc:
            logger.debug("Plex get_show_seasons children failed for %s: %s",
                         show_rating_key, exc)
            return {}

        seasons: dict[int, PlexSeason] = {}
        for directory in root.findall("Directory"):
            season_num = int(directory.get("index") or 0)
            if season_num <= 0:
                # Specials / unsorted episodes — skip; bot deals with numbered seasons.
                continue
            season_key = directory.get("ratingKey", "")
            episode_count = int(directory.get("leafCount") or 0)
            should_fetch = (
                fetch_resolution_for is None
                or season_num in fetch_resolution_for
            )
            if should_fetch:
                file_paths, resolution = self._fetch_season_episode_files(season_key)
            else:
                file_paths, resolution = [], ""
            seasons[season_num] = PlexSeason(
                rating_key=season_key,
                season_number=season_num,
                episode_count=episode_count,
                file_paths=file_paths,
                resolution=resolution,
            )
        return seasons

    def _fetch_season_episode_files(self, season_rating_key: str) -> tuple[list[str], str]:
        """Return (file_paths, best_resolution) for episodes of a single season.

        Best-effort: missing data or transient failures yield an empty list +
        empty resolution; the caller can fall back to substring match later.
        """
        if not season_rating_key:
            return [], ""
        try:
            root = self._get(f"/library/metadata/{season_rating_key}/children")
        except Exception as exc:
            logger.debug("Plex season children fetch failed for %s: %s",
                         season_rating_key, exc)
            return [], ""

        files: list[str] = []
        best_resolution = ""
        for video in root.findall("Video"):
            for media in video.findall("Media"):
                if not best_resolution:
                    cand = _normalise_resolution(media.get("videoResolution", ""))
                    if cand:
                        best_resolution = cand
                for part in media.findall("Part"):
                    fp = part.get("file", "")
                    if fp:
                        files.append(fp)
        return files, best_resolution


# ------------------------------------------------------------------
# XML parsing helpers
# ------------------------------------------------------------------

def _parse_video(video: ElementTree.Element) -> PlexMovie:
    """Build a PlexMovie from a <Video> XML element."""
    resolution = ""
    file_paths: list[str] = []

    for media in video.findall("Media"):
        res_raw = media.get("videoResolution", "")
        if not resolution:
            resolution = _normalise_resolution(res_raw)
        for part in media.findall("Part"):
            fp = part.get("file", "")
            if fp:
                file_paths.append(fp)

    return PlexMovie(
        title=video.get("title", ""),
        year=int(video.get("year") or 0),
        rating_key=video.get("ratingKey", ""),
        resolution=resolution,
        added_at=int(video.get("addedAt") or 0),
        file_paths=file_paths,
        guid=video.get("guid", ""),
    )


def _parse_show(directory: ElementTree.Element) -> PlexShow:
    """Build a PlexShow from a <Directory> XML element (type=2 in show sections)."""
    return PlexShow(
        title=directory.get("title", ""),
        year=int(directory.get("year") or 0),
        rating_key=directory.get("ratingKey", ""),
        seasons={},  # populated lazily by get_show_seasons
        guid=directory.get("guid", ""),
        original_title=directory.get("originalTitle", ""),
        external_guids=[
            guid.get("id", "")
            for guid in directory.findall("Guid")
            if guid.get("id")
        ],
    )


def is_unmatched(entry) -> bool:
    """Return True if Plex couldn't match this file with any metadata agent.

    Plex tags such entries with a ``local://`` GUID; matched entries have
    ``plex://``, ``kinopoisk://``, ``kp://``, ``imdb://``, ``tvdb://`` or
    similar agent prefixes. Empty guid (very old / corrupt entries) is also
    treated as unmatched.
    """
    g = (getattr(entry, "guid", "") or "").lower()
    return not g or g.startswith("local://")


# ------------------------------------------------------------------
# Quality-based pre-download check (stateless helpers)
# ------------------------------------------------------------------

def check_before_download(
    plex_movie: PlexMovie,
    requested_resolution: str,
) -> PlexCheckResult:
    """Determine what to show to the user when a duplicate is found in Plex.

    *requested_resolution* should be a normalised string ("1080", "4k", etc.)
    or empty string if unknown.
    """
    cmp = compare_quality(plex_movie.resolution, requested_resolution)
    if cmp == "worse":
        action = "offer_upgrade"
    elif cmp == "better":
        action = "warn_better"
    else:
        action = "warn_same"
    return PlexCheckResult(plex_movie=plex_movie, action=action)


def check_before_download_season(
    show: PlexShow,
    season: PlexSeason,
    requested_resolution: str,
) -> PlexSeriesCheckResult:
    """Determine what to show to the user when a season is already in Plex.

    Mirrors :func:`check_before_download` but for a season's best resolution.
    """
    cmp = compare_quality(season.resolution, requested_resolution)
    if cmp == "worse":
        action = "offer_upgrade"
    elif cmp == "better":
        action = "warn_better"
    else:
        action = "warn_same"
    return PlexSeriesCheckResult(show=show, season=season, action=action)
