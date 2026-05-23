"""Tests for subscription_policy — the helpers powering 1.3 (notify_policy +
download_policy split). Covers migration, runtime predicates, and the new
download_policy=only_when_complete mode that didn't exist before 1.3.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

from subscription_policy import (
    DOWNLOAD_ASK,
    DOWNLOAD_AUTO_EACH_UPDATE,
    DOWNLOAD_NOTIFY_ONLY,
    DOWNLOAD_ONLY_WHEN_COMPLETE,
    NOTIFY_EACH_UPDATE,
    NOTIFY_FINAL_ONLY,
    NOTIFY_SILENT,
    migrate_subscription_in_place,
    migrate_subscriptions_in_place,
    policies_summary_ru,
    should_download,
    should_notify,
)


class MigrationTests(unittest.TestCase):
    """legacy notify_mode → (notify_policy, download_policy) translation."""

    def test_per_episode_maps_to_each_update_auto(self):
        sub = {"notify_mode": "per_episode"}
        changed = migrate_subscription_in_place(sub)
        self.assertTrue(changed)
        self.assertEqual(sub["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def test_season_complete_maps_to_final_only_auto(self):
        sub = {"notify_mode": "season_complete"}
        migrate_subscription_in_place(sub)
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def test_missing_notify_mode_falls_back_to_safest_default(self):
        sub = {}
        migrate_subscription_in_place(sub)
        self.assertEqual(sub["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def test_unknown_legacy_value_falls_back_to_safest_default(self):
        sub = {"notify_mode": "weird_unknown_value"}
        migrate_subscription_in_place(sub)
        self.assertEqual(sub["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def test_migration_is_idempotent(self):
        sub = {"notify_mode": "per_episode"}
        changed_first = migrate_subscription_in_place(sub)
        snapshot = dict(sub)
        changed_again = migrate_subscription_in_place(sub)
        self.assertTrue(changed_first)
        self.assertFalse(changed_again)
        self.assertEqual(sub, snapshot)

    def test_explicit_policy_fields_are_not_overwritten(self):
        """Subscriptions created by 1.3+ code already have policy fields —
        the migrator must not clobber explicit values with legacy-derived ones."""
        sub = {
            "notify_mode": "per_episode",
            "notify_policy": NOTIFY_FINAL_ONLY,
            "download_policy": DOWNLOAD_ONLY_WHEN_COMPLETE,
        }
        migrate_subscription_in_place(sub)
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)

    def test_invalid_policy_value_is_overwritten_with_derived(self):
        """If someone hand-edited JSON with bogus values, the migrator
        repairs them using legacy notify_mode as the source of truth."""
        sub = {
            "notify_mode": "season_complete",
            "notify_policy": "garbage",
            "download_policy": None,
        }
        migrate_subscription_in_place(sub)
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)

    def test_migrate_subscriptions_in_place_counts_changes(self):
        subs = {
            "a": {"notify_mode": "per_episode"},
            "b": {"notify_mode": "season_complete"},
            # already migrated — no change
            "c": {
                "notify_policy": NOTIFY_SILENT,
                "download_policy": DOWNLOAD_NOTIFY_ONLY,
            },
        }
        n = migrate_subscriptions_in_place(subs)
        self.assertEqual(n, 2)
        # Already-migrated entry untouched.
        self.assertEqual(subs["c"]["notify_policy"], NOTIFY_SILENT)


class ShouldNotifyTests(unittest.TestCase):
    """Decision: send a Telegram push for this update?"""

    def test_each_update_always_notifies(self):
        sub = {"notify_policy": NOTIFY_EACH_UPDATE, "download_policy": DOWNLOAD_AUTO_EACH_UPDATE}
        self.assertTrue(should_notify(sub, is_complete=False))
        self.assertTrue(should_notify(sub, is_complete=True))

    def test_final_only_notifies_only_on_complete(self):
        sub = {"notify_policy": NOTIFY_FINAL_ONLY, "download_policy": DOWNLOAD_AUTO_EACH_UPDATE}
        self.assertFalse(should_notify(sub, is_complete=False))
        self.assertTrue(should_notify(sub, is_complete=True))

    def test_silent_never_notifies(self):
        sub = {"notify_policy": NOTIFY_SILENT, "download_policy": DOWNLOAD_AUTO_EACH_UPDATE}
        self.assertFalse(should_notify(sub, is_complete=False))
        self.assertFalse(should_notify(sub, is_complete=True))

    def test_legacy_per_episode_resolves_to_each_update(self):
        """Helpers must work on subs that came from old JSON without policy
        fields populated yet (defensive — state_store migrates on load but
        tests / external callers may bypass it)."""
        sub = {"notify_mode": "per_episode"}
        self.assertTrue(should_notify(sub, is_complete=False))

    def test_legacy_season_complete_resolves_to_final_only(self):
        sub = {"notify_mode": "season_complete"}
        self.assertFalse(should_notify(sub, is_complete=False))
        self.assertTrue(should_notify(sub, is_complete=True))


class ShouldDownloadTests(unittest.TestCase):
    """Decision: trigger an auto-download for this update?"""

    def test_auto_each_update_always_downloads(self):
        sub = {"notify_policy": NOTIFY_EACH_UPDATE, "download_policy": DOWNLOAD_AUTO_EACH_UPDATE}
        self.assertTrue(should_download(sub, is_complete=False))
        self.assertTrue(should_download(sub, is_complete=True))

    def test_only_when_complete_waits(self):
        """The new 1.3 mode — skip intermediate episodes, trigger when season closes."""
        sub = {
            "notify_policy": NOTIFY_FINAL_ONLY,
            "download_policy": DOWNLOAD_ONLY_WHEN_COMPLETE,
        }
        self.assertFalse(should_download(sub, is_complete=False))
        self.assertTrue(should_download(sub, is_complete=True))

    def test_notify_only_never_downloads(self):
        sub = {"notify_policy": NOTIFY_EACH_UPDATE, "download_policy": DOWNLOAD_NOTIFY_ONLY}
        self.assertFalse(should_download(sub, is_complete=False))
        self.assertFalse(should_download(sub, is_complete=True))

    def test_ask_treated_as_no_auto_in_background_loops(self):
        """download_policy=ask means «show user a button»; the background
        loops can't show buttons, so this resolves to «don't auto-download»."""
        sub = {"download_policy": DOWNLOAD_ASK}
        self.assertFalse(should_download(sub, is_complete=False))
        self.assertFalse(should_download(sub, is_complete=True))


class PoliciesSummaryTests(unittest.TestCase):
    def test_renders_known_pair(self):
        sub = {
            "notify_policy": NOTIFY_FINAL_ONLY,
            "download_policy": DOWNLOAD_ONLY_WHEN_COMPLETE,
        }
        s = policies_summary_ru(sub)
        self.assertIn("финал", s.lower())
        self.assertIn("после финала", s.lower())

    def test_handles_missing_fields_with_defaults(self):
        s = policies_summary_ru({})
        # Defaults are each_update + auto_each_update
        self.assertIn("каждой", s)
        self.assertIn("каждую", s)

    def test_respects_legacy_notify_mode(self):
        s = policies_summary_ru({"notify_mode": "season_complete"})
        self.assertIn("финал", s.lower())


class JackettSubscriptionBuilderTests(unittest.TestCase):
    """build_jackett_subscription should accept the new policy fields and
    always emit migrated subscriptions regardless of which input style was
    used."""

    def _result(self) -> dict:
        return {
            "title": "Show S1E1-3 of 10",
            "url": "https://rutracker.org/forum/viewtopic.php?t=77",
            "tracker_name": "rutracker",
        }

    def test_explicit_policy_fields_carry_through(self):
        from jackett_subscriptions import build_jackett_subscription
        sub = build_jackett_subscription(
            chat_id=100, query="Show", result=self._result(),
            seen_results=[], added_at="2026-05-23 10:00",
            notify_policy=NOTIFY_FINAL_ONLY,
            download_policy=DOWNLOAD_ONLY_WHEN_COMPLETE,
        )
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_ONLY_WHEN_COMPLETE)

    def test_legacy_notify_mode_only_still_migrates(self):
        from jackett_subscriptions import build_jackett_subscription
        sub = build_jackett_subscription(
            chat_id=100, query="Show", result=self._result(),
            seen_results=[], added_at="2026-05-23 10:00",
            notify_mode="season_complete",  # legacy input
        )
        # Migrated fields present in addition to legacy notify_mode.
        self.assertEqual(sub["notify_policy"], NOTIFY_FINAL_ONLY)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)
        self.assertEqual(sub["notify_mode"], "season_complete")


if __name__ == "__main__":
    unittest.main()
