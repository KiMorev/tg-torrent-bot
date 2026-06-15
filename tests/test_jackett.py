import unittest
import threading
import json

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
        self.requests = []

    def get(self, *args, **kwargs) -> FakeResponse:
        self.requests.append((args, kwargs))
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

    def test_search_uses_configured_read_timeout(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret", search_timeout=90)
        payload = {
            "Results": [{
                "Title": "Movie 2026 1080p",
                "Size": 1_000_000_000,
                "Seeders": 10,
                "TrackerId": "rutracker",
                "Details": "https://rutracker.org/forum/viewtopic.php?t=123",
                "Link": "http://jackett.local/dl/rutracker/?path=ABC",
                "MagnetUri": "",
                "PublishDate": "",
            }],
        }
        session = SequenceSession([FakeResponse(text=json.dumps(payload))])
        client._session = session

        client.search("movie")

        self.assertEqual(session.requests[0][1]["timeout"], (10, 90.0))


class WarmupTests(unittest.TestCase):
    def test_warmup_returns_success_and_tracker_filter(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        payload = {
            "Results": [{"Title": "Movie 1080p"}],
            "Indexers": [
                {"ID": "rutracker", "Name": "Rutracker", "Status": 0, "Results": 1, "Error": ""},
            ],
        }
        session = SequenceSession([FakeResponse(text=json.dumps(payload))])
        client._session = session

        result = client.warmup("1080p", indexers=["rutracker", "kinozal"], timeout=(1, 2))

        self.assertTrue(result["ok"])
        self.assertEqual(result["results_count"], 1)
        self.assertEqual(result["indexers"], ["rutracker", "kinozal"])
        self.assertEqual([st.indexer_id for st in result["indexer_statuses"]], ["rutracker"])
        params = session.requests[0][1]["params"]
        self.assertIn(("Tracker[]", "rutracker"), params)
        self.assertIn(("Tracker[]", "kinozal"), params)
        self.assertEqual(session.requests[0][1]["timeout"], (1, 2))

    def test_warmup_skips_when_client_is_busy(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        client._lock = threading.Lock()
        client._lock.acquire()
        try:
            result = client.warmup("1080p")
        finally:
            client._lock.release()

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "busy")

    def test_get_indexers_if_idle_skips_when_client_is_busy(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        client._lock = threading.Lock()
        client._lock.acquire()
        try:
            result = client.get_indexers_if_idle()
        finally:
            client._lock.release()

        self.assertIsNone(result)


class ParseJsonResultsTests(unittest.TestCase):
    """Tests for _parse_json_results — correct Link/MagnetUri classification."""

    def _client(self) -> JackettClient:
        return JackettClient("http://jackett.local:9117", "secret")

    def _json_response(self, results: list[dict]) -> str:
        import json
        return json.dumps({"Results": results})

    def _base_item(self, **overrides) -> dict:
        item = {
            "Title": "Test Movie 2026 1080p",
            "Size": 1_000_000_000,
            "Seeders": 10,
            "TrackerId": "rutracker",
            "Details": "https://rutracker.org/forum/viewtopic.php?t=1234",
            "Link": "http://jackett.local/dl/rutracker/?jackett_apikey=secret&path=ABC",
            "MagnetUri": "magnet:?xt=urn:btih:abc123",
            "PublishDate": "",
        }
        item.update(overrides)
        return item

    def test_http_link_becomes_torrent_url(self) -> None:
        client = self._client()
        results = client._parse_json_results(self._json_response([self._base_item()]))
        self.assertEqual(results[0].torrent_url, "http://jackett.local/dl/rutracker/?jackett_apikey=secret&path=ABC")
        self.assertEqual(results[0].magnet_url, "magnet:?xt=urn:btih:abc123")

    def test_magnet_in_link_field_is_reclassified_as_magnet_url(self) -> None:
        """Indexer bug: magnet URI placed in Link instead of MagnetUri."""
        client = self._client()
        item = self._base_item(Link="magnet:?xt=urn:btih:wrongfield", MagnetUri="")
        results = client._parse_json_results(self._json_response([item]))
        self.assertIsNone(results[0].torrent_url)
        self.assertEqual(results[0].magnet_url, "magnet:?xt=urn:btih:wrongfield")

    def test_empty_link_gives_none_torrent_url(self) -> None:
        client = self._client()
        item = self._base_item(Link="")
        results = client._parse_json_results(self._json_response([item]))
        self.assertIsNone(results[0].torrent_url)
        self.assertEqual(results[0].magnet_url, "magnet:?xt=urn:btih:abc123")


class ParseIndexerStatusesTests(unittest.TestCase):
    """JackettClient.parse_indexer_statuses extracts per-indexer status from
    the `Indexers` field — used by movie discovery to detect partial Jackett
    outages (e.g. one tracker timed out while others responded) without
    relying on coarse «total results» heuristics."""

    def test_returns_empty_when_field_missing(self):
        # Plain Results response with no Indexers field → empty list, not crash.
        json_text = '{"Results": []}'
        statuses = JackettClient.parse_indexer_statuses(json_text)
        self.assertEqual(statuses, [])

    def test_returns_empty_for_invalid_json(self):
        statuses = JackettClient.parse_indexer_statuses("not json")
        self.assertEqual(statuses, [])

    def test_parses_mix_of_ok_and_failed_indexers(self):
        import json as _json
        payload = {
            "Results": [],
            "Indexers": [
                {"ID": "rutracker", "Name": "RuTracker", "Status": 0, "Results": 100, "Error": ""},
                {"ID": "nnmclub", "Name": "NNM-Club", "Status": 1, "Results": 0, "Error": "Read timeout"},
                {"ID": "kinozal", "Name": "Kinozal", "Status": 0, "Results": 50, "Error": ""},
            ],
        }
        statuses = JackettClient.parse_indexer_statuses(_json.dumps(payload))
        self.assertEqual(len(statuses), 3)
        by_id = {s.indexer_id: s for s in statuses}
        self.assertTrue(by_id["rutracker"].is_ok)
        self.assertEqual(by_id["rutracker"].results, 100)
        self.assertFalse(by_id["nnmclub"].is_ok)
        self.assertEqual(by_id["nnmclub"].error, "Read timeout")
        self.assertTrue(by_id["kinozal"].is_ok)

    def test_lowercases_indexer_id(self):
        """Tracker IDs need consistent casing for merge logic — Jackett may
        return them with different cases across versions / indexers."""
        import json as _json
        payload = {
            "Results": [],
            "Indexers": [
                {"ID": "RUTracker", "Name": "RT", "Status": 0, "Results": 5, "Error": ""},
            ],
        }
        statuses = JackettClient.parse_indexer_statuses(_json.dumps(payload))
        self.assertEqual(statuses[0].indexer_id, "rutracker")

    def test_status_2_with_results_is_treated_as_ok(self):
        """Torznab Status=2 is a WARNING, not a failure. Indexers reporting
        Status=2 alongside Results>0 actually did their job — the warning
        is for non-fatal issues like «captcha solved with delay» or «one
        sub-request timed out but I still got data». Don't mark as failed."""
        from jackett import JackettIndexerStatus
        status_warn_with_data = JackettIndexerStatus(
            indexer_id="rutracker", name="RT",
            status=2, results=50, error="",
        )
        self.assertTrue(status_warn_with_data.is_ok)

    def test_status_2_with_zero_results_is_failed(self):
        """Status=2 + Results=0 = actually got nothing back. Treat as failed
        so the supplement step pulls prev cache for this indexer."""
        from jackett import JackettIndexerStatus
        warn_no_data = JackettIndexerStatus(
            indexer_id="eztv", name="EZTV",
            status=2, results=0, error="",
        )
        self.assertFalse(warn_no_data.is_ok)

    def test_status_1_always_failed(self):
        """Status=1 is a definitive error. Even if Results were somehow >0
        the indexer reported a problem we should respect — but in practice
        Status=1 comes with Results=0."""
        from jackett import JackettIndexerStatus
        err = JackettIndexerStatus(
            indexer_id="noname-club", name="NN",
            status=1, results=0, error="Challenge detected",
        )
        self.assertFalse(err.is_ok)

    def test_status_0_always_ok(self):
        from jackett import JackettIndexerStatus
        ok = JackettIndexerStatus(
            indexer_id="kinozal", name="KZ",
            status=0, results=18, error="",
        )
        self.assertTrue(ok.is_ok)

    def test_skips_malformed_entries(self):
        import json as _json
        payload = {
            "Results": [],
            "Indexers": [
                {"ID": "ok", "Status": 0, "Results": 5, "Error": ""},
                "garbage string",
                {"ID": "broken", "Status": "not-an-int"},  # ValueError on int()
                {"ID": "also-ok", "Status": 0, "Results": 1, "Error": ""},
            ],
        }
        statuses = JackettClient.parse_indexer_statuses(_json.dumps(payload))
        # garbage + broken are skipped, two real ones survive
        self.assertEqual(len(statuses), 2)
        self.assertEqual({s.indexer_id for s in statuses}, {"ok", "also-ok"})


class SearchWithStatusesTests(unittest.TestCase):
    def test_search_with_statuses_returns_results_and_same_response_statuses(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        payload = {
            "Results": [{
                "Title": "Movie 2026 1080p",
                "Size": 1_000_000_000,
                "Seeders": 10,
                "TrackerId": "rutracker",
                "Details": "https://rutracker.org/forum/viewtopic.php?t=123",
                "Link": "http://jackett.local/dl/rutracker/?path=ABC",
                "MagnetUri": "",
                "PublishDate": "",
            }],
            "Indexers": [
                {"ID": "rutracker", "Name": "Rutracker", "Status": 0, "Results": 1, "Error": ""},
                {"ID": "kinozal", "Name": "Kinozal", "Status": 1, "Results": 0, "Error": "timeout"},
            ],
        }
        session = SequenceSession([FakeResponse(text=json.dumps(payload))])
        client._session = session

        results, statuses = client.search_with_statuses("movie")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tracker, "rutracker")
        self.assertEqual([st.indexer_id for st in statuses], ["rutracker", "kinozal"])
        self.assertFalse(statuses[1].is_ok)


class ParseXmlResultsTests(unittest.TestCase):
    """Tests for _parse_results — correct <enclosure> / magneturl handling."""

    def _client(self) -> JackettClient:
        return JackettClient("http://jackett.local:9117", "secret")

    def _xml_item(
        self,
        title: str = "Test 1080p",
        enclosure_url: str = "",
        enclosure_type: str = "application/x-bittorrent",
        link: str = "",
        magnet_attr: str = "",
    ) -> str:
        enclosure = (
            f'<enclosure url="{enclosure_url}" type="{enclosure_type}" length="1000000"/>'
            if enclosure_url else ""
        )
        magnet_elem = (
            f'<torznab:attr name="magneturl" value="{magnet_attr}"/>'
            if magnet_attr else ""
        )
        return f"""<item>
            <title>{title}</title>
            <link>{link}</link>
            <guid>https://rutracker.org/forum/viewtopic.php?t=1</guid>
            <pubDate></pubDate>
            <size>1000000000</size>
            {enclosure}
            {magnet_elem}
            <torznab:attr name="seeders" value="5"/>
            <torznab:attr name="tracker" value="rutracker"/>
        </item>"""

    def _wrap(self, items: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
        <channel>{items}</channel></rss>"""

    def test_enclosure_torrent_type_becomes_torrent_url(self) -> None:
        xml = self._wrap(self._xml_item(
            enclosure_url="http://jackett.local/dl/rt/?path=X",
            enclosure_type="application/x-bittorrent",
            magnet_attr="magnet:?xt=urn:btih:abc",
        ))
        results = self._client()._parse_results(xml)
        self.assertEqual(results[0].torrent_url, "http://jackett.local/dl/rt/?path=X")
        self.assertEqual(results[0].magnet_url, "magnet:?xt=urn:btih:abc")

    def test_enclosure_magnet_type_becomes_magnet_url(self) -> None:
        xml = self._wrap(self._xml_item(
            enclosure_url="magnet:?xt=urn:btih:xyz",
            enclosure_type="application/x-bittorrent;x-scheme-handler/magnet",
        ))
        results = self._client()._parse_results(xml)
        self.assertIsNone(results[0].torrent_url)
        self.assertEqual(results[0].magnet_url, "magnet:?xt=urn:btih:xyz")

    def test_no_enclosure_falls_back_to_link_element(self) -> None:
        xml = self._wrap(self._xml_item(
            link="http://jackett.local/dl/rt/?path=Y",
        ))
        results = self._client()._parse_results(xml)
        self.assertEqual(results[0].torrent_url, "http://jackett.local/dl/rt/?path=Y")

    def test_magnet_in_link_element_reclassified(self) -> None:
        xml = self._wrap(self._xml_item(
            link="magnet:?xt=urn:btih:inlink",
        ))
        results = self._client()._parse_results(xml)
        self.assertIsNone(results[0].torrent_url)
        self.assertEqual(results[0].magnet_url, "magnet:?xt=urn:btih:inlink")


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
