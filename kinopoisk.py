"""Kinopoisk client via kinopoiskapiunofficial.tech API.

All methods are synchronous — call them via asyncio.to_thread() from async code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

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


class KinopoiskClient:
    def __init__(self, api_key: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        })

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
        except Exception:
            return ""

        seen: set[str] = set()
        directors: list[str] = []
        for person in staff:
            if person.get("professionKey") != "DIRECTOR":
                continue
            name = (person.get("nameRu") or person.get("nameEn") or "").strip()
            if name and name not in seen:
                seen.add(name)
                directors.append(name)
            if len(directors) == 2:
                break

        return ", ".join(directors)
