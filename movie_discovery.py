from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any


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


def normalize_movie_title(title: str) -> str:
    cleaned = YEAR_RE.split(title, maxsplit=1)[0]
    cleaned = cleaned.split("[", 1)[0].split("(", 1)[0]
    parts = [part.strip() for part in cleaned.split("/") if part.strip()]
    if parts:
        cleaned = parts[0]
    cleaned = re.sub(r"\b(1080p|2160p|720p|web-?dl|webrip|bdremux|bdrip|bluray|blu-ray|uhd|4k)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^\wа-яА-ЯёЁ]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


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

    release = {
        "source": source,
        "title": title,
        "movie_title": normalize_movie_title(title),
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
        _finalize_card(existing, known_fingerprints)
    return list(merged.values())


def build_cards(
    releases: list[dict],
    *,
    now_text: str,
    known_fingerprints: set[str],
    limit: int,
    min_kp_rating: float,
    kinopoisk_client=None,
) -> dict:
    grouped: dict[str, dict] = {}
    for release in releases:
        key = movie_key(release["movie_title"], release["year"])
        card = grouped.setdefault(
            key,
            {
                "key": key,
                "title": release["movie_title"],
                "year": release["year"],
                "first_seen_at": now_text,
                "releases": [],
            },
        )
        card["releases"].append(release)

    cards = []
    for card in grouped.values():
        card = _finalize_card(card, known_fingerprints)

        if kinopoisk_client is not None:
            match = kinopoisk_client.search_movie(card["title"], card["year"])
            if match is None:
                continue
            if match.year and match.year not in {card["year"], card["year"] - 1, card["year"] + 1}:
                continue
            if match.rating is not None and match.rating < min_kp_rating:
                continue
            card["kp_id"] = match.kp_id
            card["kp_url"] = match.url
            card["rating"] = match.rating
            card["genres"] = match.genres
            card["title"] = match.title

        card = _finalize_card(card, known_fingerprints)
        cards.append(card)

    cards = _merge_duplicate_cards(cards, known_fingerprints)
    cards.sort(key=lambda item: item["score"], reverse=True)
    all_fingerprints = set(known_fingerprints)
    for release in releases:
        all_fingerprints.add(fingerprint(release))

    return {
        "updated_at": now_text,
        "seen_fingerprints": sorted(all_fingerprints)[-2000:],
        "cards": cards[:limit],
    }
