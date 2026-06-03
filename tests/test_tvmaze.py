import unittest
from unittest.mock import MagicMock

from tvmaze import TVmazeClient


class TVmazeClientTests(unittest.TestCase):
    def _response(self, payload: object, *, status_code: int = 200) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def test_season_episode_count_resolves_tvdb_id(self) -> None:
        client = TVmazeClient()
        client._session = MagicMock()
        client._session.get.side_effect = [
            self._response({"id": 50701}),
            self._response([
                {"id": 1, "number": 1, "episodeOrder": 5},
                {"id": 2, "number": 2, "episodeOrder": 5},
            ]),
        ]

        total = client.season_episode_count(tvdb_id="367178", season_number=2)

        self.assertEqual(total, 5)
        first_call = client._session.get.call_args_list[0]
        self.assertIn("/lookup/shows", first_call.args[0])
        self.assertEqual(first_call.kwargs["params"], {"thetvdb": "367178"})

    def test_season_episode_count_falls_back_to_episode_list(self) -> None:
        client = TVmazeClient()
        client._session = MagicMock()
        client._session.get.side_effect = [
            self._response({"id": 50701}),
            self._response([{"id": 100, "number": 1, "episodeOrder": None}]),
            self._response([{"id": 1}, {"id": 2}, {"id": 3}]),
        ]

        total = client.season_episode_count(imdb_id="tt2531336", season_number=1)

        self.assertEqual(total, 3)
        self.assertIn("/seasons/100/episodes", client._session.get.call_args_list[2].args[0])

    def test_season_episode_count_returns_none_without_external_id(self) -> None:
        client = TVmazeClient()
        client._session = MagicMock()

        self.assertIsNone(client.season_episode_count(season_number=1))
        client._session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
