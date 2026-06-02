from __future__ import annotations

import json
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable


DEFAULT_SEARCH_FACTS_PATH = Path(__file__).resolve().parent / "data" / "search_facts.json"
DEFAULT_SEARCH_FACT_ALIASES_PATH = Path(__file__).resolve().parent / "data" / "search_fact_aliases.json"
DEFAULT_POOL_SIZE = 100
DEFAULT_REFRESH_THRESHOLD = 0.7
DEFAULT_RECENT_LIMIT = 500
DEFAULT_CATALOG_REFRESH_THRESHOLD = 0.7
MIN_CATALOG_FACTS = 40


@dataclass(frozen=True)
class SearchFact:
    id: str
    text: str
    tags: tuple[str, ...] = ()


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
        tags = tuple(
            str(tag).strip().lower()
            for tag in item.get("tags", [])
            if str(tag).strip()
        )
        seen_ids.add(fact_id)
        facts.append(SearchFact(id=fact_id, text=text, tags=tags))
    return facts


def load_search_fact_aliases(path: Path = DEFAULT_SEARCH_FACT_ALIASES_PATH) -> dict[str, tuple[str, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}

    aliases: dict[str, tuple[str, ...]] = {}
    for alias, raw_tags in payload.items():
        alias_text = str(alias).strip()
        if not alias_text or alias_text in aliases or not isinstance(raw_tags, list):
            continue
        tags = tuple(dict.fromkeys(str(tag).strip().lower() for tag in raw_tags if str(tag).strip()))
        if tags:
            aliases[alias_text] = tags
    return aliases


def load_search_fact_catalog(
    runtime_path: Path | None = None,
) -> tuple[list[SearchFact], dict[str, tuple[str, ...]], dict[str, Any]]:
    if runtime_path is not None:
        payload = _load_json_payload(runtime_path)
        catalog = validate_search_fact_catalog(payload, min_facts=MIN_CATALOG_FACTS)
        if catalog is not None:
            return (
                _facts_from_payload(catalog["facts"]),
                _aliases_from_payload(catalog["aliases"]),
                catalog.get("markers", {}),
            )

    return (
        load_search_facts(),
        load_search_fact_aliases(),
        {"source": "bundled"},
    )


def validate_search_fact_catalog(payload: Any, *, min_facts: int = MIN_CATALOG_FACTS) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    facts = _normalise_fact_payload(payload.get("facts"))
    aliases = _normalise_alias_payload(payload.get("aliases"))
    if len(facts) < min_facts or not aliases:
        return None

    fact_tags = {tag for fact in facts for tag in fact["tags"]}
    alias_tags = {tag for tags in aliases.values() for tag in tags}
    if alias_tags - fact_tags:
        return None

    markers = payload.get("markers")
    if not isinstance(markers, dict):
        markers = {}

    return {
        "schema": 1,
        "generated_at": str(payload.get("generated_at") or _utc_now_iso()),
        "facts": facts,
        "aliases": aliases,
        "markers": {str(k): v for k, v in markers.items() if str(k).strip()},
    }


def build_search_fact_catalog_payload(
    facts: list[SearchFact],
    aliases: dict[str, tuple[str, ...]],
    *,
    markers: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "generated_at": generated_at or _utc_now_iso(),
        "facts": [
            {"id": fact.id, "text": fact.text, "tags": list(fact.tags)}
            for fact in facts
        ],
        "aliases": {alias: list(tags) for alias, tags in aliases.items()},
        "markers": markers or {},
    }


def format_search_fact_line(fact_text: str | None) -> str:
    if not fact_text:
        return ""
    return f"\n\nПока ждёте: {fact_text}"


def select_search_fact(
    facts: list[SearchFact],
    state: dict[str, Any] | None,
    chat_id: int,
    *,
    query: str | None = None,
    pool_size: int = DEFAULT_POOL_SIZE,
    refresh_threshold: float = DEFAULT_REFRESH_THRESHOLD,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    choice: Callable[[list[str]], str] | None = None,
    sample: Callable[[list[str], int], list[str]] | None = None,
    aliases: dict[str, tuple[str, ...]] | None = None,
    catalog_refresh_threshold: float = DEFAULT_CATALOG_REFRESH_THRESHOLD,
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
    query_tags = detect_query_tags(query or "", aliases=aliases)
    priority_ids = _priority_fact_ids(facts, query_tags)
    candidate_ids = priority_ids or all_ids
    target_pool_size = min(max(1, pool_size), len(all_ids))

    recent_ids = _normalise_id_list(chat_state.get("recent_shown_ids"), fact_by_id)
    pool_ids = _normalise_id_list(chat_state.get("pool_fact_ids"), fact_by_id)
    shown_ids = _normalise_id_list(chat_state.get("shown_in_pool"), fact_by_id)
    pool_query_tags = _normalise_tag_list(chat_state.get("pool_query_tags"))
    current_query_tags = sorted(query_tags)

    if (
        not pool_ids
        or pool_query_tags != current_query_tags
        or _pool_is_stale(pool_ids, shown_ids, refresh_threshold)
    ):
        pool_ids = _build_pool(candidate_ids, all_ids, recent_ids, target_pool_size, sample)
        shown_ids = []

    fact_id = _pick_fact_id(pool_ids, shown_ids, recent_ids, choice)
    if fact_id is None:
        pool_ids = _build_pool(candidate_ids, all_ids, recent_ids, target_pool_size, sample)
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
    chat_state["pool_query_tags"] = current_query_tags
    _record_catalog_progress(state, facts, fact_id, catalog_refresh_threshold)

    return fact_by_id[fact_id].text, state


def detect_query_tags(query: str, aliases: dict[str, tuple[str, ...]] | None = None) -> set[str]:
    normalized = _normalize_query(query)
    if not normalized:
        return set()
    aliases = aliases if aliases is not None else load_search_fact_aliases()
    tags: set[str] = set()
    for alias, alias_tags in aliases.items():
        if _alias_matches(normalized, _normalize_query(alias)):
            tags.update(alias_tags)
    if re.search(r"\bs\d{1,2}\b|\bseason\b|сезон", normalized):
        tags.add("series")
    return tags


def _build_pool(
    preferred_ids: list[str],
    all_ids: list[str],
    recent_ids: list[str],
    target_pool_size: int,
    sample: Callable[[list[str], int], list[str]],
) -> list[str]:
    recent = set(recent_ids)
    fresh_ids = [fact_id for fact_id in preferred_ids if fact_id not in recent]
    if fresh_ids:
        candidates = fresh_ids
    else:
        candidates = [fact_id for fact_id in all_ids if fact_id not in recent] or all_ids
    if len(candidates) <= target_pool_size:
        return list(candidates)
    return sample(list(candidates), target_pool_size)


def _priority_fact_ids(facts: list[SearchFact], query_tags: set[str]) -> list[str]:
    if not query_tags:
        return []
    return [fact.id for fact in facts if query_tags & set(fact.tags)]


def _normalize_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query).lower()
    normalized = normalized.replace("ё", "е")
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _alias_matches(query: str, alias: str) -> bool:
    if not alias:
        return False
    return re.search(rf"(^|\s){re.escape(alias)}(\s|$)", query) is not None


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


def _normalise_tag_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(tag).strip().lower() for tag in value if str(tag).strip()})


def _append_unique(items: list[str], item: str) -> list[str]:
    return [existing for existing in items if existing != item] + [item]


def _pool_id(pool_ids: list[str]) -> str:
    digest = sha1("\n".join(pool_ids).encode("utf-8")).hexdigest()[:12]
    return f"pool:{digest}"


def _record_catalog_progress(
    state: dict[str, Any],
    facts: list[SearchFact],
    fact_id: str,
    refresh_threshold: float,
) -> None:
    catalog_id = _catalog_id(facts)
    catalog = state.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("id") != catalog_id:
        catalog = {
            "id": catalog_id,
            "shown_unique_ids": [],
            "refresh_requested_at": "",
            "last_refresh_attempt_at": "",
            "last_refresh_success_at": "",
            "last_refresh_error": "",
        }
        state["catalog"] = catalog

    shown_ids = _normalise_id_list(catalog.get("shown_unique_ids"), {fact.id: fact for fact in facts})
    shown_ids = _append_unique(shown_ids, fact_id)
    total = len(facts)
    shown_percent = (len(shown_ids) / total) if total else 0.0
    catalog["shown_unique_ids"] = shown_ids
    catalog["total_facts"] = total
    catalog["shown_percent"] = round(shown_percent, 4)
    if shown_percent >= max(0.0, min(1.0, refresh_threshold)) and not catalog.get("refresh_requested_at"):
        catalog["refresh_requested_at"] = _utc_now_iso()


def _catalog_id(facts: list[SearchFact]) -> str:
    digest = sha1(
        "\n".join(f"{fact.id}\t{fact.text}" for fact in facts).encode("utf-8")
    ).hexdigest()[:12]
    return f"catalog:{digest}"


def _load_json_payload(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _facts_from_payload(payload: list[dict[str, Any]]) -> list[SearchFact]:
    return [
        SearchFact(id=str(item["id"]), text=str(item["text"]), tags=tuple(item["tags"]))
        for item in payload
    ]


def _aliases_from_payload(payload: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    return {str(alias): tuple(tags) for alias, tags in payload.items()}


def _normalise_fact_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    facts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        fact_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not fact_id or not text or fact_id in seen_ids or text.casefold() in seen_texts:
            continue
        tags = tuple(dict.fromkeys(
            str(tag).strip().lower()
            for tag in item.get("tags", [])
            if str(tag).strip()
        ))
        if not tags or len(text) > 180:
            continue
        facts.append({"id": fact_id, "text": text, "tags": list(tags)})
        seen_ids.add(fact_id)
        seen_texts.add(text.casefold())
    return facts


def _normalise_alias_payload(payload: Any) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        return {}
    aliases: dict[str, list[str]] = {}
    for alias, raw_tags in payload.items():
        alias_text = str(alias).strip()
        if not alias_text or not isinstance(raw_tags, list):
            continue
        tags = list(dict.fromkeys(
            str(tag).strip().lower()
            for tag in raw_tags
            if str(tag).strip()
        ))
        if tags:
            aliases[alias_text] = tags
    return aliases


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
