import unittest
from types import SimpleNamespace

from jackett_subscriptions import (
    build_jackett_subscription,
    select_jackett_subscription_candidate,
)


def _result(
    title: str,
    *,
    tracker: str = "rutracker",
    topic_url: str = "https://rutracker.org/forum/viewtopic.php?t=123",
    seeders: int = 10,
):
    return SimpleNamespace(
        title=title,
        size="1.0 GB",
        seeders=seeders,
        tracker=tracker,
        topic_url=topic_url,
        magnet_url=None,
        torrent_url="http://jackett.local/download/123",
    )


class JackettSubscriptionTests(unittest.TestCase):
    def test_build_subscription_stores_selected_result_anchor(self) -> None:
        title = "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]"
        sub = build_jackett_subscription(
            chat_id=100,
            query="Клиника Сезон: 1 1080p",
            result={
                "title": title,
                "tracker_name": "rutracker",
                "url": "https://rutracker.org/forum/viewtopic.php?t=123",
            },
            seen_results=[{"title": title}, {"title": "other"}],
            added_at="2026-05-12 10:00",
        )

        self.assertEqual(sub["version"], 2)
        self.assertEqual(sub["tracker"], "rutracker")
        self.assertEqual(sub["topic_url"], "https://rutracker.org/forum/viewtopic.php?t=123")
        self.assertEqual(sub["season"], 1)
        self.assertEqual(sub["last_episode_end"], 8)
        self.assertEqual(sub["total_episodes"], 10)
        self.assertEqual(sub["seen_titles"], [title, "other"])

    def test_select_candidate_requires_tracker_season_and_episode_progress(self) -> None:
        sub = {
            "type": "jackett",
            "version": 2,
            "tracker": "rutracker",
            "topic_url": "https://rutracker.org/forum/viewtopic.php?t=123",
            "title": "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]",
            "season": 1,
            "last_episode_end": 8,
        }

        wrong_tracker = _result(
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]",
            tracker="kinozal",
        )
        wrong_season = _result(
            "Клиника / Scrubs / Сезон: 2 / Серии: 1-9 из 10 [WEB-DL]",
        )
        same_episode = _result(
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]",
        )
        expected = _result(
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]",
        )

        selected = select_jackett_subscription_candidate(
            sub,
            [wrong_tracker, wrong_season, same_episode, expected],
        )

        self.assertIs(selected, expected)

    def test_select_candidate_rejects_unrelated_result_without_topic_match(self) -> None:
        sub = {
            "type": "jackett",
            "version": 2,
            "tracker": "rutracker",
            "topic_url": "https://rutracker.org/forum/viewtopic.php?t=123",
            "title": "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]",
            "season": 1,
            "last_episode_end": 8,
        }

        result = _result(
            "Доктор Хаус / House M.D. / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]",
            topic_url="https://rutracker.org/forum/viewtopic.php?t=999",
        )

        self.assertIsNone(select_jackett_subscription_candidate(sub, [result]))

    def test_legacy_subscription_keeps_seen_titles_fallback(self) -> None:
        sub = {"type": "jackett", "query": "series", "seen_titles": ["old"]}
        expected = _result("new")

        self.assertIs(select_jackett_subscription_candidate(sub, [_result("old"), expected]), expected)


if __name__ == "__main__":
    unittest.main()
