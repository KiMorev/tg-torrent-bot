import threading
import time
import urllib.error
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from download_station import DownloadStationClient, DownloadStationError


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


class ContentionDetectingDownloadStationClient(DownloadStationClient):
    def __init__(self) -> None:
        super().__init__(
            "https://nas.example:5001",
            "account",
            "password",
            destination="video",
        )
        self._guard = threading.Lock()
        self.active_requests = 0
        self.max_active_requests = 0

    def _request(self, path: str, params: dict[str, str], method: str = "GET") -> dict:
        with self._guard:
            self.active_requests += 1
            self.max_active_requests = max(self.max_active_requests, self.active_requests)

        try:
            time.sleep(0.02)
            api = params.get("api")
            request_method = params.get("method")
            if api == "SYNO.API.Auth" and request_method == "login":
                return {"success": True, "data": {"sid": "sid"}}
            if api == "SYNO.API.Auth" and request_method == "logout":
                return {"success": True}
            if api == "SYNO.DownloadStation.Task" and request_method == "list":
                return {"success": True, "data": {"tasks": []}}
            return {"success": True}
        finally:
            with self._guard:
                self.active_requests -= 1


class DownloadStationLockingTests(unittest.TestCase):
    def test_download_station_operations_serialize_session_cycles(self) -> None:
        client = ContentionDetectingDownloadStationClient()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(client.list_tasks) for _ in range(2)]
            for future in futures:
                future.result()

        self.assertEqual(client.max_active_requests, 1)


class DownloadStationSecurityTests(unittest.TestCase):
    def test_login_uses_post_so_password_is_not_in_url(self) -> None:
        client = DownloadStationClient(
            "https://nas.example:5001",
            "account",
            "secret",
            destination="video",
        )
        requests = []

        def fake_urlopen(request, **kwargs):
            requests.append(request)
            return FakeResponse(b'{"success": true, "data": {"sid": "sid123"}}')

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client._login(), "sid123")

        request = requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("passwd=", request.full_url)
        self.assertIn(b"passwd=secret", request.data)

    def test_connection_errors_sanitize_password_and_sid(self) -> None:
        client = DownloadStationClient(
            "https://nas.example:5001",
            "account",
            "secret",
            destination="video",
            retry_attempts=1,
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(
                "https://nas.example/webapi/auth.cgi?passwd=secret&_sid=sid123"
            ),
        ):
            with self.assertRaises(DownloadStationError) as cm:
                client._login()

        message = str(cm.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn("sid123", message)
        self.assertIn("passwd=***", message)
        self.assertIn("_sid=***", message)


if __name__ == "__main__":
    unittest.main()
