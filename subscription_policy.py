"""Subscription policy helpers — the source of truth for what `notify_policy`
and `download_policy` mean and how they interact with subscription state.

Subscriptions use two independent fields:
  1. WHEN to push the user (each update / final only / never)
  2. WHEN to trigger the download (each update / only after season closes / never)

This module owns the runtime predicates so all three subscription loops
(`_check_subscriptions`, `_check_jackett_subscriptions`,
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

_NOTIFY_POLICY_LABELS_RU = {
    NOTIFY_EACH_UPDATE: "о каждой новой серии",
    NOTIFY_FINAL_ONLY: "только когда сезон завершится",
    NOTIFY_SILENT: "не уведомлять",
}
_NOTIFY_POLICY_ICONS_RU = {
    NOTIFY_EACH_UPDATE: "🔔",
    NOTIFY_FINAL_ONLY: "🎯",
    NOTIFY_SILENT: "🔇",
}
_DOWNLOAD_POLICY_LABELS_RU = {
    DOWNLOAD_AUTO_EACH_UPDATE: "новые серии по мере выхода",
    DOWNLOAD_ONLY_WHEN_COMPLETE: "когда сезон завершится",
    DOWNLOAD_NOTIFY_ONLY: "не скачивать автоматически",
    DOWNLOAD_ASK: "спрашивать перед скачиванием",
}
_DOWNLOAD_POLICY_ICONS_RU = {
    DOWNLOAD_AUTO_EACH_UPDATE: "⬇️",
    DOWNLOAD_ONLY_WHEN_COMPLETE: "📦",
    DOWNLOAD_NOTIFY_ONLY: "⏸",
    DOWNLOAD_ASK: "❓",
}


def _resolved_policies(sub: dict) -> tuple[str, str]:
    """Read (notify_policy, download_policy), defaulting invalid/missing values."""
    notify = sub.get("notify_policy")
    download = sub.get("download_policy")
    return (
        str(notify) if notify in VALID_NOTIFY_POLICIES else NOTIFY_EACH_UPDATE,
        str(download) if download in VALID_DOWNLOAD_POLICIES else DOWNLOAD_AUTO_EACH_UPDATE,
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


def notify_policy_label_ru(policy: str | None, *, icon: bool = False) -> str:
    resolved, _ = _resolved_policies({"notify_policy": policy})
    label = _NOTIFY_POLICY_LABELS_RU.get(resolved, resolved)
    if icon:
        return f"{_NOTIFY_POLICY_ICONS_RU.get(resolved, '')} {label}".strip()
    return label


def download_policy_label_ru(policy: str | None, *, icon: bool = False) -> str:
    _, resolved = _resolved_policies({"download_policy": policy})
    label = _DOWNLOAD_POLICY_LABELS_RU.get(resolved, resolved)
    if icon:
        return f"{_DOWNLOAD_POLICY_ICONS_RU.get(resolved, '')} {label}".strip()
    return label


def policies_summary_ru(sub: dict) -> str:
    """Compact human-readable description of a subscription's policy pair.
    For use in admin diagnostics and subscription-list UI."""
    n, d = _resolved_policies(sub)
    n_label = notify_policy_label_ru(n, icon=True)
    d_label = download_policy_label_ru(d, icon=True)
    return f"{n_label} · {d_label}"
