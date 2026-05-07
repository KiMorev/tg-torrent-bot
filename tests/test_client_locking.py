import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from jackett import JackettClient
from kinopoisk import KinopoiskClient
from rutracker import RutrackerClient


class FakeResponse:
    status_code = 403
    text = ""
    content = b""
    headers = {}
    history = []
    url = "http://example.local/"

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"items": []}


class ContentionDetectingSession:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def get(self, *args, **kwargs) -> FakeResponse:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

        try:
            time.sleep(0.02)
            return FakeResponse()
        finally:
            with self._guard:
                self.active -= 1


def _run_in_parallel(callback) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(callback) for _ in range(2)]
        for future in futures:
            future.result()


class ClientLockingTests(unittest.TestCase):
    def test_rutracker_diagnostics_serialize_shared_session_use(self) -> None:
        client = RutrackerClient("user", "pass")
        session = ContentionDetectingSession()
        client._session = session

        _run_in_parallel(client.diagnose)

        self.assertEqual(session.max_active, 1)

    def test_jackett_diagnostics_serialize_shared_session_use(self) -> None:
        client = JackettClient("http://jackett.local:9117", "secret")
        session = ContentionDetectingSession()
        client._session = session

        _run_in_parallel(client.test_connection)

        self.assertEqual(session.max_active, 1)

    def test_kinopoisk_search_serialize_shared_session_use(self) -> None:
        client = KinopoiskClient("secret")
        session = ContentionDetectingSession()
        client._session = session

        _run_in_parallel(lambda: client.search_series_seasons("test"))

        self.assertEqual(session.max_active, 1)


if __name__ == "__main__":
    unittest.main()
