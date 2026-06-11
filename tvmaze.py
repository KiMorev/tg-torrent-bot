"""Small TVmaze client for validating TV season episode totals."""

from __future__ import annotations

import logging
import threading
from datetime import date

import requests

logger = logging.getLogger("tg_torrent_drop")

_API_BASE = "https://api.tvmaze.com"
_REQUEST_TIMEOUT = 10


class TVmazeClient:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def season_episode_count(
        self,
        *,
        season_number: int,
        imdb_id: str = "",
        tvdb_id: str = "",
    ) -> int | None:
        """Return total episodes for a TV season, or None when unknown."""
        try:
            season = int(season_number)
        except (TypeError, ValueError):
            return None
        if season <= 0:
            return None

        with self._lock:
            show_id = self._lookup_show_id(imdb_id=imdb_id, tvdb_id=tvdb_id)
            if show_id is None:
                return None
            return self._season_episode_count(show_id, season)

    def season_episode_counts(
        self,
        *,
        imdb_id: str = "",
        tvdb_id: str = "",
    ) -> dict[int, int]:
        """Return {season_number: episode_count} for a TV show."""
        with self._lock:
            show_id = self._lookup_show_id(imdb_id=imdb_id, tvdb_id=tvdb_id)
            if show_id is None:
                return {}
            return self._season_episode_counts(show_id)

    def season_aired_episode_count(
        self,
        *,
        season_number: int,
        imdb_id: str = "",
        tvdb_id: str = "",
    ) -> int | None:
        """Return episodes with an air date up to today."""
        try:
            season = int(season_number)
        except (TypeError, ValueError):
            return None
        if season <= 0:
            return None

        with self._lock:
            show_id = self._lookup_show_id(imdb_id=imdb_id, tvdb_id=tvdb_id)
            if show_id is None:
                return None
            return self._season_aired_episode_count(show_id, season)

    def season_released_episode_counts(
        self,
        *,
        imdb_id: str = "",
        tvdb_id: str = "",
    ) -> dict[int, int]:
        """Return seasons whose premiere date is not in the future."""
        with self._lock:
            show_id = self._lookup_show_id(imdb_id=imdb_id, tvdb_id=tvdb_id)
            if show_id is None:
                return {}
            return self._season_released_episode_counts(show_id)

    def _lookup_show_id(self, *, imdb_id: str = "", tvdb_id: str = "") -> int | None:
        for param, value in (("thetvdb", tvdb_id), ("imdb", imdb_id)):
            value = str(value or "").strip()
            if not value:
                continue
            try:
                response = self._session.get(
                    f"{_API_BASE}/lookup/shows",
                    params={param: value},
                    timeout=_REQUEST_TIMEOUT,
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError, TypeError) as exc:
                logger.debug("TVmaze show lookup failed source=%s id=%s: %s", param, value, exc)
                continue
            show_id = self._safe_int(data.get("id")) if isinstance(data, dict) else None
            if show_id is not None:
                return show_id
        return None

    def _season_episode_count(self, show_id: int, season_number: int) -> int | None:
        try:
            response = self._session.get(
                f"{_API_BASE}/shows/{show_id}/seasons",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TVmaze seasons lookup failed show_id=%s: %s", show_id, exc)
            return None
        if not isinstance(data, list):
            return None
        for season in data:
            if not isinstance(season, dict):
                continue
            if self._safe_int(season.get("number")) != season_number:
                continue
            total = self._safe_int(season.get("episodeOrder"))
            if total is not None:
                return total
            season_id = self._safe_int(season.get("id"))
            if season_id is None:
                return None
            return self._episode_count_from_season_id(season_id)
        return None

    def _season_aired_episode_count(self, show_id: int, season_number: int) -> int | None:
        try:
            response = self._session.get(
                f"{_API_BASE}/shows/{show_id}/seasons",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TVmaze seasons lookup failed show_id=%s: %s", show_id, exc)
            return None
        if not isinstance(data, list):
            return None
        for season in data:
            if not isinstance(season, dict):
                continue
            if self._safe_int(season.get("number")) != season_number:
                continue
            season_id = self._safe_int(season.get("id"))
            if season_id is None:
                return None
            aired = self._aired_episode_count_from_season_id(season_id)
            if aired is not None:
                return aired
            end_date = self._parse_date(season.get("endDate"))
            total = self._safe_int(season.get("episodeOrder"))
            if end_date is not None and end_date <= date.today() and total is not None:
                return total
            return None
        return None

    def _season_episode_counts(self, show_id: int) -> dict[int, int]:
        try:
            response = self._session.get(
                f"{_API_BASE}/shows/{show_id}/seasons",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TVmaze seasons lookup failed show_id=%s: %s", show_id, exc)
            return {}
        if not isinstance(data, list):
            return {}

        result: dict[int, int] = {}
        for season in data:
            if not isinstance(season, dict):
                continue
            season_number = self._safe_int(season.get("number"))
            if season_number is None:
                continue
            total = self._safe_int(season.get("episodeOrder"))
            if total is None:
                season_id = self._safe_int(season.get("id"))
                if season_id is not None:
                    total = self._episode_count_from_season_id(season_id)
            if total is not None:
                result[season_number] = total
        return result

    def _season_released_episode_counts(self, show_id: int) -> dict[int, int]:
        try:
            response = self._session.get(
                f"{_API_BASE}/shows/{show_id}/seasons",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TVmaze seasons lookup failed show_id=%s: %s", show_id, exc)
            return {}
        if not isinstance(data, list):
            return {}

        result: dict[int, int] = {}
        for season in data:
            if not isinstance(season, dict):
                continue
            season_number = self._safe_int(season.get("number"))
            if season_number is None:
                continue
            premiere_date = self._parse_date(season.get("premiereDate"))
            if premiere_date is None or premiere_date > date.today():
                continue
            total = self._safe_int(season.get("episodeOrder"))
            if total is None:
                season_id = self._safe_int(season.get("id"))
                if season_id is not None:
                    total = self._aired_episode_count_from_season_id(season_id)
            if total is not None:
                result[season_number] = total
        return result

    def _episode_count_from_season_id(self, season_id: int) -> int | None:
        try:
            response = self._session.get(
                f"{_API_BASE}/seasons/{season_id}/episodes",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TVmaze season episodes lookup failed season_id=%s: %s", season_id, exc)
            return None
        if isinstance(data, list):
            return len(data) or None
        return None

    def _aired_episode_count_from_season_id(self, season_id: int) -> int | None:
        try:
            response = self._session.get(
                f"{_API_BASE}/seasons/{season_id}/episodes",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TVmaze season episodes lookup failed season_id=%s: %s", season_id, exc)
            return None
        if not isinstance(data, list):
            return None
        aired = 0
        dated = 0
        for episode in data:
            if not isinstance(episode, dict):
                continue
            air_date = self._parse_date(episode.get("airdate"))
            if air_date is None:
                continue
            dated += 1
            if air_date <= date.today():
                aired += 1
        if dated:
            return aired or None
        return None

    @staticmethod
    def _safe_int(value: object) -> int | None:
        try:
            parsed = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_date(value: object) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
