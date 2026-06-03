"""Small TMDB client for TV season episode totals."""

from __future__ import annotations

import logging
import threading

import requests

logger = logging.getLogger("tg_torrent_drop")

_API_BASE = "https://api.themoviedb.org/3"
_REQUEST_TIMEOUT = 10


class TMDBClient:
    def __init__(self, api_token: str) -> None:
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
        })

    def season_episode_count(
        self,
        *,
        season_number: int,
        tmdb_id: str = "",
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
            tv_id = self._safe_int(tmdb_id)
            if tv_id is None:
                tv_id = self._resolve_tv_id(imdb_id=imdb_id, tvdb_id=tvdb_id)
            if tv_id is None:
                return None
            return self._season_episode_count(tv_id, season)

    def _resolve_tv_id(self, *, imdb_id: str = "", tvdb_id: str = "") -> int | None:
        for external_id, source in (
            (imdb_id, "imdb_id"),
            (tvdb_id, "tvdb_id"),
        ):
            external_id = str(external_id or "").strip()
            if not external_id:
                continue
            try:
                response = self._session.get(
                    f"{_API_BASE}/find/{external_id}",
                    params={"external_source": source},
                    timeout=_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError, TypeError) as exc:
                logger.debug("TMDB external id lookup failed source=%s id=%s: %s", source, external_id, exc)
                continue
            tv_results = data.get("tv_results") if isinstance(data, dict) else None
            if not tv_results:
                continue
            tv_id = self._safe_int(tv_results[0].get("id"))
            if tv_id is not None:
                return tv_id
        return None

    def _season_episode_count(self, tv_id: int, season_number: int) -> int | None:
        try:
            response = self._session.get(
                f"{_API_BASE}/tv/{tv_id}/season/{season_number}",
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.debug("TMDB season lookup failed tv_id=%s season=%s: %s", tv_id, season_number, exc)
            return None
        episodes = data.get("episodes") if isinstance(data, dict) else None
        if isinstance(episodes, list):
            return len(episodes) or None
        return None

    @staticmethod
    def _safe_int(value: object) -> int | None:
        try:
            parsed = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
