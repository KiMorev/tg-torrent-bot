import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from download_station import DownloadStationClient


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


if __name__ == "__main__":
    unittest.main()
