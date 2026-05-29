"""Pure formatting and display helpers.

All functions are stateless — they depend only on their arguments.
No imports from other project modules.
"""

from __future__ import annotations

import re


def _format_size(value: int | float | None) -> str:
    if not value:
        return "0 B"

    size = float(value)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def _progress_percent(downloaded: int | float | None, total: int | float | None) -> float | None:
    if not total:
        return None

    return min(100.0, max(0.0, (float(downloaded or 0) / float(total)) * 100))


def _progress_bar(percent: float | None, width: int = 12) -> str:
    if percent is None:
        return "░" * width

    filled = round((percent / 100) * width)
    return "█" * filled + "░" * (width - filled)


def _format_progress(downloaded: int | float | None, total: int | float | None) -> str:
    percent = _progress_percent(downloaded, total)
    if percent is None:
        return _format_size(downloaded)

    return f"{_format_size(downloaded)} из {_format_size(total)} ({percent:.1f}%)"


def _format_eta(remaining_bytes: int | float | None, speed_bytes: int | float | None) -> str:
    if not remaining_bytes or not speed_bytes:
        return "неизвестно"

    seconds = int(float(remaining_bytes) / float(speed_bytes))
    if seconds <= 0:
        return "меньше минуты"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)

    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")

    return " ".join(parts[:2]) or "меньше минуты"


def _task_remaining_bytes(task: dict, transfer: dict) -> float | None:
    total = task.get("size")
    downloaded = transfer.get("size_downloaded")
    if not total:
        return None

    return max(0.0, float(total) - float(downloaded or 0))


def _status_icon(status: str | None) -> str:
    return {
        "downloading": "⬇️",
        "seeding": "🌱",
        "finished": "✅",
        "paused": "⏸️",
        "waiting": "⏳",
        "finishing": "🔄",
        "hash_checking": "🔎",
        "error": "⚠️",
    }.get((status or "").lower(), "•")


def _status_label(status: str | None) -> str:
    return {
        "downloading": "скачивается",
        "seeding": "раздается",
        "finished": "завершено",
        "paused": "на паузе",
        "waiting": "ожидание",
        "finishing": "завершение",
        "hash_checking": "проверка",
        "error": "ошибка",
    }.get((status or "unknown").lower(), status or "unknown")


def _progress_meter(percent: float | None, width: int = 10) -> str:
    bar = _progress_bar(percent, width=width)
    if percent is None:
        return f"{bar} неизвестно"

    return f"{bar} {percent:.1f}%"


def _magnet_wait_bar(step: int, width: int = 8) -> str:
    active = step % width
    return "".join("▰" if index == active else "▱" for index in range(width))


def _magnet_wait_text(step: int, attempts: int) -> str:
    return (
        "⏳ Добавляю magnet-ссылку\n\n"
        "Ищу созданную задачу в списке загрузок.\n"
        "Обычно это занимает до 10-15 секунд.\n\n"
        f"Попытка {min(step + 1, attempts)} из {attempts}: {_magnet_wait_bar(step)}"
    )


def _format_hours(hours: float) -> str:
    if hours < 1:
        minutes = max(1, round(hours * 60))
        return f"{minutes} мин"

    if hours.is_integer():
        hours_int = int(hours)
        return f"{hours_int} ч"

    return f"{hours:.1f} ч"


def _short_title(task: dict, limit: int = 34) -> str:
    title = task.get("title") or task.get("id") or "без названия"
    if len(title) <= limit:
        return title

    return f"{title[: limit - 1]}…"


# ---------------------------------------------------------------------------
# Rutracker result scoring
# ---------------------------------------------------------------------------

# Release-type quality score (higher = better source)
_QUALITY_SCORE: dict[str, int] = {
    "web-dl": 600, "webdl": 600,
    "web-dlrip": 550, "webdlrip": 550,
    "webrip": 500,
    "bdremux": 480,
    "bdrip": 400,
    "hdtv": 300,
    "dvdremux": 280,
    "dvdrip": 250,
    "hdrip": 200,
}

# Resolution score
_RESOLUTION_SCORE: dict[str, int] = {
    "4k": 400, "2160p": 400, "uhd": 400,
    "1080p": 300, "1080i": 280,
    "720p": 150,
    "576p": 50, "480p": 50,
}

# Audio track bonus (Rutracker naming: DUB, MVO, DVO, AVO, SVO, VO)
# Higher = better (studio dub > multi-voice > dual-voice > single-voice)
_AUDIO_TYPE_BONUS: dict[str, int] = {
    "dub": 80,
    "mvo": 60,
    "dvo": 50,
    "avo": 40,
    "svo": 30,
    "vo": 30,
}
# Matches "3xDUB", "2 x MVO", "5 х Dub" (Cyrillic х), etc.
_MULTI_AUDIO_RE = re.compile(
    r"(\d+)\s*[xх]\s*(dub|mvo|dvo|avo|svo|vo)\b",
    re.IGNORECASE,
)


def _score_result(result: dict) -> float:
    """Compute a quality score for a Rutracker search result.

    Higher = better. Used to mark the recommended torrent in the results list.
    Factors: release type, resolution, audio tracks, subtitles, seeders.
    """
    title_lower = (result.get("title") or "").lower()
    seeders = int(result.get("seeders") or 0)

    quality = max(
        (s for k, s in _QUALITY_SCORE.items() if k in title_lower),
        default=0,
    )
    resolution = max(
        (s for k, s in _RESOLUTION_SCORE.items() if k in title_lower),
        default=0,
    )
    seeder_bonus = min(seeders, 500) * 0.5  # caps at 250 to avoid dominating

    # Audio bonus: count "NxTYPE" patterns (e.g. "3xDUB"), then add single-mention bonus
    found_counted: set[str] = set()
    audio_bonus = 0.0
    for m in _MULTI_AUDIO_RE.finditer(title_lower):
        count = min(int(m.group(1)), 3)           # cap multiplier at 3
        atype = m.group(2).lower()
        audio_bonus += _AUDIO_TYPE_BONUS.get(atype, 0) * count
        found_counted.add(atype)
    # Single mentions not already covered by the "Nx" pattern
    for atype, bonus in _AUDIO_TYPE_BONUS.items():
        if atype not in found_counted and re.search(rf"\b{atype}\b", title_lower):
            audio_bonus += bonus
    audio_bonus = min(audio_bonus, 250.0)   # hard cap

    # Subtitle bonus — "Sub", "Sub Rus", etc.
    sub_bonus = 60.0 if re.search(r"\bsub\b", title_lower) else 0.0

    return quality + resolution + seeder_bonus + audio_bonus + sub_bonus


# ---------------------------------------------------------------------------
# Series episode parsing
# ---------------------------------------------------------------------------

# Matches "Серии: 1-8 из 10" or "Серия: 1-8 из 10" (case-insensitive: also
# СЕРИИ, серии). Note: [яи] is a morphology variant (Серия/Серии), not a case
# class — it must stay alongside re.IGNORECASE.
_EPISODE_RE = re.compile(r"сери[яи][:\s]+(\d+)-(\d+)\s+из\s+(\d+)", re.IGNORECASE)
# English form: 'S2E1-9 of 9' → (last_end=9, total=9)
_EPISODE_EN_OF_RE = re.compile(
    r"\bS\d{1,2}E(\d{1,2})-(\d{1,2})\s+of\s+(\d{1,2})\b", re.IGNORECASE
)
# English form without `of N`: 'S2E1-9' → (last_end=9, total=last_end)
_EPISODE_EN_RE = re.compile(r"\bS\d{1,2}E(\d{1,2})-(\d{1,2})\b", re.IGNORECASE)


def _parse_episode_info(title: str) -> tuple[int, int] | None:
    """Return (current_end, total) from a series title, or None.

    Recognises both Russian Rutracker form ('Серии: 1-9 из 10') and the English
    form common on Jackett/foreign trackers ('S2E1-9 of 9' or 'S2E1-9').
    Returns None when no episode pattern is found.
    Caller should check current_end < total to decide if the series is partial.
    """
    m = _EPISODE_RE.search(title)
    if m:
        return int(m.group(2)), int(m.group(3))
    m = _EPISODE_EN_OF_RE.search(title)
    if m:
        return int(m.group(2)), int(m.group(3))
    m = _EPISODE_EN_RE.search(title)
    if m:
        end = int(m.group(2))
        return end, end
    return None


_SEASON_NO_COLON_RE = re.compile(r"\bсезон[:\s]+(\d+)\b", re.IGNORECASE)
_SEASON_PREFIX_RE = re.compile(
    r"\b0*(\d{1,2})\s*(?:[-‑–—]?\s*(?:й|ый|ой))?\s+сезон\b",
    re.IGNORECASE,
)


def _normalize_season_in_query(query: str) -> str:
    """Normalise 'Сезон' → 'Сезон: N' regardless of surrounding punctuation.

    Handles all variants:
        'сезон 10'  → 'Сезон: 10'
        'Сезон:10'  → 'Сезон: 10'
        'Сезон: 10' → 'Сезон: 10'   (already correct, no-op)
        'СЕЗОН  10' → 'Сезон: 10'   (extra whitespace)
        '1-й сезон' → 'Сезон: 1'
    Case-insensitive; replacement is always title-case 'Сезон'.
    """
    query = _SEASON_NO_COLON_RE.sub(r"Сезон: \1", query)
    return _SEASON_PREFIX_RE.sub(r"Сезон: \1", query)


def _extract_series_base_query(title: str) -> str | None:
    """Return the Russian series name suitable for a 'find another season' search.

    Input:  'Клиника / Scrubs / Сезон: 10 / Серии: 1-8 из 10 [WEB-DL]'
    Output: 'Клиника'

    Also recognises the English ``SNN`` / ``SNNeNN`` form used by Jackett-fed
    foreign trackers (e.g. 'Аркейн / Arcane / S2E1-9 of 9 [...]' → 'Аркейн').

    Returns None when the title has no season marker (i.e. it looks like a
    movie rather than a multi-season series).
    """
    if not re.search(r"сезон|\bS\d{1,2}(?:E\d|\b)", title, re.IGNORECASE):
        return None

    parts = [p.strip() for p in title.split("/")]
    ru_title = parts[0].strip() if parts else ""

    # Sanity: must be at least 2 chars and not start with a digit
    if len(ru_title) < 2 or ru_title[0].isdigit():
        return None

    return ru_title


def _extract_season_from_query(query: str) -> int | None:
    """Return the season number embedded in a search query, or None.

    Recognises both the normalised Russian form produced by
    _normalize_season_in_query and the English ``SNN`` / ``SNNeNN`` form:
        'Клиника Сезон: 10 1080p' → 10
        'СЕЗОН: 10'                → 10
        '1-й сезон'                → 1
        'Arcane S01'               → 1
        'Show S1E3'                → 1
        'Breaking Bad'             → None
    Case-insensitive.
    """
    m = re.search(r"сезон[:\s]+(\d+)\b", query, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = _SEASON_PREFIX_RE.search(query)
    if m:
        return int(m.group(1))
    m = re.search(r"\bS(\d{1,2})(?:E\d|\b)", query, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _filter_by_season(results: list[dict], season_num: int) -> list[dict]:
    """Keep only results whose title contains the given season marker.

    Matches both Russian ('Сезон: N', 'Сезон N', 'Сезон 0N', 'N-й сезон')
    and English ('S1', 'S01', 'S01E03') forms, case-insensitive, with
    boundaries so that season 1 never matches season 10/11/12…
    """
    pattern = re.compile(
        rf"(?:сезон[:\s]+0*{season_num}\b|"
        rf"\b0*{season_num}\s*(?:[-‑–—]?\s*(?:й|ый|ой))?\s+сезон\b|"
        rf"s0*{season_num}(?:e\d|\b))",
        re.IGNORECASE,
    )
    return [r for r in results if pattern.search(r.get("title", ""))]


def _quality_to_query_suffix(normalised_quality: str) -> str:
    """Convert a normalised Plex quality string into a search-query suffix.

    Used to carry the quality the user actually picked from a season's release
    into the next season's search, so the bot doesn't drop the filter.

    Examples:
        "1080" → " 1080p"
        "4k"   → " 2160p"
        "720"  → " 720p"
        "480"  → " 480p"
        ""     → "" (unknown quality → search unfiltered)
    """
    return {
        "4k": " 2160p",
        "1080": " 1080p",
        "720": " 720p",
        "480": " 480p",
    }.get(normalised_quality, "")


def _seasons_available_in_results(results: list[dict]) -> list[int]:
    """Extract every season number mentioned in result titles, sorted ascending.

    Used when a season-specific search returns 0 hits — we tell the user which
    seasons the tracker *does* have so they don't keep guessing.
    Recognises both Russian ('Сезон: N', 'N-й сезон') and English
    ('S1', 'S01', 'S01E03') forms, case-insensitive.
    """
    found: set[int] = set()
    ru_pattern = re.compile(r"сезон[:\s]+(\d+)", re.IGNORECASE)
    en_pattern = re.compile(r"\bS(\d{1,2})(?:E\d|\b)", re.IGNORECASE)
    for r in results:
        title = r.get("title") or ""
        for m in ru_pattern.finditer(title):
            found.add(int(m.group(1)))
        for m in _SEASON_PREFIX_RE.finditer(title):
            found.add(int(m.group(1)))
        for m in en_pattern.finditer(title):
            found.add(int(m.group(1)))
    return sorted(found)


def _format_sub_title(title: str) -> str:
    """Extract a short display name from a full Rutracker series title.

    Input:  'Клиника / Scrubs / Сезон: 10 / Серии: 1-9 из 10 (...) [...]'
    Output: 'Клиника / Сезон 10'
    """
    parts = [p.strip() for p in title.split("/")]
    ru_title = parts[0] if parts else title

    season = ""
    for part in parts:
        m = re.search(r"сезон[:\s]+(\d+)", part, re.IGNORECASE)
        if not m:
            m = _SEASON_PREFIX_RE.search(part)
        if m:
            season = f" / Сезон {m.group(1)}"
            break

    result = (ru_title + season).strip()
    return result[:60] if len(result) > 60 else result


# ---------------------------------------------------------------------------
# Jackett tracker abbreviations
# ---------------------------------------------------------------------------

_TRACKER_ABBR: dict[str, str] = {
    "rutracker": "RT",
    "rutor": "RuTor",
    "thepiratebay": "TPB",
    "nonameclub": "NNM",
    "bigfangroup": "BFG",
    "1337x": "1337x",
    "kinozal": "KZ",
    "lostfilm": "LF",
    "hdrezka": "HDR",
    "nnmclub": "NNM",
    "rutracker_private": "RT",
}


def _tracker_abbr(tracker_id: str) -> str:
    """Return short abbreviation for a Jackett tracker ID."""
    key = tracker_id.lower().strip()
    if key in _TRACKER_ABBR:
        return _TRACKER_ABBR[key]
    # Fallback: up to 5 uppercase chars
    clean = re.sub(r"[^a-z0-9]", "", key)
    return clean[:5].upper() if clean else tracker_id[:5].upper()
