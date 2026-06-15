"""Persistent per-indexer readiness state for Jackett.

The guard does not restart Jackett or change indexer configuration.  It keeps
memory about recent per-indexer failures so callers can treat a recently broken
indexer as unready until it proves recovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


STATE_OK = "ok"
STATE_WARMING = "warming"
STATE_DEGRADED = "degraded"
STATE_QUARANTINED = "quarantined"
STATE_MANUAL_REQUIRED = "manual_required"

UNREADY_STATES = {STATE_WARMING, STATE_DEGRADED, STATE_QUARANTINED, STATE_MANUAL_REQUIRED}

_TRANSIENT_BACKOFF_SECONDS = (60, 180, 600, 1800, 3600)
_MANUAL_RECHECK_SECONDS = 12 * 3600
_QUARANTINE_AFTER_FAILURES = 3
_RECENT_RECOVERIES_LIMIT = 20

_MANUAL_MARKERS = (
    "cloudflare",
    "ddos-guard",
    "flaresolverr",
    "cookie",
    "captcha",
    "login",
    "signin",
    "auth",
    "unauthorized",
    "forbidden",
    "invalid api",
    "api key",
    "api-key",
)


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def ts_text(ts: float | int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")


def normalize_indexer_id(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    indexers = payload.get("indexers")
    if not isinstance(indexers, dict):
        indexers = {}
    clean_indexers: dict[str, dict] = {}
    for raw_id, raw_entry in indexers.items():
        indexer_id = normalize_indexer_id(raw_id)
        if not indexer_id or not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        entry["id"] = indexer_id
        entry["state"] = str(entry.get("state") or STATE_OK)
        entry["name"] = str(entry.get("name") or indexer_id)
        entry["fail_streak"] = max(0, _to_int(entry.get("fail_streak")))
        entry["zero_streak"] = max(0, _to_int(entry.get("zero_streak")))
        entry["next_retry_ts"] = max(0.0, _to_float(entry.get("next_retry_ts")))
        clean_indexers[indexer_id] = entry
    recent = payload.get("recent_recoveries")
    if not isinstance(recent, list):
        recent = []
    return {
        "version": 1,
        "updated_at": str(payload.get("updated_at") or ""),
        "indexers": clean_indexers,
        "recent_recoveries": [r for r in recent if isinstance(r, dict)][-_RECENT_RECOVERIES_LIMIT:],
    }


def classify_error(error_kind: object = "", error: object = "") -> str:
    kind = str(error_kind or "").strip().lower()
    text = str(error or "").strip().lower()
    if kind in {"auth", "manual_required"}:
        return STATE_MANUAL_REQUIRED
    if any(marker in text for marker in _MANUAL_MARKERS):
        return STATE_MANUAL_REQUIRED
    return STATE_DEGRADED


def record_success(
    payload: dict | None,
    indexer_id: object,
    *,
    name: object = "",
    source: str = "",
    query: str = "",
    results: int | None = None,
    elapsed_seconds: float | None = None,
    now: float | None = None,
) -> tuple[dict, dict | None]:
    state = normalize_payload(payload)
    indexer_id = normalize_indexer_id(indexer_id)
    if not indexer_id:
        return state, None
    ts = now_ts() if now is None else float(now)
    indexers = state["indexers"]
    previous = indexers.get(indexer_id, {})
    previous_state = str(previous.get("state") or STATE_OK)
    was_unready = previous_state in UNREADY_STATES
    entry = {
        **previous,
        "id": indexer_id,
        "name": str(name or previous.get("name") or indexer_id),
        "state": STATE_OK,
        "last_source": source,
        "last_query": query,
        "last_checked_at": ts_text(ts),
        "last_ok_at": ts_text(ts),
        "last_results": max(0, int(results)) if results is not None else previous.get("last_results", 0),
        "fail_streak": 0,
        "zero_streak": 0,
        "last_error_kind": "",
        "last_error_short": "",
        "next_retry_ts": 0.0,
        "next_retry_at": "",
    }
    if elapsed_seconds is not None:
        entry["last_elapsed_seconds"] = round(float(elapsed_seconds), 3)
    indexers[indexer_id] = entry
    state["updated_at"] = ts_text(ts)

    event = None
    if was_unready:
        event = {"kind": "recovered", "indexer_id": indexer_id, "name": entry["name"], "at": ts_text(ts)}
        recent = list(state.get("recent_recoveries") or [])
        recent.append(event)
        state["recent_recoveries"] = recent[-_RECENT_RECOVERIES_LIMIT:]
    return state, event


def record_failure(
    payload: dict | None,
    indexer_id: object,
    *,
    name: object = "",
    error_kind: object = "",
    error: object = "",
    source: str = "",
    query: str = "",
    now: float | None = None,
) -> tuple[dict, dict | None]:
    state = normalize_payload(payload)
    indexer_id = normalize_indexer_id(indexer_id)
    if not indexer_id:
        return state, None
    ts = now_ts() if now is None else float(now)
    indexers = state["indexers"]
    previous = indexers.get(indexer_id, {})
    fail_streak = max(0, _to_int(previous.get("fail_streak"))) + 1
    classified = classify_error(error_kind, error)
    if classified == STATE_MANUAL_REQUIRED:
        new_state = STATE_MANUAL_REQUIRED
        retry_after = _MANUAL_RECHECK_SECONDS
    else:
        kind = str(error_kind or "").lower()
        if kind in {"startup", "loading"} and fail_streak < _QUARANTINE_AFTER_FAILURES:
            new_state = STATE_WARMING
        elif fail_streak >= _QUARANTINE_AFTER_FAILURES:
            new_state = STATE_QUARANTINED
        else:
            new_state = STATE_DEGRADED
        retry_after = _retry_after_seconds(fail_streak)

    next_retry_ts = ts + retry_after
    entry = {
        **previous,
        "id": indexer_id,
        "name": str(name or previous.get("name") or indexer_id),
        "state": new_state,
        "last_source": source,
        "last_query": query,
        "last_checked_at": ts_text(ts),
        "last_error_at": ts_text(ts),
        "last_error_kind": str(error_kind or ""),
        "last_error_short": _short_error(error),
        "fail_streak": fail_streak,
        "next_retry_ts": next_retry_ts,
        "next_retry_at": ts_text(next_retry_ts),
    }
    indexers[indexer_id] = entry
    state["updated_at"] = ts_text(ts)
    event = {
        "kind": new_state,
        "indexer_id": indexer_id,
        "name": entry["name"],
        "at": ts_text(ts),
        "fail_streak": fail_streak,
    }
    return state, event


def record_statuses(
    payload: dict | None,
    statuses: Iterable[Any],
    *,
    source: str,
    query: str,
    now: float | None = None,
) -> tuple[dict, list[dict]]:
    state = normalize_payload(payload)
    events: list[dict] = []
    ts = now_ts() if now is None else float(now)
    for status in statuses:
        indexer_id = normalize_indexer_id(getattr(status, "indexer_id", ""))
        if not indexer_id:
            continue
        name = getattr(status, "name", "") or indexer_id
        results = max(0, _to_int(getattr(status, "results", 0)))
        if bool(getattr(status, "is_ok", False)):
            state, event = record_success(
                state,
                indexer_id,
                name=name,
                source=source,
                query=query,
                results=results,
                now=ts,
            )
        else:
            state, event = record_failure(
                state,
                indexer_id,
                name=name,
                error_kind=f"status_{getattr(status, 'status', '')}",
                error=getattr(status, "error", ""),
                source=source,
                query=query,
                now=ts,
            )
        if event:
            events.append(event)
    return state, events


def record_batch_failure(
    payload: dict | None,
    indexer_ids: Iterable[object],
    *,
    error_kind: object = "",
    error: object = "",
    source: str,
    query: str,
    now: float | None = None,
) -> tuple[dict, list[dict]]:
    state = normalize_payload(payload)
    events: list[dict] = []
    ts = now_ts() if now is None else float(now)
    for indexer_id in indexer_ids:
        state, event = record_failure(
            state,
            indexer_id,
            error_kind=error_kind,
            error=error,
            source=source,
            query=query,
            now=ts,
        )
        if event:
            events.append(event)
    return state, events


def unready_indexer_ids(payload: dict | None) -> set[str]:
    state = normalize_payload(payload)
    return {
        indexer_id
        for indexer_id, entry in state["indexers"].items()
        if str(entry.get("state") or "") in UNREADY_STATES
    }


def unready_summary(payload: dict | None, enabled_ids: set[str] | None) -> dict[str, list[str]]:
    state = normalize_payload(payload)
    enabled: list[str] = []
    disabled: list[str] = []
    manual: list[str] = []
    for indexer_id, entry in sorted(state["indexers"].items()):
        entry_state = str(entry.get("state") or "")
        if entry_state not in UNREADY_STATES:
            continue
        if entry_state == STATE_MANUAL_REQUIRED:
            manual.append(indexer_id)
        if enabled_ids is None or indexer_id in enabled_ids:
            enabled.append(indexer_id)
        else:
            disabled.append(indexer_id)
    return {
        "enabled": enabled,
        "disabled": disabled,
        "manual_required": manual,
    }


def due_indexer_ids(
    payload: dict | None,
    *,
    pool: Iterable[object] | None = None,
    limit: int | None = None,
    now: float | None = None,
) -> list[str]:
    state = normalize_payload(payload)
    ts = now_ts() if now is None else float(now)
    allowed = None if pool is None else {normalize_indexer_id(item) for item in pool}
    due: list[tuple[float, str]] = []
    for indexer_id, entry in state["indexers"].items():
        if allowed is not None and indexer_id not in allowed:
            continue
        if str(entry.get("state") or "") not in UNREADY_STATES:
            continue
        next_retry_ts = _to_float(entry.get("next_retry_ts"))
        if not next_retry_ts or next_retry_ts <= ts:
            due.append((next_retry_ts, indexer_id))
    due_ids = [indexer_id for _, indexer_id in sorted(due)]
    if limit is not None:
        return due_ids[:max(0, int(limit))]
    return due_ids


def next_due_delay(payload: dict | None, *, default: float, now: float | None = None) -> float:
    state = normalize_payload(payload)
    ts = now_ts() if now is None else float(now)
    next_values = [
        _to_float(entry.get("next_retry_ts"))
        for entry in state["indexers"].values()
        if str(entry.get("state") or "") in UNREADY_STATES and _to_float(entry.get("next_retry_ts")) > 0
    ]
    if not next_values:
        return float(default)
    return max(0.0, min(float(default), min(next_values) - ts))


def _retry_after_seconds(fail_streak: int) -> int:
    if fail_streak <= 0:
        return _TRANSIENT_BACKOFF_SECONDS[0]
    index = min(fail_streak - 1, len(_TRANSIENT_BACKOFF_SECONDS) - 1)
    return _TRANSIENT_BACKOFF_SECONDS[index]


def _short_error(value: object) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > 180:
        return text[:177] + "..."
    return text


def _to_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
