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


if __name__ == "__main__":
    unittest.main()
