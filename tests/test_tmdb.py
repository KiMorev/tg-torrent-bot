import unittest
from unittest.mock import MagicMock

from tmdb import TMDBClient


class TMDBClientTests(unittest.TestCase):
    def _response(self, payload: dict) -> MagicMock:
        response = MagicMock()
        response.json.return_value = payload
        return response

    def test_season_episode_count_uses_direct_tmdb_id(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()
        client._session.get.return_value = self._response({
            "episodes": [{"id": 1}, {"id": 2}, {"id": 3}],
        })

        total = client.season_episode_count(tmdb_id="85862", season_number=5)

        self.assertEqual(total, 3)
        client._session.get.assert_called_once()
        self.assertIn("/tv/85862/season/5", client._session.get.call_args.args[0])

    def test_season_episode_count_resolves_tvdb_id(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()
        client._session.get.side_effect = [
            self._response({"tv_results": [{"id": 85862}]}),
            self._response({"episodes": [{"id": 1}, {"id": 2}]}),
        ]

        total = client.season_episode_count(tvdb_id="362204", season_number=5)

        self.assertEqual(total, 2)
        first_call = client._session.get.call_args_list[0]
        self.assertIn("/find/362204", first_call.args[0])
        self.assertEqual(first_call.kwargs["params"], {"external_source": "tvdb_id"})

    def test_season_episode_count_returns_none_without_external_id(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()

        self.assertIsNone(client.season_episode_count(season_number=5))
        client._session.get.assert_not_called()

    def test_season_episode_counts_uses_direct_tmdb_id(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()
        client._session.get.return_value = self._response({
            "seasons": [
                {"season_number": 0, "episode_count": 3},
                {"season_number": 1, "episode_count": 20},
                {"season_number": 2, "episode_count": 18},
                {"season_number": 3, "episode_count": None},
            ],
        })

        totals = client.season_episode_counts(tmdb_id="85862")

        self.assertEqual(totals, {1: 20, 2: 18})
        client._session.get.assert_called_once()
        self.assertIn("/tv/85862", client._session.get.call_args.args[0])

    def test_season_episode_counts_resolves_tvdb_id(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()
        client._session.get.side_effect = [
            self._response({"tv_results": [{"id": 85862}]}),
            self._response({"seasons": [{"season_number": 7, "episode_count": 18}]}),
        ]

        totals = client.season_episode_counts(tvdb_id="362204")

        self.assertEqual(totals, {7: 18})
        first_call = client._session.get.call_args_list[0]
        self.assertIn("/find/362204", first_call.args[0])
        self.assertEqual(first_call.kwargs["params"], {"external_source": "tvdb_id"})

    def test_season_aired_episode_count_counts_only_released_episodes(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()
        client._session.get.return_value = self._response({
            "episodes": [
                {"id": 1, "air_date": "2024-03-07"},
                {"id": 2, "air_date": "2024-03-14"},
                {"id": 3, "air_date": "2999-01-01"},
                {"id": 4, "air_date": ""},
            ],
        })

        total = client.season_aired_episode_count(tmdb_id="85862", season_number=2)

        self.assertEqual(total, 2)
        client._session.get.assert_called_once()
        self.assertIn("/tv/85862/season/2", client._session.get.call_args.args[0])

    def test_season_released_episode_counts_skips_future_and_empty_seasons(self) -> None:
        client = TMDBClient("token")
        client._session = MagicMock()
        client._session.get.return_value = self._response({
            "seasons": [
                {"season_number": 1, "episode_count": 8, "air_date": "2024-03-07"},
                {"season_number": 2, "episode_count": 8, "air_date": "2999-01-01"},
                {"season_number": 3, "episode_count": 8, "air_date": None},
                {"season_number": 4, "episode_count": 0, "air_date": "2024-03-07"},
            ],
        })

        totals = client.season_released_episode_counts(tmdb_id="85862")

        self.assertEqual(totals, {1: 8})


if __name__ == "__main__":
    unittest.main()
