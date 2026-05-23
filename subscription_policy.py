"""Subscription policy helpers — the source of truth for what `notify_policy`
and `download_policy` mean and how they interact with subscription state.

Background: subscriptions historically had a single `notify_mode` field with
two values:
  - `per_episode`     — push on every new episode, auto-download every episode
  - `season_complete` — silent download every episode, push only when season is full

That conflated two orthogonal axes:
  1. WHEN to push the user (each update / final only / never)
  2. WHEN to trigger the download (each update / only after season closes / never)

1.3 splits these into two independent fields so we can offer combinations the
old single field couldn't express (most importantly: «wait for season to be
fully released, then download as a single torrent»).

This module owns the migration map AND the runtime predicates so all three
subscription loops (`_check_subscriptions`, `_check_jackett_subscriptions`,
`_check_jackett_sub_via_rutracker_direct`) make consistent decisions.
"""
from __future__ import annotations

# notify_policy values
NOTIFY_EACH_UPDATE = "each_update"
NOTIFY_FINAL_ONLY = "final_only"
NOTIFY_SILENT = "silent"
VALID_NOTIFY_POLICIES = frozenset({NOTIFY_EACH_UPDATE, NOTIFY_FINAL_ONLY, NOTIFY_SILENT})

# download_policy values
DOWNLOAD_AUTO_EACH_UPDATE = "auto_each_update"
DOWNLOAD_ONLY_WHEN_COMPLETE = "only_when_complete"  # NEW in 1.3 — wait for season to close
DOWNLOAD_NOTIFY_ONLY = "notify_only"
DOWNLOAD_ASK = "ask"
VALID_DOWNLOAD_POLICIES = frozenset({
    DOWNLOAD_AUTO_EACH_UPDATE,
    DOWNLOAD_ONLY_WHEN_COMPLETE,
    DOWNLOAD_NOTIFY_ONLY,
    DOWNLOAD_ASK,
})


# Map from legacy single-field values to (notify_policy, download_policy) pairs.
# Selected so existing user behaviour is preserved exactly.
_LEGACY_NOTIFY_MODE_MAP = {
    "per_episode":     (NOTIFY_EACH_UPDATE, DOWNLOAD_AUTO_EACH_UPDATE),
    "season_complete": (NOTIFY_FINAL_ONLY,  DOWNLOAD_AUTO_EACH_UPDATE),
}


def migrate_subscription_in_place(sub: dict) -> bool:
    """Inject notify_policy/download_policy if missing, derived from legacy
    notify_mode. Returns True if anything changed (caller may want to persist).

    Idempotent: running twice is a no-op. Safe to call on every load.
    Tolerates malformed input — unknown values fall back to the safest pair
    (each_update + auto_each_update = «old default» behaviour).
    """
    if not isinstance(sub, dict):
        return False

    changed = False
    has_notify = "notify_policy" in sub and sub["notify_policy"] in VALID_NOTIFY_POLICIES
    has_download = "download_policy" in sub and sub["download_policy"] in VALID_DOWNLOAD_POLICIES

    if has_notify and has_download:
        return False  # already migrated, nothing to do

    legacy = str(sub.get("notify_mode") or "per_episode").lower()
    pair = _LEGACY_NOTIFY_MODE_MAP.get(legacy)
    if pair is None:
        # Unknown legacy value — fall back to safest pair preserving old default
        pair = (NOTIFY_EACH_UPDATE, DOWNLOAD_AUTO_EACH_UPDATE)

    if not has_notify:
        sub["notify_policy"] = pair[0]
        changed = True
    if not has_download:
        sub["download_policy"] = pair[1]
        changed = True
    return changed


def migrate_subscriptions_in_place(subs: dict) -> int:
    """Run ``migrate_subscription_in_place`` over every value of ``subs``.

    Returns the count of dicts that actually changed — caller can decide
    whether to persist (typically: if N > 0).
    """
    if not isinstance(subs, dict):
        return 0
    n_changed = 0
    for sub in subs.values():
        if migrate_subscription_in_place(sub):
            n_changed += 1
    return n_changed


def _resolved_policies(sub: dict) -> tuple[str, str]:
    """Read (notify_policy, download_policy) from a sub, lazily migrating from
    legacy notify_mode if needed. Defensive against subs that came from old
    JSON, tests, or paths that bypass state_store.load_topic_subscriptions.
    """
    notify = sub.get("notify_policy")
    download = sub.get("download_policy")
    if notify in VALID_NOTIFY_POLICIES and download in VALID_DOWNLOAD_POLICIES:
        return (str(notify), str(download))
    # Fall back to deriving from legacy notify_mode — does NOT mutate the
    # caller's dict (helpers stay read-only). state_store loader is the
    # authoritative path that persists the migrated form.
    legacy = str(sub.get("notify_mode") or "per_episode").lower()
    pair = _LEGACY_NOTIFY_MODE_MAP.get(
        legacy, (NOTIFY_EACH_UPDATE, DOWNLOAD_AUTO_EACH_UPDATE)
    )
    return (
        str(notify) if notify in VALID_NOTIFY_POLICIES else pair[0],
        str(download) if download in VALID_DOWNLOAD_POLICIES else pair[1],
    )


def should_notify(sub: dict, *, is_complete: bool) -> bool:
    """Should we send a push to the user about this update?

    ``is_complete`` reflects whether the new state advances the subscription
    to ``new_end >= total_episodes`` (i.e. season just closed). The check
    loop computes this once and passes it in.
    """
    policy, _ = _resolved_policies(sub)
    if policy == NOTIFY_SILENT:
        return False
    if policy == NOTIFY_FINAL_ONLY:
        return is_complete
    # NOTIFY_EACH_UPDATE (and any unrecognised value — safe default)
    return True


def should_download(sub: dict, *, is_complete: bool) -> bool:
    """Should we trigger an auto-download for this update?

    ``is_complete`` is the same trigger condition used by ``should_notify``.
    """
    _, policy = _resolved_policies(sub)
    if policy == DOWNLOAD_NOTIFY_ONLY:
        return False
    if policy == DOWNLOAD_ASK:
        # Caller is expected to render a button instead of auto-downloading.
        # For the existing background loops this means «don't auto», so False.
        return False
    if policy == DOWNLOAD_ONLY_WHEN_COMPLETE:
        return is_complete
    # DOWNLOAD_AUTO_EACH_UPDATE (and any unrecognised value — safe default)
    return True


def policies_summary_ru(sub: dict) -> str:
    """Compact human-readable description of a subscription's policy pair.
    For use in admin diagnostics and subscription-list UI."""
    n = sub.get("notify_policy") or NOTIFY_EACH_UPDATE
    d = sub.get("download_policy") or DOWNLOAD_AUTO_EACH_UPDATE
    n_label = {
        NOTIFY_EACH_UPDATE: "📺 о каждой",
        NOTIFY_FINAL_ONLY:  "🎯 при финале",
        NOTIFY_SILENT:      "🔇 молча",
    }.get(n, n)
    d_label = {
        DOWNLOAD_AUTO_EACH_UPDATE:   "⬇️ каждую",
        DOWNLOAD_ONLY_WHEN_COMPLETE: "📦 после финала",
        DOWNLOAD_NOTIFY_ONLY:        "⏸ без загрузки",
        DOWNLOAD_ASK:                "❓ спрашивать",
    }.get(d, d)
    return f"{n_label} · {d_label}"
