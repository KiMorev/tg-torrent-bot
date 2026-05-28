"""Helpers for the series catch-up flow.

The first layer is deliberately pure: it describes how a Plex show is identified
without depending on Telegram handlers, tracker clients, or Download Station.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from plex import PlexShow


@dataclass(frozen=True)
class PlexSeriesIdentity:
    plex_rating_key: str
    plex_guid: str
    imdb_id: str = ""
    tmdb_id: str = ""
    tvdb_id: str = ""
    title: str = ""
    original_title: str = ""
    year: int = 0


@dataclass(frozen=True)
class SeriesCatchUpCandidate:
    identity: PlexSeriesIdentity
    season_number: int
    present_count: int = 0
    present_episode_numbers: tuple[int, ...] = field(default_factory=tuple)
    quality: str = ""
    source: str = "plex"
    topic_id: str = ""


def external_guid_id(external_guids: list[str] | tuple[str, ...], scheme: str) -> str:
    prefix = f"{scheme.strip().lower()}://"
    for raw in external_guids:
        text = str(raw or "").strip()
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def identity_from_plex_show(show: PlexShow) -> PlexSeriesIdentity:
    external_guids = list(getattr(show, "external_guids", []) or [])
    return PlexSeriesIdentity(
        plex_rating_key=getattr(show, "rating_key", "") or "",
        plex_guid=getattr(show, "guid", "") or "",
        imdb_id=external_guid_id(external_guids, "imdb"),
        tmdb_id=external_guid_id(external_guids, "tmdb"),
        tvdb_id=external_guid_id(external_guids, "tvdb")
        or external_guid_id(external_guids, "thetvdb"),
        title=getattr(show, "title", "") or "",
        original_title=getattr(show, "original_title", "") or "",
        year=int(getattr(show, "year", 0) or 0),
    )
