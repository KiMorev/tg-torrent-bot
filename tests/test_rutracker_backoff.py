import unittest

import requests

from rutracker import RutrackerClient, RutrackerError, RutrackerTopicUnavailable


class FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", content: bytes = b"") -> None:
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class SequenceSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.get_calls = 0
        self.post_calls = 0
        self.cookies = {}

    def get(self, *args, **kwargs) -> FakeResponse:
        self.get_calls += 1
        return self.responses.pop(0)

    def post(self, *args, **kwargs) -> FakeResponse:
        self.post_calls += 1
        return self.responses.pop(0)


class RutrackerBackoffTests(unittest.TestCase):
    def _client(self, session: SequenceSession) -> RutrackerClient:
        client = RutrackerClient(
            "user",
            "pass",
            backoff_base_seconds=10,
            backoff_max_seconds=30,
        )
        client._session = session
        client._logged_in = True
        return client

    def test_search_sets_cooldown_after_http_503(self) -> None:
        session = SequenceSession([FakeResponse(status_code=503, text="busy")])
        client = self._client(session)

        with self.assertRaisesRegex(RutrackerError, "отложен"):
            client.search("movie")

        self.assertEqual(session.get_calls, 1)
        self.assertEqual(client._cooldown_reason, "HTTP 503")
        self.assertGreater(client._cooldown_remaining_seconds(), 0)

    def test_search_uses_active_cooldown_without_http_request(self) -> None:
        session = SequenceSession([FakeResponse(status_code=503, text="busy")])
        client = self._client(session)

        with self.assertRaises(RutrackerError):
            client.search("movie")

        with self.assertRaisesRegex(RutrackerError, "временно на паузе"):
            client.search("movie")

        self.assertEqual(session.get_calls, 1)

    def test_backoff_doubles_after_repeated_failures(self) -> None:
        session = SequenceSession([
            FakeResponse(status_code=503, text="busy"),
            FakeResponse(status_code=503, text="busy"),
        ])
        client = self._client(session)

        with self.assertRaises(RutrackerError):
            client.search("movie")

        client._cooldown_until = 0

        with self.assertRaises(RutrackerError):
            client.search("movie")

        self.assertEqual(client._backoff_current_seconds, 20)

    def test_successful_search_clears_previous_backoff(self) -> None:
        session = SequenceSession([FakeResponse(text="<html><body><table></table></body></html>")])
        client = self._client(session)
        client._backoff_current_seconds = 10
        client._cooldown_reason = "HTTP 503"
        client._cooldown_until = 0

        self.assertEqual(client.search("movie"), [])

        self.assertEqual(client._backoff_current_seconds, 0)
        self.assertEqual(client._cooldown_reason, "")

    def test_diagnostics_reports_active_cooldown_without_http_request(self) -> None:
        session = SequenceSession([])
        client = self._client(session)
        client._set_backoff("капча")

        result = client.diagnose()

        self.assertIn("временно на паузе", result["error"])
        self.assertEqual(session.get_calls, 0)

    def test_get_topic_title_404_raises_unavailable_without_backoff(self) -> None:
        session = SequenceSession([FakeResponse(status_code=404, text="not found")])
        client = self._client(session)

        with self.assertRaises(RutrackerTopicUnavailable):
            client.get_topic_title("123")

        self.assertEqual(client._cooldown_remaining_seconds(), 0)

    def test_get_topic_title_deleted_page_raises_unavailable_without_backoff(self) -> None:
        session = SequenceSession([FakeResponse(text="<html><body>Такой темы не существует</body></html>")])
        client = self._client(session)

        with self.assertRaises(RutrackerTopicUnavailable):
            client.get_topic_title("123")

        self.assertEqual(client._cooldown_remaining_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
