import unittest

from jackett import JackettClient


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        json_data=None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_data = json_data
        self.history = []
        self.url = "http://jackett.local/api/v2.0/indexers"

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if isinstance(self._json_data, BaseException):
            raise self._json_data
        return self._json_data


class SequenceSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.headers = {}

    def get(self, *args, **kwargs) -> FakeResponse:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class JackettStartupRetryTests(unittest.TestCase):
    def test_diagnostics_retries_transient_startup_html_before_reporting_failure(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        session = SequenceSession([
            FakeResponse(headers={"Content-Type": "text/html"}, text="<html>starting</html>"),
            FakeResponse(
                headers={"Content-Type": "application/json"},
                json_data=[{"id": "rutracker", "name": "Rutracker", "configured": True}],
            ),
        ])
        client._session = session

        diag = client.test_connection()

        self.assertTrue(diag["api_ok"])
        self.assertEqual(diag["indexers"], [{"id": "rutracker", "name": "Rutracker"}])
        self.assertEqual(session.calls, 2)

    def test_get_indexers_retries_transient_non_json_response(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        session = SequenceSession([
            FakeResponse(headers={"Content-Type": "application/json"}, json_data=ValueError("empty")),
            FakeResponse(
                headers={"Content-Type": "application/json"},
                json_data=[
                    {"id": "rutracker", "name": "Rutracker", "configured": True},
                    {"id": "disabled", "name": "Disabled", "configured": False},
                ],
            ),
        ])
        client._session = session

        self.assertEqual(client.get_indexers(), [{"id": "rutracker", "name": "Rutracker"}])
        self.assertEqual(session.calls, 2)


if __name__ == "__main__":
    unittest.main()
