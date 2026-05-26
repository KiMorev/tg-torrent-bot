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
    policies_summary_ru,
    should_download,
    should_notify,
)


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

    def test_missing_policy_defaults_to_each_update(self):
        sub = {}
        self.assertTrue(should_notify(sub, is_complete=False))

    def test_invalid_policy_defaults_to_each_update(self):
        sub = {"notify_policy": "garbage"}
        self.assertTrue(should_notify(sub, is_complete=False))


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
        self.assertIn("сезон завершится", s.lower())
        self.assertIn("когда сезон завершится", s.lower())

    def test_handles_missing_fields_with_defaults(self):
        s = policies_summary_ru({})
        # Defaults are each_update + auto_each_update
        self.assertIn("о каждой новой серии", s)
        self.assertIn("новые серии по мере выхода", s)


class JackettSubscriptionBuilderTests(unittest.TestCase):
    """build_jackett_subscription should accept the new policy fields and
    always emit explicit policy subscriptions."""

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

    def test_default_policy_fields_are_written(self):
        from jackett_subscriptions import build_jackett_subscription
        sub = build_jackett_subscription(
            chat_id=100, query="Show", result=self._result(),
            seen_results=[], added_at="2026-05-23 10:00",
        )
        self.assertEqual(sub["notify_policy"], NOTIFY_EACH_UPDATE)
        self.assertEqual(sub["download_policy"], DOWNLOAD_AUTO_EACH_UPDATE)
        self.assertNotIn("notify_mode", sub)


if __name__ == "__main__":
    unittest.main()
