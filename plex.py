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
class PlexMovie:
    title: str
    year: int
    rating_key: str
    resolution: str        # "4k", "1080", "720", "480", "sd", ""
    added_at: int          # Unix timestamp (addedAt field from Plex)
    file_paths: list[str] = field(default_factory=list)  # Media[].Part[].file


@dataclass
class PlexCheckResult:
    """Result of a pre-download Plex duplicate check."""
    plex_movie: PlexMovie
    # warn_same    — same or equivalent quality already in Plex
    # warn_better  — Plex already has better quality than requested
    # offer_upgrade — Plex has lower quality; downloading would be an upgrade
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
    ) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._section_id: str | None = movie_section_id
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
    )


# ------------------------------------------------------------------
# Quality-based pre-download check (stateless helper)
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
