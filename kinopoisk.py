"""Kinopoisk client via kinopoiskapiunofficial.tech API.

All methods are synchronous — call them via asyncio.to_thread() from async code.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import requests

# Minimum gap between consecutive KP API calls (seconds).
# The free tier has a strict burst limit; sequential calls without a pause
# all get 429 after the first 3-5 requests.
_KP_REQUEST_INTERVAL = 1.0

logger = logging.getLogger("tg_torrent_drop")

# Matches kinopoisk.ru and kp.ru film/series URLs.
# Group 1 captures the path segment after /film|series|show/.
KP_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?(?:kinopoisk\.ru|kp\.ru)"
    r"/(?:film|series|show)/([^/?#\s]+)",
    re.IGNORECASE,
)

_API_BASE = "https://kinopoiskapiunofficial.tech/api"


class KinopoiskError(RuntimeError):
    pass


def extract_kp_id(text: str) -> int | None:
    """Extract a numeric Kinopoisk film ID from a URL string.

    Returns None when no valid ID can be found.
    """
    match = KP_URL_RE.search(text)
    if not match:
        return None

    slug = match.group(1).rstrip("/")
    # The slug may be just digits ("12345") or "title-12345"
    id_match = re.search(r"(\d+)$", slug)
    return int(id_match.group(1)) if id_match else None


@dataclass
class KinopoiskInfo:
    kp_id: int
    title_ru: str
    title_en: str
    year: int | None
    media_type: str   # "FILM", "TV_SERIES", "MINI_SERIES", "TV_SHOW", …
    director: str     # comma-separated, may be empty

    @property
    def search_base(self) -> str:
        """Build a clean Rutracker search query from film metadata."""
        title = self.title_ru or self.title_en
        if self.year and self.media_type == "FILM":
            return f"{title} {self.year}"
        return title

    @property
    def type_label(self) -> str:
        return {
            "FILM": "🎬 Фильм",
            "TV_SERIES": "📺 Сериал",
            "MINI_SERIES": "📺 Мини-сериал",
            "TV_SHOW": "📺 Шоу",
            "VIDEO": "🎥 Видео",
        }.get(self.media_type, "🎬")


@dataclass
class KinopoiskMovieMatch:
    kp_id: int
    title_ru: str
    title_en: str
    year: int | None
    media_type: str
    rating: float | None
    genres: list[str]
    votes: int | None = None

    @property
    def title(self) -> str:
        return self.title_ru or self.title_en

    @property
    def url(self) -> str:
        return f"https://www.kinopoisk.ru/film/{self.kp_id}/"


def _synchronized(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class KinopoiskClient:
    def __init__(self, api_key: str) -> None:
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.headers.update({
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        })

    @_synchronized
    def get_film_info(self, kp_id: int) -> KinopoiskInfo:
        """Fetch film/series details by Kinopoisk numeric ID."""
        try:
            resp = self._session.get(
                f"{_API_BASE}/v2.2/films/{kp_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise KinopoiskError(f"Не удалось получить данные из Кинопоиска: {e}") from e

        title_ru = (data.get("nameRu") or "").strip()
        title_en = (data.get("nameEn") or data.get("nameOriginal") or "").strip()
        year_raw = data.get("year")
        media_type = (data.get("type") or "FILM").upper()

        try:
            year: int | None = int(year_raw) if year_raw else None
        except (ValueError, TypeError):
            year = None

        director = self._get_director(kp_id)

        return KinopoiskInfo(
            kp_id=kp_id,
            title_ru=title_ru,
            title_en=title_en,
            year=year,
            media_type=media_type,
            director=director,
        )

    @_synchronized
    def search_series_seasons(self, title: str) -> int | None:
        """Search KinoPoisk for a TV series by title and return its season count.

        Best-effort: returns None if the title is not found or on any network/API error.
        Callers should wrap this in asyncio.to_thread and asyncio.wait_for.
        """
        try:
            resp = self._session.get(
                f"{_API_BASE}/v2.2/films",
                params={"keyword": title, "type": "TV_SERIES", "page": 1},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            return None

        items = data.get("items") or []
        if not items:
            return None

        kp_id = items[0].get("kinopoiskId")
        if not kp_id:
            return None

        return self._get_season_count(int(kp_id))

    def _get_season_count(self, kp_id: int) -> int | None:
        """Return the total number of seasons for a TV series, or None on error."""
        try:
            resp = self._session.get(
                f"{_API_BASE}/v2.2/films/{kp_id}/seasons",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            return None

        items = data.get("items") or []
        return len(items) if items else None

    @_synchronized
    def search_movie(
        self,
        title: str,
        year: int | None = None,
        alt_title: str = "",
    ) -> KinopoiskMovieMatch | None:
        """Best-effort movie lookup for discovery cards.

        When *alt_title* is present (non-Russian original title) it is tried
        FIRST: foreign-language titles give more precise KP results and avoid
        false matches on Russian titles that share the same word
        (e.g. "Вершина" → wrong Russian film instead of "Apex").
        Falls back to *title* when the alt_title search yields nothing.
        Without an alt_title the single *title* search is used as before.
        """
        if alt_title:
            logger.debug("KP searching alt_title %r first for %r", alt_title, title)
            match = self._search_movie_keyword(alt_title, year)
            if match is None:
                logger.debug("KP alt_title miss, retrying with title %r", title)
                match = self._search_movie_keyword(title, year)
        else:
            match = self._search_movie_keyword(title, year)
        return match

    def _search_movie_keyword(
        self, keyword: str, year: int | None
    ) -> KinopoiskMovieMatch | None:
        """Single keyword search against /v2.1/films/search-by-keyword."""
        # Throttle: every individual HTTP request gets its own mandatory pause
        # so bursting N cards at once never triggers the 429 rate limit.
        time.sleep(_KP_REQUEST_INTERVAL)
        # Do not append year to the keyword — the API does a full-text match and
        # adding a year number often returns zero results (e.g. "Буратино 2026").
        # Year filtering is already applied client-side below.
        try:
            resp = self._session.get(
                f"{_API_BASE}/v2.1/films/search-by-keyword",
                params={"keyword": keyword, "page": 1},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            logger.warning("KP HTTP error for %r: %s", keyword, e)
            return None
        except (requests.RequestException, ValueError, TypeError) as e:
            logger.warning("KP request error for %r: %s", keyword, e)
            return None

        films = data.get("films") or []
        if not isinstance(films, list):
            logger.warning("KP unexpected response for %r: %r", keyword, data)
            return None

        logger.debug("KP search %r (year filter=%s) → %d results", keyword, year, len(films))

        exact_match: KinopoiskMovieMatch | None = None
        close_match: KinopoiskMovieMatch | None = None

        for item in films[:10]:
            if not isinstance(item, dict):
                continue

            media_type = str(item.get("type") or "").upper()
            if media_type in {"TV_SERIES", "MINI_SERIES", "TV_SHOW"}:
                logger.debug("  skip %r: type=%s", item.get("nameRu"), media_type)
                continue

            try:
                item_year = int(item.get("year")) if item.get("year") else None
            except (TypeError, ValueError):
                item_year = None
            if year and item_year and abs(item_year - year) > 1:
                logger.debug(
                    "  skip %r: year=%s (wanted %s)", item.get("nameRu"), item_year, year
                )
                continue
            # Skip results with no year when we have a known year — avoids
            # matching "announced / coming soon" entries that share a title
            # with the actual film we're looking for (e.g. Russian "Вершина"
            # with year=None beats the correct Netflix "Apex" year=2026).
            if year and item_year is None:
                logger.debug("  skip %r: no year in KP entry (wanted %s)", item.get("nameRu"), year)
                continue

            try:
                raw_rating = str(item.get("rating") or "").replace(",", ".")
                rating = float(raw_rating) if raw_rating and raw_rating != "null" else None
            except ValueError:
                rating = None

            genres = [
                str(g.get("genre", "")).strip()
                for g in item.get("genres", [])
                if isinstance(g, dict) and g.get("genre")
            ][:3]

            try:
                kp_id = int(item.get("filmId") or item.get("kinopoiskId") or 0)
            except (TypeError, ValueError):
                kp_id = 0

            try:
                votes_raw = item.get("ratingVoteCount")
                votes: int | None = int(votes_raw) if votes_raw is not None else None
            except (TypeError, ValueError):
                votes = None

            candidate = KinopoiskMovieMatch(
                kp_id=kp_id,
                title_ru=str(item.get("nameRu") or "").strip(),
                title_en=str(item.get("nameEn") or "").strip(),
                year=item_year,
                media_type=media_type,
                rating=rating,
                genres=genres,
                votes=votes,
            )
            if not (candidate.kp_id and candidate.title):
                continue

            logger.debug(
                "KP candidate %r → %s (id=%s year=%s rating=%s)",
                keyword, candidate.title, candidate.kp_id, candidate.year, candidate.rating,
            )
            # Prefer exact year match; keep ±1 as fallback for festival-premiere offset.
            if year and item_year == year:
                if exact_match is None:
                    exact_match = candidate
            else:
                if close_match is None:
                    close_match = candidate

            # Stop early once we have an exact match — no need to scan further.
            if exact_match is not None:
                break

        result = exact_match or close_match
        if result:
            logger.debug(
                "KP match %r → %s (id=%s year=%s rating=%s%s)",
                keyword, result.title, result.kp_id, result.year, result.rating,
                " [exact year]" if result is exact_match else " [±1 year]",
            )
            return result

        logger.debug("KP no match for %r (year=%s, %d films checked)", keyword, year, len(films[:10]))
        return None

    def _get_director(self, kp_id: int) -> str:
        """Return up to two director names for display (best-effort, never raises)."""
        try:
            resp = self._session.get(
                f"{_API_BASE}/v1/staff",
                params={"filmId": kp_id},
                timeout=10,
            )
            resp.raise_for_status()
            staff = resp.json()
        except (requests.RequestException, ValueError, TypeError):
            return ""

        if not isinstance(staff, list):
            return ""

        seen: set[str] = set()
        directors: list[str] = []
        for person in staff:
            if not isinstance(person, dict):
                continue
            if person.get("professionKey") != "DIRECTOR":
                continue
            name = (person.get("nameRu") or person.get("nameEn") or "").strip()
            if name and name not in seen:
                seen.add(name)
                directors.append(name)
            if len(directors) == 2:
                break

        return ", ".join(directors)
