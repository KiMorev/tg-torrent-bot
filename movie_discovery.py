from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger("tg_torrent_drop")


BAD_QUALITY_RE = re.compile(r"\b(camrip|cam|hdcam|ts|telesync|tc|hdts|screener|scr|workprint)\b", re.I)
SERIES_RE = re.compile(r"([сc]езон|[сc]ерии|s\d{1,2}\s*e\d{1,2}|\bs\d{1,2}\b|\be\d{1,2}\b|tv[-\s]?series)", re.I)
ADULT_RE = re.compile(
    r"\b(18\+|xxx|porn|porno|erotic|эротик|порно|для взрослых|adult|sex|hentai|хентай)\b",
    re.I,
)
COLLECTION_RE = re.compile(r"\b(collection|сборник|коллекци[яи]|трилоги[яи]|дилоги[яи])\b", re.I)
EXTRA_RE = re.compile(r"\b(trailer|sample|extras?|bonus|трейлер|сэмпл|sample)\b", re.I)
YEAR_RE = re.compile(r"\b(20[2-9]\d|19\d{2})\b")

QUALITY_LIMITS = {
    "2160p": {"hard": 4.5, "preferred": 6.0, "score": 500},
    "1080p": {"hard": 1.4, "preferred": 2.0, "score": 350},
    "720p": {"hard": 0.7, "preferred": 1.0, "score": 180},
}
QUALITY_ALIASES = {
    "4k": "2160p",
    "uhd": "2160p",
    "2160p": "2160p",
    "1080p": "1080p",
    "1080i": "1080p",
    "720p": "720p",
}
SOURCE_SCORE = {
    "bdremux": 500,
    "blu-ray": 430,
    "bluray": 430,
    "web-dl": 390,
    "webdl": 390,
    "webrip": 330,
    "bdrip": 300,
    "hdrip": 120,
}
YEAR_SCORE_STEP = 180

# Seen-fingerprints retention settings
SEEN_FP_TTL_DAYS = 60
SEEN_FP_MAX = 2000

# Normalised weights for card scoring (must sum to 1.0)
_WEIGHT_RATING = 0.35
_WEIGHT_RECENCY = 0.20
_WEIGHT_POPULARITY = 0.20
_WEIGHT_TECH = 0.25

# Normalised source quality [0–1]
_SOURCE_QUALITY: dict[str, float] = {
    "bdremux": 1.00,
    "blu-ray": 0.85,
    "bluray": 0.85,
    "web-dl": 0.75,
    "webdl": 0.75,
    "webrip": 0.60,
    "bdrip": 0.50,
    "hdrip": 0.30,
}
_QUALITY_LEVEL: dict[str, float] = {
    "2160p": 1.00,
    "1080p": 0.75,
    "720p": 0.50,
}


def discovery_years(now: datetime) -> set[int]:
    return {now.year, now.year - 1}


def discovery_queries(now: datetime, qualities: list[str]) -> list[str]:
    return [f"{year} {quality}" for year in sorted(discovery_years(now), reverse=True) for quality in qualities]


def parse_qualities(raw: str) -> list[str]:
    result = []
    for value in raw.split(","):
        quality = QUALITY_ALIASES.get(value.strip().lower())
        if quality and quality not in result:
            result.append(quality)
    return result or ["1080p"]


def parse_size_gb(size: str) -> float:
    match = re.search(r"([\d.,]+)\s*(tb|gb|mb|kb)\b", size.lower())
    if not match:
        return 0.0
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    if unit == "tb":
        return value * 1024
    if unit == "gb":
        return value
    if unit == "mb":
        return value / 1024
    return value / (1024 * 1024)


def parse_published_at(raw: str) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_recent_published_at(raw: str, *, now: datetime, max_age_days: int) -> bool:
    published_at = parse_published_at(raw)
    if published_at is None:
        return False
    comparable_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return comparable_now - timedelta(days=max_age_days) <= published_at <= comparable_now + timedelta(minutes=10)


def detect_quality(title: str) -> str | None:
    lower = title.lower()
    for token, quality in QUALITY_ALIASES.items():
        if re.search(rf"\b{re.escape(token)}\b", lower):
            return quality
    return None


def extract_year(title: str) -> int | None:
    matches = YEAR_RE.findall(title)
    return int(matches[-1]) if matches else None


def is_noise_title(title: str, category: str = "") -> bool:
    text = f"{title} {category}"
    return bool(
        BAD_QUALITY_RE.search(text)
        or SERIES_RE.search(text)
        or ADULT_RE.search(text)
        or COLLECTION_RE.search(text)
        or EXTRA_RE.search(text)
    )


_AUDIO_NOISE = frozenset({
    "original", "rus", "ru", "eng", "en", "dub", "dubbed",
    "sub", "subs", "kaz", "deu", "fra", "ita", "heb",
})


def normalize_movie_title(title: str) -> str:
    # Strip leading "[year, tech specs]" prefix (Jackett-style) before normal processing
    stripped = re.sub(r"^\s*\[\s*\d{4}[^\]]*\]\s*", "", title)
    work_title = stripped if stripped.strip() else title

    cleaned = YEAR_RE.split(work_title, maxsplit=1)[0]
    cleaned = cleaned.split("[", 1)[0].split("(", 1)[0]
    parts = [part.strip() for part in cleaned.split("/") if part.strip()]
    if parts:
        cleaned = parts[0]
    cleaned = re.sub(r"\b(1080p|2160p|720p|web-?dl|webrip|bdremux|bdrip|bluray|blu-ray|uhd|4k)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^\wа-яА-ЯёЁ]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _has_meaningful_title(normalized: str) -> bool:
    if not normalized or len(normalized) < 2:
        return False
    words = normalized.lower().split()
    return not all(w in _AUDIO_NOISE for w in words)


def extract_alt_title(title: str) -> str:
    """Return the foreign (non-Cyrillic) name from a bilingual title like 'Русское / Foreign'.

    Returns an empty string if the title is monolingual.
    """
    # Strip leading "[year, tech]" prefix before processing
    stripped = re.sub(r"^\s*\[\s*\d{4}[^\]]*\]\s*", "", title)
    work_title = stripped if stripped.strip() else title

    # Work only with the part before the year/tech block
    pre_year = YEAR_RE.split(work_title, maxsplit=1)[0]
    pre_year = pre_year.split("[", 1)[0].split("(", 1)[0]

    parts = [p.strip() for p in pre_year.split("/") if p.strip()]
    if len(parts) < 2:
        return ""

    # Return the first part that contains no Cyrillic characters
    for part in parts[1:]:
        if not re.search(r"[а-яА-ЯёЁ]", part):
            cleaned = re.sub(r"\s+", " ", part).strip()
            if cleaned:
                return cleaned
    return ""


def movie_key(title: str, year: int) -> str:
    normalized = normalize_movie_title(title).lower()
    return f"{year}:{normalized}"


def fingerprint(release: dict) -> str:
    return "|".join(
        str(release.get(key, "") or "")
        for key in ("source", "tracker", "topic_id", "topic_url", "title", "size")
    )


def evaluate_result(
    result: Any,
    *,
    source: str,
    allowed_years: set[int],
    qualities: set[str],
) -> tuple[dict | None, str]:
    title = str(getattr(result, "title", "") or "")
    category = str(getattr(result, "category", "") or getattr(result, "tracker", "") or "")
    if not title:
        return None, "empty_title"
    if is_noise_title(title, category):
        return None, "noise_title"

    year = extract_year(title)
    if year not in allowed_years:
        return None, "year_not_allowed"

    quality = detect_quality(title)
    if quality not in qualities:
        return None, "quality_not_allowed"

    size = str(getattr(result, "size", "") or "")
    size_gb = parse_size_gb(size)
    limits = QUALITY_LIMITS.get(quality, {})
    if size_gb < limits.get("hard", 0):
        return None, "size_too_small"

    seeders = int(getattr(result, "seeders", 0) or 0)
    topic_id = str(getattr(result, "topic_id", "") or "")
    topic_url = str(getattr(result, "topic_url", "") or "")
    tracker = str(getattr(result, "tracker", "") or ("rutracker" if source == "rutracker" else ""))
    url = topic_url or (f"https://rutracker.org/forum/viewtopic.php?t={topic_id}" if topic_id else "")

    year_score = max(0, (year - min(allowed_years)) * YEAR_SCORE_STEP) if allowed_years else 0
    score = limits.get("score", 0) + year_score + min(seeders, 300)
    if size_gb >= limits.get("preferred", 0):
        score += 100
    lower = title.lower()
    score += max((points for token, points in SOURCE_SCORE.items() if token in lower), default=0)

    movie_title = normalize_movie_title(title)
    if not _has_meaningful_title(movie_title):
        return None, "no_movie_title"

    alt_title = extract_alt_title(title)

    release = {
        "source": source,
        "title": title,
        "movie_title": movie_title,
        "alt_title": alt_title,
        "year": year,
        "quality": quality,
        "size": size,
        "size_gb": round(size_gb, 2),
        "seeders": seeders,
        "tracker": tracker,
        "topic_id": topic_id,
        "topic_url": topic_url,
        "url": url,
        "magnet_url": getattr(result, "magnet_url", None),
        "torrent_url": getattr(result, "torrent_url", None),
        "published_at": getattr(result, "published_at", ""),
        "score": score,
    }
    return release, "accepted"


def release_from_result(result: Any, *, source: str, allowed_years: set[int], qualities: set[str]) -> dict | None:
    release, _reason = evaluate_result(result, source=source, allowed_years=allowed_years, qualities=qualities)
    return release


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse a stored fingerprint timestamp in various formats."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return parse_published_at(ts)


def prune_seen_fingerprints(
    seen: dict[str, str],
    *,
    now: datetime,
    ttl_days: int = SEEN_FP_TTL_DAYS,
    max_entries: int = SEEN_FP_MAX,
) -> dict[str, str]:
    """Remove expired and excess entries from the seen-fingerprints dict.

    ``seen`` maps fingerprint → ISO/datetime timestamp string.
    Entries older than ``ttl_days`` are dropped; if more than ``max_entries``
    remain the oldest ones are trimmed.
    """
    if not seen:
        return {}
    comparable_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff = comparable_now - timedelta(days=ttl_days)

    pruned: dict[str, str] = {}
    no_ts: dict[str, str] = {}
    for fp, ts in seen.items():
        if not ts:
            no_ts[fp] = ts
            continue
        dt = _parse_timestamp(ts)
        if dt is None or dt >= cutoff:
            pruned[fp] = ts

    # Entries without timestamps are kept but deprioritised for trimming
    combined = {**no_ts, **pruned}
    if len(combined) > max_entries:
        # Keep newest max_entries by timestamp (entries without ts sort first → trimmed first)
        sorted_items = sorted(combined.items(), key=lambda item: item[1] or "")
        combined = dict(sorted_items[-max_entries:])
    return combined


def _compute_card_score(card: dict, current_year: int) -> float:
    """Return a normalised [0, ~1.1] relevance score for a card.

    Weights: KP rating 35 %, recency 20 %, popularity 20 %, tech quality 25 %.
    A small novelty bonus (+0.08) is added for cards not seen before.
    """
    best_release = card["releases"][0] if card.get("releases") else {}

    # Rating [0, 1]: maps KP range [5.0, 9.5] → [0, 1]; unknown → neutral 0.5
    rating = card.get("rating")
    if rating is not None:
        rating_score = max(0.0, min((float(rating) - 5.0) / 4.5, 1.0))
    else:
        rating_score = 0.5

    # Recency: current year = 1.0, previous year = 0.65
    year = card.get("year") or 0
    year_score = 1.0 if year >= current_year else 0.65

    # Popularity: log-scaled seeders, capped at 500
    seeders = int(card.get("best_seeders") or 0)
    pop_score = min(math.log1p(seeders) / math.log1p(500), 1.0)

    # Technical quality: resolution + source type
    q_score = _QUALITY_LEVEL.get(card.get("best_quality") or "", 0.5)
    title_lower = (best_release.get("title") or "").lower()
    s_score = max(
        (v for k, v in _SOURCE_QUALITY.items() if k in title_lower),
        default=0.4,
    )
    tech_score = q_score * 0.6 + s_score * 0.4

    new_bonus = 0.08 if card.get("is_new") else 0.0

    return round(
        rating_score  * _WEIGHT_RATING
        + year_score  * _WEIGHT_RECENCY
        + pop_score   * _WEIGHT_POPULARITY
        + tech_score  * _WEIGHT_TECH
        + new_bonus,
        4,
    )


def _finalize_card(card: dict, known_fingerprints: set[str]) -> dict:
    deduped = {fingerprint(release): release for release in card["releases"]}
    releases_sorted = sorted(deduped.values(), key=lambda item: item["score"], reverse=True)
    card["releases"] = releases_sorted[:8]
    best = releases_sorted[0]
    card["best_quality"] = best["quality"]
    card["best_size"] = best["size"]
    card["best_seeders"] = best["seeders"]
    card["release_count"] = len(releases_sorted)
    card["is_new"] = any(fingerprint(release) not in known_fingerprints for release in releases_sorted)
    # Preliminary score used internally during merge; overridden by _compute_card_score in build_cards.
    card["score"] = best["score"] + (float(card.get("rating") or 0) * 30) + (250 if card["is_new"] else 0)
    return card


def _merge_duplicate_cards(cards: list[dict], known_fingerprints: set[str]) -> list[dict]:
    merged: dict[str, dict] = {}
    for card in cards:
        key = f"kp:{card['kp_id']}" if card.get("kp_id") else movie_key(card["title"], card["year"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = card
            continue
        existing["releases"].extend(card["releases"])
        existing["first_seen_at"] = min(str(existing.get("first_seen_at") or ""), str(card.get("first_seen_at") or ""))
        if not existing.get("kp_id") and card.get("kp_id"):
            for field in ("kp_id", "kp_url", "rating", "genres", "title"):
                existing[field] = card.get(field)
        if not existing.get("alt_title") and card.get("alt_title"):
            existing["alt_title"] = card["alt_title"]
        _finalize_card(existing, known_fingerprints)
    return list(merged.values())


def build_cards(
    releases: list[dict],
    *,
    now_text: str,
    known_fingerprints: set[str] | dict[str, str],
    limit: int,
    min_kp_rating: float,
    kinopoisk_client=None,
) -> dict:
    """Build scored movie cards from raw releases.

    ``known_fingerprints`` accepts either the legacy ``set[str]`` format or the
    new ``dict[str, str]`` format (fingerprint → timestamp).  The return value
    always uses the dict format so callers can persist it and prune with TTL.
    """
    # Normalise known_fingerprints to both set (is_new checks) and dict (timestamped save)
    if isinstance(known_fingerprints, dict):
        known_fps_set: set[str] = set(known_fingerprints.keys())
        known_fps_dict: dict[str, str] = dict(known_fingerprints)
    else:
        known_fps_set = set(known_fingerprints)
        known_fps_dict = {fp: "" for fp in known_fingerprints}

    try:
        current_year = int(now_text[:4])
    except (ValueError, IndexError):
        current_year = datetime.now(timezone.utc).year

    grouped: dict[str, dict] = {}
    for release in releases:
        key = movie_key(release["movie_title"], release["year"])
        card = grouped.setdefault(
            key,
            {
                "key": key,
                "title": release["movie_title"],
                "alt_title": release.get("alt_title", ""),
                "year": release["year"],
                "first_seen_at": now_text,
                "releases": [],
            },
        )
        # Promote alt_title from release if card doesn't have one yet
        if not card.get("alt_title") and release.get("alt_title"):
            card["alt_title"] = release["alt_title"]
        card["releases"].append(release)

    cards = []
    for card in grouped.values():
        _finalize_card(card, known_fps_set)

        if kinopoisk_client is not None:
            match = kinopoisk_client.search_movie(
                card["title"], card["year"], alt_title=card.get("alt_title", "")
            )
            if match is not None:
                # Discard match if KP returned a film from a very different year
                year_ok = not (
                    match.year and match.year not in {card["year"], card["year"] - 1, card["year"] + 1}
                )
                if year_ok:
                    card["kp_id"] = match.kp_id
                    card["kp_url"] = match.url
                    card["rating"] = match.rating
                    card["genres"] = match.genres
                    card["title"] = match.title
            # Apply rating filter only when we have a confirmed rating
            if card.get("rating") is not None and card["rating"] < min_kp_rating:
                continue

        cards.append(card)

    cards = _merge_duplicate_cards(cards, known_fps_set)

    # Apply normalised scoring after all KP enrichment and merging are done
    for card in cards:
        card["score"] = _compute_card_score(card, current_year)

    cards.sort(key=lambda item: item["score"], reverse=True)

    # Record new fingerprints with current timestamp
    for release in releases:
        fp = fingerprint(release)
        if fp not in known_fps_dict:
            known_fps_dict[fp] = now_text

    return {
        "updated_at": now_text,
        "seen_fingerprints": known_fps_dict,
        "cards": cards[:limit],
    }
