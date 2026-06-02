from __future__ import annotations

import json
import random
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable


DEFAULT_SEARCH_FACTS_PATH = Path(__file__).resolve().parent / "data" / "search_facts.json"
DEFAULT_POOL_SIZE = 100
DEFAULT_REFRESH_THRESHOLD = 0.7
DEFAULT_RECENT_LIMIT = 500


@dataclass(frozen=True)
class SearchFact:
    id: str
    text: str


def load_search_facts(path: Path = DEFAULT_SEARCH_FACTS_PATH) -> list[SearchFact]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, list):
        return []

    facts: list[SearchFact] = []
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        fact_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not fact_id or not text or fact_id in seen_ids:
            continue
        seen_ids.add(fact_id)
        facts.append(SearchFact(id=fact_id, text=text))
    return facts


def format_search_fact_line(fact_text: str | None) -> str:
    if not fact_text:
        return ""
    return f"\n\nПока ждёте: {fact_text}"


def select_search_fact(
    facts: list[SearchFact],
    state: dict[str, Any] | None,
    chat_id: int,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    refresh_threshold: float = DEFAULT_REFRESH_THRESHOLD,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    choice: Callable[[list[str]], str] | None = None,
    sample: Callable[[list[str], int], list[str]] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    if not facts:
        return None, state if isinstance(state, dict) else {}

    choice = choice or random.choice
    sample = sample or random.sample
    state = state.copy() if isinstance(state, dict) else {}
    chats = state.get("chats")
    if not isinstance(chats, dict):
        chats = {}
        state["chats"] = chats

    chat_key = str(chat_id)
    chat_state = chats.get(chat_key)
    if not isinstance(chat_state, dict):
        chat_state = {}
        chats[chat_key] = chat_state

    fact_by_id = {fact.id: fact for fact in facts}
    all_ids = [fact.id for fact in facts]
    target_pool_size = min(max(1, pool_size), len(all_ids))

    recent_ids = _normalise_id_list(chat_state.get("recent_shown_ids"), fact_by_id)
    pool_ids = _normalise_id_list(chat_state.get("pool_fact_ids"), fact_by_id)
    shown_ids = _normalise_id_list(chat_state.get("shown_in_pool"), fact_by_id)

    if not pool_ids or _pool_is_stale(pool_ids, shown_ids, refresh_threshold):
        pool_ids = _build_pool(all_ids, recent_ids, target_pool_size, sample)
        shown_ids = []

    fact_id = _pick_fact_id(pool_ids, shown_ids, recent_ids, choice)
    if fact_id is None:
        pool_ids = _build_pool(all_ids, recent_ids, target_pool_size, sample)
        shown_ids = []
        fact_id = _pick_fact_id(pool_ids, shown_ids, recent_ids, choice)
    if fact_id is None:
        return None, state

    shown_ids = _append_unique(shown_ids, fact_id)
    recent_ids = _append_unique(recent_ids, fact_id)[-recent_limit:]

    chat_state["pool_id"] = _pool_id(pool_ids)
    chat_state["pool_fact_ids"] = pool_ids
    chat_state["shown_in_pool"] = shown_ids
    chat_state["recent_shown_ids"] = recent_ids

    return fact_by_id[fact_id].text, state


def _build_pool(
    all_ids: list[str],
    recent_ids: list[str],
    target_pool_size: int,
    sample: Callable[[list[str], int], list[str]],
) -> list[str]:
    recent = set(recent_ids)
    fresh_ids = [fact_id for fact_id in all_ids if fact_id not in recent]
    candidates = fresh_ids if len(fresh_ids) >= target_pool_size else all_ids
    if len(candidates) <= target_pool_size:
        return list(candidates)
    return sample(list(candidates), target_pool_size)


def _pick_fact_id(
    pool_ids: list[str],
    shown_ids: list[str],
    recent_ids: list[str],
    choice: Callable[[list[str]], str],
) -> str | None:
    shown = set(shown_ids)
    recent = set(recent_ids)
    candidates = [fact_id for fact_id in pool_ids if fact_id not in shown and fact_id not in recent]
    if not candidates:
        candidates = [fact_id for fact_id in pool_ids if fact_id not in shown]
    if not candidates:
        return None
    return choice(candidates)


def _pool_is_stale(pool_ids: list[str], shown_ids: list[str], refresh_threshold: float) -> bool:
    if not pool_ids:
        return True
    shown_in_pool = len(set(pool_ids) & set(shown_ids))
    return shown_in_pool / len(pool_ids) >= refresh_threshold


def _normalise_id_list(value: Any, fact_by_id: dict[str, SearchFact]) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        fact_id = str(item)
        if fact_id in fact_by_id and fact_id not in seen:
            ids.append(fact_id)
            seen.add(fact_id)
    return ids


def _append_unique(items: list[str], item: str) -> list[str]:
    return [existing for existing in items if existing != item] + [item]


def _pool_id(pool_ids: list[str]) -> str:
    digest = sha1("\n".join(pool_ids).encode("utf-8")).hexdigest()[:12]
    return f"pool:{digest}"
