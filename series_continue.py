"""Helpers for the series catch-up flow.

The first layer is deliberately pure: it describes how a Plex show is identified
without depending on Telegram handlers, tracker clients, or Download Station.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from formatters import _parse_episode_info
from plex import PlexSeason, PlexShow


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
    known_total: int = 0
    quality: str = ""
    source: str = "plex"
    topic_id: str = ""
    topic_url: str = ""
    tracker: str = ""
    history_event: str = ""
    history_chat_ids: tuple[int, ...] = field(default_factory=tuple)
    history_last_episode_end: int = 0


@dataclass(frozen=True)
class SeriesMissingSeasonCandidate:
    identity: PlexSeriesIdentity
    season_number: int
    episode_count: int = 0
    present_seasons: tuple[int, ...] = field(default_factory=tuple)
    quality: str = ""
    source: str = "metadata"
    history_chat_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeriesCompleteness:
    confidence: str
    reason_for_user: str
    missing_episode_numbers: tuple[int, ...] = field(default_factory=tuple)
    known_total: int = 0


@dataclass(frozen=True)
class SeriesTopicUpdateCheck:
    action: str
    reason_for_user: str
    topic_episode_end: int = 0
    topic_total: int = 0
    baseline_episode_end: int = 0
    topic_title: str = ""


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


def build_series_catch_up_candidates(
    shows: Iterable[PlexShow],
    history_entries: Iterable[dict],
    *,
    chat_id: int | None = None,
    scope: str = "all",
    known_totals_by_show: Mapping[str, Mapping[int | str, int]] | None = None,
) -> list[SeriesCatchUpCandidate]:
    """Build the fast first-screen candidate list from Plex + local history.

    This helper is intentionally offline: it does not query trackers, external
    metadata providers, or Plex watch-state. ``PlexSeason.episode_count`` is
    treated as "episodes currently present in Plex", not as total season size.
    """
    normal_scope = (scope or "all").strip().lower()
    mine_only = normal_scope == "mine"
    history = [entry for entry in history_entries if isinstance(entry, dict)]
    candidates: list[SeriesCatchUpCandidate] = []

    for show in shows:
        identity = identity_from_plex_show(show)
        for season_number, season in sorted((show.seasons or {}).items()):
            if season_number <= 0:
                continue
            history_entry = _select_history_entry(
                show,
                season_number,
                history,
                chat_id=chat_id,
                mine_only=mine_only,
            )
            known_total = _known_total_for_show(
                show,
                season_number,
                known_totals_by_show,
            )
            if history_entry:
                candidate = _candidate_from_history(identity, season, history_entry, known_total)
                if _candidate_needs_catch_up(candidate):
                    candidates.append(candidate)
            elif not mine_only and known_total > int(season.episode_count or 0):
                candidates.append(
                    SeriesCatchUpCandidate(
                        identity=identity,
                        season_number=season_number,
                        present_count=int(season.episode_count or 0),
                        known_total=known_total,
                        quality=season.resolution or "",
                        source="plex",
                    )
                )

    return sorted(
        candidates,
        key=lambda c: (
            0 if chat_id is not None and chat_id in c.history_chat_ids else 1,
            c.identity.title.casefold(),
            c.season_number,
        ),
    )


def build_series_missing_season_candidates(
    shows: Iterable[PlexShow],
    metadata_totals_by_show: Mapping[str, Mapping[int | str, int]] | None,
    history_entries: Iterable[dict],
    *,
    chat_id: int | None = None,
    scope: str = "all",
) -> list[SeriesMissingSeasonCandidate]:
    """Build seasons absent from Plex but present in external metadata."""
    if not metadata_totals_by_show:
        return []
    normal_scope = (scope or "all").strip().lower()
    mine_only = normal_scope == "mine"
    history = [entry for entry in history_entries if isinstance(entry, dict)]
    candidates: list[SeriesMissingSeasonCandidate] = []

    for show in shows:
        metadata_totals = _known_totals_for_show(show, metadata_totals_by_show)
        if not metadata_totals:
            continue
        matching_history = _history_entries_for_show(
            show,
            history,
            chat_id=chat_id,
            mine_only=mine_only,
        )
        if mine_only and not matching_history:
            continue
        present_seasons = tuple(sorted(
            int(season_number)
            for season_number in (show.seasons or {})
            if int(season_number) > 0
        ))
        present_set = set(present_seasons)
        history_chat_ids = tuple(sorted({
            chat
            for entry in matching_history
            for chat in _entry_chat_ids(entry)
        }))
        quality = _missing_quality_for_show(show, matching_history)
        identity = identity_from_plex_show(show)
        for season_number, episode_count in sorted(metadata_totals.items()):
            if season_number in present_set:
                continue
            candidates.append(
                SeriesMissingSeasonCandidate(
                    identity=identity,
                    season_number=season_number,
                    episode_count=episode_count,
                    present_seasons=present_seasons,
                    quality=quality,
                    history_chat_ids=history_chat_ids,
                )
            )

    return sorted(
        candidates,
        key=lambda c: (
            0 if chat_id is not None and chat_id in c.history_chat_ids else 1,
            c.identity.title.casefold(),
            c.season_number,
        ),
    )


def resolve_series_completeness(
    candidate: SeriesCatchUpCandidate,
    *,
    present_episode_numbers: Iterable[int] | None = None,
    known_total: int | None = None,
    watch_state: Mapping[str, object] | None = None,
) -> SeriesCompleteness:
    """Explain why a candidate looks incomplete or uncertain.

    ``watch_state`` is accepted for caller convenience but ignored by design:
    viewed/unviewed flags must not decide whether a season can be continued.
    """
    del watch_state
    present = _normalise_episode_numbers(
        candidate.present_episode_numbers
        if present_episode_numbers is None
        else present_episode_numbers
    )
    gap = _missing_inside_present_range(present)
    if gap:
        return SeriesCompleteness(
            confidence="gap",
            reason_for_user=f"В Plex есть пропуск в сезоне: не хватает {', '.join(map(str, gap))}.",
            missing_episode_numbers=gap,
            known_total=_safe_int(known_total) or candidate.known_total,
        )

    total = _safe_int(known_total)
    if total <= 0:
        total = candidate.known_total
    present_count = int(candidate.present_count or 0)
    if total > 0 and present_count < total:
        return SeriesCompleteness(
            confidence="exact_total",
            reason_for_user=f"В Plex {present_count} из {total} серий.",
            missing_episode_numbers=_missing_after_present(present, present_count, total),
            known_total=total,
        )

    if candidate.topic_id or candidate.history_last_episode_end > 0:
        return SeriesCompleteness(
            confidence="history_partial",
            reason_for_user="Есть история прошлой загрузки, но точный размер сезона ещё не подтверждён.",
            known_total=total,
        )

    return SeriesCompleteness(
        confidence="unknown",
        reason_for_user="По быстрым данным нельзя уверенно понять, нужен ли сезон для докачки.",
        known_total=total,
    )


def resolve_same_topic_update(
    candidate: SeriesCatchUpCandidate,
    topic_title: str,
    *,
    topic_unavailable: bool = False,
) -> SeriesTopicUpdateCheck:
    if topic_unavailable:
        return SeriesTopicUpdateCheck(
            action="unknown",
            reason_for_user="Тема раздачи сейчас недоступна.",
        )
    if not candidate.topic_id:
        return SeriesTopicUpdateCheck(
            action="unknown",
            reason_for_user="Нет сохранённой темы прошлой раздачи.",
        )

    parsed = _parse_episode_info(topic_title or "")
    baseline = max(
        int(candidate.present_count or 0),
        int(candidate.history_last_episode_end or 0),
    )
    if parsed is None:
        return SeriesTopicUpdateCheck(
            action="unknown",
            reason_for_user="Не удалось понять диапазон серий в теме.",
            baseline_episode_end=baseline,
            topic_title=topic_title or "",
        )

    topic_end, topic_total = parsed
    if topic_end > baseline:
        return SeriesTopicUpdateCheck(
            action="same_topic_update",
            reason_for_user=f"В той же раздаче появились серии до {topic_end}.",
            topic_episode_end=topic_end,
            topic_total=topic_total,
            baseline_episode_end=baseline,
            topic_title=topic_title or "",
        )

    return SeriesTopicUpdateCheck(
        action="no_update",
        reason_for_user="В той же раздаче пока нет новых серий.",
        topic_episode_end=topic_end,
        topic_total=topic_total,
        baseline_episode_end=baseline,
        topic_title=topic_title or "",
    )


def _candidate_needs_catch_up(candidate: SeriesCatchUpCandidate) -> bool:
    if candidate.known_total > 0:
        return candidate.present_count < candidate.known_total
    return bool(candidate.topic_id)


def _candidate_from_history(
    identity: PlexSeriesIdentity,
    season: PlexSeason,
    entry: dict,
    fallback_total: int,
) -> SeriesCatchUpCandidate:
    known_total = _entry_int(entry, "total_episodes", "episode_total", "known_total")
    if known_total <= 0:
        known_total = fallback_total
    return SeriesCatchUpCandidate(
        identity=identity,
        season_number=season.season_number,
        present_count=int(season.episode_count or 0),
        known_total=known_total,
        quality=str(entry.get("quality") or season.resolution or ""),
        source="history",
        topic_id=str(entry.get("topic_id") or ""),
        topic_url=str(entry.get("topic_url") or ""),
        tracker=str(entry.get("tracker") or entry.get("indexer") or ""),
        history_event=str(entry.get("event") or ""),
        history_chat_ids=_entry_chat_ids(entry),
        history_last_episode_end=_entry_int(entry, "last_episode_end", "episode_end"),
    )


def _select_history_entry(
    show: PlexShow,
    season_number: int,
    history_entries: list[dict],
    *,
    chat_id: int | None,
    mine_only: bool,
) -> dict | None:
    fallback: dict | None = None
    for entry in reversed(history_entries):
        if str(entry.get("kind") or "").lower() != "series":
            continue
        if _entry_season(entry) != season_number:
            continue
        if not _history_entry_matches_show(entry, show):
            continue
        entry_chat_ids = _entry_chat_ids(entry)
        is_mine = chat_id is not None and chat_id in entry_chat_ids
        if mine_only and not is_mine:
            continue
        if is_mine:
            return entry
        if fallback is None:
            fallback = entry
    return fallback


def _history_entry_matches_show(entry: dict, show: PlexShow) -> bool:
    entry_rating_key = str(entry.get("plex_rating_key") or "").strip()
    if entry_rating_key and entry_rating_key == str(show.rating_key):
        return True

    names = {
        title_match_key(getattr(show, "title", "")),
        title_match_key(getattr(show, "original_title", "")),
    }
    names.discard("")
    for field in ("series_query", "canonical_title", "title"):
        candidate = title_match_key(entry.get(field))
        if candidate in names:
            return True
    return False


def _known_total_for_show(
    show: PlexShow,
    season_number: int,
    known_totals_by_show: Mapping[str, Mapping[int | str, int]] | None,
) -> int:
    if not known_totals_by_show:
        return 0
    for raw_key in (show.rating_key, show.guid, show.title, show.original_title):
        key = str(raw_key or "").strip()
        if not key:
            continue
        by_season = known_totals_by_show.get(key)
        if not isinstance(by_season, Mapping):
            continue
        total = _safe_int(by_season.get(season_number))
        if total <= 0:
            total = _safe_int(by_season.get(str(season_number)))
        if total > 0:
            return total
    return 0


def _known_totals_for_show(
    show: PlexShow,
    known_totals_by_show: Mapping[str, Mapping[int | str, int]] | None,
) -> dict[int, int]:
    if not known_totals_by_show:
        return {}
    totals: dict[int, int] = {}
    for raw_key in (show.rating_key, show.guid, show.title, show.original_title):
        key = str(raw_key or "").strip()
        if not key:
            continue
        by_season = known_totals_by_show.get(key)
        if not isinstance(by_season, Mapping):
            continue
        for raw_season, raw_total in by_season.items():
            season_number = _safe_int(raw_season)
            total = _safe_int(raw_total)
            if season_number > 0 and total > 0:
                totals.setdefault(season_number, total)
    return totals


def _history_entries_for_show(
    show: PlexShow,
    history_entries: list[dict],
    *,
    chat_id: int | None,
    mine_only: bool,
) -> list[dict]:
    matches: list[dict] = []
    for entry in reversed(history_entries):
        if str(entry.get("kind") or "").lower() != "series":
            continue
        if not _history_entry_matches_show(entry, show):
            continue
        entry_chat_ids = _entry_chat_ids(entry)
        is_mine = chat_id is not None and chat_id in entry_chat_ids
        if mine_only and not is_mine:
            continue
        matches.append(entry)
    return matches


def _missing_quality_for_show(show: PlexShow, history_entries: list[dict]) -> str:
    for entry in history_entries:
        quality = str(entry.get("quality") or "").strip()
        if quality:
            return quality
    for season_number, season in sorted((show.seasons or {}).items(), reverse=True):
        if season_number <= 0:
            continue
        quality = str(getattr(season, "resolution", "") or "").strip()
        if quality:
            return quality
    return ""


def _entry_season(entry: dict) -> int:
    return _entry_int(entry, "season", "season_num")


def _entry_chat_ids(entry: dict) -> tuple[int, ...]:
    ids: set[int] = set()
    chat_id = _safe_int(entry.get("chat_id"))
    if chat_id:
        ids.add(chat_id)
    raw_ids = entry.get("chat_ids")
    if isinstance(raw_ids, (list, tuple, set)):
        for raw_id in raw_ids:
            parsed = _safe_int(raw_id)
            if parsed:
                ids.add(parsed)
    return tuple(sorted(ids))


def _entry_int(entry: dict, *keys: str) -> int:
    for key in keys:
        parsed = _safe_int(entry.get(key))
        if parsed:
            return parsed
    return 0


def _normalise_episode_numbers(values: Iterable[int]) -> tuple[int, ...]:
    numbers = sorted({_safe_int(value) for value in values})
    return tuple(number for number in numbers if number > 0)


def _missing_inside_present_range(present: tuple[int, ...]) -> tuple[int, ...]:
    if not present:
        return ()
    present_set = set(present)
    return tuple(number for number in range(1, max(present) + 1) if number not in present_set)


def _missing_after_present(
    present: tuple[int, ...],
    present_count: int,
    known_total: int,
) -> tuple[int, ...]:
    if known_total <= 0:
        return ()
    present_set = set(present)
    if present_set:
        return tuple(number for number in range(1, known_total + 1) if number not in present_set)
    start = max(0, present_count) + 1
    return tuple(range(start, known_total + 1))


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalise_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


_APOSTROPHE_RE = re.compile(r"['’‘`´ʼ]")
_TITLE_KEY_SEPARATORS_RE = re.compile(r"[^0-9a-zа-яеё]+", re.IGNORECASE)


def title_match_key(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = _APOSTROPHE_RE.sub("", text)
    text = _TITLE_KEY_SEPARATORS_RE.sub(" ", text)
    return " ".join(text.split())
