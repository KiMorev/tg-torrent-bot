import unittest

import requests

from jackett import JackettClient, JackettError, JackettMagnetRedirect


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        content: bytes = b"",
        json_data=None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.content = content or text.encode()
        self._json_data = json_data
        self.history = []
        self.url = "http://jackett.local/api/v2.0/indexers"

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

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


class RaisingSession:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.headers = {}

    def get(self, *args, **kwargs):
        raise self.error

    def close(self) -> None:
        return None


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

    def test_get_indexers_masks_api_key_in_request_errors(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        client._session = RaisingSession(
            requests.RequestException("GET http://jackett.local/api/v2.0/indexers?apikey=secret&configured=true")
        )

        with self.assertRaises(JackettError) as cm:
            client.get_indexers()

        message = str(cm.exception)
        self.assertNotIn("secret", message)
        self.assertIn("apikey=***", message)

    def test_search_masks_api_key_in_request_errors(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        client._session = RaisingSession(
            requests.ConnectionError("GET http://jackett.local/api/v2.0/indexers/all/results?apikey=secret")
        )

        with self.assertRaises(JackettError) as cm:
            client.search("movie")

        message = str(cm.exception)
        self.assertNotIn("secret", message)
        self.assertIn("apikey=***", message)


class DownloadTorrentTests(unittest.TestCase):
    """Tests for JackettClient.download_torrent redirect & error handling."""

    _TORRENT_BYTES = b"d8:announce39:http://tracker.example.com/announcee"

    def _client_with_session(self, session) -> JackettClient:
        client = JackettClient("http://jackett.local:9117", "secret")
        client._session = session
        return client

    def test_direct_torrent_response_returned(self) -> None:
        client = self._client_with_session(SequenceSession([
            FakeResponse(status_code=200, content=self._TORRENT_BYTES),
        ]))
        result = client.download_torrent("http://jackett.local/dl/rt/?jackett_apikey=secret&path=X")
        self.assertEqual(result, self._TORRENT_BYTES)

    def test_magnet_redirect_raises_JackettMagnetRedirect(self) -> None:
        magnet = "magnet:?xt=urn:btih:abc123&dn=Test"
        client = self._client_with_session(SequenceSession([
            FakeResponse(status_code=302, headers={"Location": magnet}),
        ]))
        with self.assertRaises(JackettMagnetRedirect) as cm:
            client.download_torrent("http://jackett.local/dl/rt/?jackett_apikey=secret&path=X")
        self.assertEqual(cm.exception.magnet_url, magnet)

    def test_http_redirect_is_followed(self) -> None:
        client = self._client_with_session(SequenceSession([
            FakeResponse(status_code=302, headers={"Location": "http://jackett.local/dl/rt/redirected"}),
            FakeResponse(status_code=200, content=self._TORRENT_BYTES),
        ]))
        result = client.download_torrent("http://jackett.local/dl/rt/?jackett_apikey=secret&path=X")
        self.assertEqual(result, self._TORRENT_BYTES)

    def test_404_raises_JackettError(self) -> None:
        client = self._client_with_session(SequenceSession([
            FakeResponse(status_code=404, text="Not Found"),
        ]))
        with self.assertRaises(JackettError) as cm:
            client.download_torrent("http://jackett.local/dl/rt/?jackett_apikey=secret&path=X")
        self.assertIn("404", str(cm.exception))
        self.assertNotIn("secret", str(cm.exception))

    def test_html_response_raises_JackettError(self) -> None:
        client = self._client_with_session(SequenceSession([
            FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, text="<html>Login</html>"),
        ]))
        with self.assertRaises(JackettError) as cm:
            client.download_torrent("http://jackett.local/dl/rt/?jackett_apikey=secret&path=X")
        self.assertIn("HTML", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
