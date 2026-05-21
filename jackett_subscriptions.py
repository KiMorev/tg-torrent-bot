from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from formatters import _extract_season_from_query, _parse_episode_info


JACKETT_SUBSCRIPTION_SCHEMA = 2


_BRACKETS_RE = re.compile(r"[\[\(].*?[\]\)]")
_SPACE_RE = re.compile(r"\s+")


def _result_title(result: Any) -> str:
    return str(getattr(result, "title", "") or "")


def _result_tracker(result: Any) -> str:
    return str(getattr(result, "tracker", "") or "").strip().lower()


def _result_topic_url(result: Any) -> str:
    return str(getattr(result, "topic_url", "") or "").strip().rstrip("/").lower()


def _normalize_title(title: str) -> str:
    parts = [
        part.strip()
        for part in title.split("/")
        if not re.search(r"сезон|сери", part, re.IGNORECASE)
    ]
    if parts:
        title = " ".join(parts[:2])
    title = _BRACKETS_RE.sub(" ", title.lower())
    title = re.sub(r"\b\d{3,4}p\b", " ", title)
    title = re.sub(r"\b(web-?dl|webrip|hdtv|bdremux|bdrip|hdrip|dvdrip)\b", " ", title)
    title = re.sub(r"\b(x264|x265|h\.?264|h\.?265|hevc|avc)\b", " ", title)
    title = re.sub(r"\b(rus|eng|ukr|sub|dub|lostfilm|newstudio|kubik|jaskier)\b", " ", title)
    return _SPACE_RE.sub(" ", title).strip()


def _title_similarity(left: str, right: str) -> float:
    left_norm = _normalize_title(left)
    right_norm = _normalize_title(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def build_jackett_subscription(
    *,
    chat_id: int | None,
    query: str,
    result: dict,
    seen_results: list[dict],
    added_at: str,
    notify_mode: str = "per_episode",
) -> dict:
    """Build a Jackett subscription dict.

    ``notify_mode`` controls when push notifications fire:
      - ``per_episode``: notify on every detected episode-end advance (default,
        preserves legacy behaviour).
      - ``season_complete``: silently advance state, notify only when
        ``new_end >= total_episodes`` — one consolidated push per season.
    Both modes still trigger auto-download so Plex gets every episode file.
    """
    title = str(result.get("title") or "")
    query = str(query or title)
    episode_info = _parse_episode_info(title)
    season = _extract_season_from_query(title) or _extract_season_from_query(query)

    sub: dict[str, Any] = {
        "type": "jackett",
        "version": JACKETT_SUBSCRIPTION_SCHEMA,
        "chat_id": chat_id,
        "query": query,
        "title": title,
        "tracker": str(result.get("tracker_name") or result.get("category") or "").strip(),
        "topic_url": str(result.get("url") or "").strip(),
        "season": season,
        "seen_titles": [r["title"] for r in seen_results if r.get("title")],
        "added_at": added_at,
        "last_check": added_at,
        "notify_mode": notify_mode,
    }
    if episode_info:
        sub["last_episode_end"] = episode_info[0]
        sub["total_episodes"] = episode_info[1]
    return sub


def select_jackett_subscription_candidate(sub: dict, results: list[Any]) -> Any | None:
    if sub.get("version") != JACKETT_SUBSCRIPTION_SCHEMA:
        seen_titles = set(sub.get("seen_titles", []))
        return next((r for r in results if _result_title(r) not in seen_titles), None)

    expected_tracker = str(sub.get("tracker") or "").strip().lower()
    expected_topic_url = str(sub.get("topic_url") or "").strip().rstrip("/").lower()
    expected_title = str(sub.get("title") or "")
    expected_season = sub.get("season")
    last_episode_end = sub.get("last_episode_end")

    candidates: list[tuple[float, Any, tuple[int, int] | None]] = []
    for result in results:
        title = _result_title(result)
        if not title:
            continue

        if expected_tracker and _result_tracker(result) != expected_tracker:
            continue

        result_season = _extract_season_from_query(title)
        if expected_season and result_season != expected_season:
            continue

        episode_info = _parse_episode_info(title)
        if isinstance(last_episode_end, int):
            if not episode_info or episode_info[0] <= last_episode_end:
                continue

        topic_match = bool(expected_topic_url and _result_topic_url(result) == expected_topic_url)
        similarity = _title_similarity(expected_title, title) if expected_title else 0.0
        if expected_title and not topic_match and similarity < 0.58:
            continue

        score = similarity * 100
        if topic_match:
            score += 1000
        if episode_info:
            score += episode_info[0]
        score += min(int(getattr(result, "seeders", 0) or 0), 100) / 100
        candidates.append((score, result, episode_info))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def apply_jackett_subscription_match(sub: dict, result: Any, checked_at: str) -> None:
    title = _result_title(result)
    sub["title"] = title
    sub["tracker"] = str(getattr(result, "tracker", "") or sub.get("tracker") or "").strip()
    sub["topic_url"] = str(getattr(result, "topic_url", "") or sub.get("topic_url") or "").strip()
    sub["last_check"] = checked_at

    episode_info = _parse_episode_info(title)
    if episode_info:
        sub["last_episode_end"] = episode_info[0]
        sub["total_episodes"] = episode_info[1]

    seen_titles = list(dict.fromkeys([*sub.get("seen_titles", []), title]))
    sub["seen_titles"] = seen_titles[-100:]
