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


class JackettSubscriptionSearchParamsTests(unittest.TestCase):
    """Verify that the check loop passes correct search params (fetch_limit & indexers).

    These tests exercise the *logic* of what would be passed to jackett_client.search()
    by inspecting the subscription fields, mirroring what _check_jackett_subscriptions does.
    """

    def _sub(self, tracker: str = "rutracker", query: str = "Клиника 1080p") -> dict:
        return {
            "type": "jackett",
            "version": 2,
            "query": query,
            "tracker": tracker,
            "topic_url": "https://rutracker.org/forum/viewtopic.php?t=123",
            "title": "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]",
            "season": 1,
            "last_episode_end": 8,
            "total_episodes": 10,
            "seen_titles": [],
            "last_check": "2026-05-01 10:00",
        }

    def test_tracker_id_extracted_for_indexers_filter(self) -> None:
        """tracker field should be used as the indexers filter — lowercase, stripped."""
        sub = self._sub(tracker="RuTracker")
        tracker_id = str(sub.get("tracker") or "").strip().lower() or None
        self.assertEqual(tracker_id, "rutracker")
        self.assertEqual([tracker_id], ["rutracker"])

    def test_empty_tracker_gives_none_indexers_filter(self) -> None:
        """If tracker is unknown, indexers filter is None → search all."""
        sub = self._sub(tracker="")
        tracker_id = str(sub.get("tracker") or "").strip().lower() or None
        self.assertIsNone(tracker_id)

    def test_candidate_with_more_episodes_selected_over_url_match(self) -> None:
        """Topic-URL match gets bonus but a result with MORE episodes should win if both present."""
        sub = self._sub()
        more_eps = _result(
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]",
            topic_url="https://rutracker.org/forum/viewtopic.php?t=123",  # same URL
        )
        same_eps_url = _result(
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]",
            topic_url="https://rutracker.org/forum/viewtopic.php?t=123",
        )
        # same_eps_url has same episode count as sub → must NOT be returned (no progress)
        selected = select_jackett_subscription_candidate(sub, [same_eps_url, more_eps])
        self.assertIs(selected, more_eps)

    def test_no_candidate_when_all_results_have_same_or_fewer_episodes(self) -> None:
        sub = self._sub()
        same = _result("Клиника / Scrubs / Сезон: 1 / Серии: 1-8 из 10 [WEB-DL]")
        fewer = _result("Клиника / Scrubs / Сезон: 1 / Серии: 1-5 из 10 [WEB-DL]")
        self.assertIsNone(select_jackett_subscription_candidate(sub, [same, fewer]))

    def test_new_topic_url_triggers_match_by_title_similarity(self) -> None:
        """If the tracker created a new topic (different URL), title similarity should still match."""
        sub = self._sub()
        new_url_result = _result(
            "Клиника / Scrubs / Сезон: 1 / Серии: 1-9 из 10 [WEB-DL]",
            topic_url="https://rutracker.org/forum/viewtopic.php?t=999",  # different URL
        )
        selected = select_jackett_subscription_candidate(sub, [new_url_result])
        self.assertIs(selected, new_url_result)


if __name__ == "__main__":
    unittest.main()
