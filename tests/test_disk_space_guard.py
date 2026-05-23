"""Tests for the disk-space guard (R.1 in improvements-roadmap.md).

Three layers:
1. DownloadStationClient.get_volume_info() — parsing DSM response into
   normalised {total_bytes, free_bytes, used_pct, mount_point}.
2. _check_disk_space_for_download() in bot — severity classification
   (block / warn / None) based on free percentage.
3. Diagnostics integration — Volume line in the Download Station block.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "111:testtoken")
os.environ.setdefault("ALLOWED_CHAT_IDS", "100")
os.environ.setdefault("DS_URL", "https://nas.local:5001")
os.environ.setdefault("DS_ACCOUNT", "testuser")
os.environ.setdefault("DS_PASSWORD", "testpass")
os.environ.setdefault("DS_DESTINATION", "video")

from download_station import DownloadStationClient


def _client(destination: str = "video") -> DownloadStationClient:
    return DownloadStationClient(
        base_url="https://nas.local:5001",
        account="u",
        password="p",
        destination=destination,
        verify_ssl=False,
    )


class GetVolumeInfoParseTests(unittest.TestCase):
    """How the raw DSM response is normalised."""

    def _patched_client(
        self,
        client: DownloadStationClient,
        volumes_response: dict | Exception | None,
    ):
        """Bypass the actual HTTP + login by patching ``_request`` directly."""
        def fake_request(path, params, method="GET"):
            # Login → return fake sid
            if path == "/webapi/auth.cgi":
                return {"data": {"sid": "fake-sid"}}
            # Volume list
            if isinstance(volumes_response, Exception):
                raise volumes_response
            return {"data": {"volumes": volumes_response or []}}
        return patch.object(client, "_request", side_effect=fake_request)

    def test_returns_normalised_info_for_matching_volume(self):
        c = _client(destination="video")
        volumes = [{
            "volume_path": "/volume1",
            "size": {"total": "1000000000000", "free": "200000000000"},
        }]
        with self._patched_client(c, volumes):
            info = c.get_volume_info(use_cache=False)
        self.assertIsNotNone(info)
        self.assertEqual(info["total_bytes"], 1_000_000_000_000)
        self.assertEqual(info["free_bytes"], 200_000_000_000)
        self.assertAlmostEqual(info["used_pct"], 80.0, places=1)
        self.assertEqual(info["mount_point"], "/volume1")

    def test_longest_prefix_wins_for_nested_volumes(self):
        c = _client(destination="/volume2/media/movies")
        volumes = [
            {"volume_path": "/volume1", "size": {"total": "100", "free": "10"}},
            {"volume_path": "/volume2", "size": {"total": "200", "free": "20"}},
            {"volume_path": "/volume2/media", "size": {"total": "50", "free": "5"}},
        ]
        with self._patched_client(c, volumes):
            info = c.get_volume_info(use_cache=False)
        # /volume2/media is the deepest prefix of /volume2/media/movies
        self.assertEqual(info["mount_point"], "/volume2/media")
        self.assertEqual(info["total_bytes"], 50)

    def test_single_volume_fallback_when_no_prefix_match(self):
        """User configured DS_DESTINATION as relative path 'video' — no
        volume_path matches, but there's only one volume → use it."""
        c = _client(destination="video")
        volumes = [{
            "volume_path": "/volume1",
            "size": {"total": "1000", "free": "500"},
        }]
        with self._patched_client(c, volumes):
            info = c.get_volume_info(use_cache=False)
        self.assertIsNotNone(info)
        self.assertEqual(info["mount_point"], "/volume1")

    def test_returns_none_when_dsm_lacks_volume_api(self):
        """Older DSM returns an API error for SYNO.Core.Storage.Volume —
        we degrade gracefully, not crash."""
        from download_station import DownloadStationError
        c = _client()
        with self._patched_client(c, DownloadStationError("API not supported")):
            info = c.get_volume_info(use_cache=False)
        self.assertIsNone(info)

    def test_returns_none_when_no_volumes_at_all(self):
        c = _client()
        with self._patched_client(c, []):
            info = c.get_volume_info(use_cache=False)
        self.assertIsNone(info)

    def test_returns_none_when_total_size_is_zero(self):
        """Defensive: division by zero must never happen."""
        c = _client()
        volumes = [{"volume_path": "/volume1", "size": {"total": "0", "free": "0"}}]
        with self._patched_client(c, volumes):
            info = c.get_volume_info(use_cache=False)
        self.assertIsNone(info)

    def test_used_pct_calculated_correctly(self):
        c = _client(destination="video")
        volumes = [{
            "volume_path": "/volume1",
            "size": {"total": "1000", "free": "150"},  # 15% free → 85% used
        }]
        with self._patched_client(c, volumes):
            info = c.get_volume_info(use_cache=False)
        self.assertAlmostEqual(info["used_pct"], 85.0, places=1)
        self.assertEqual(info["free_bytes"], 150)


class VolumeInfoCacheTests(unittest.TestCase):
    """60-second TTL on volume info — avoid hammering DSM on rapid button taps."""

    def test_second_call_within_ttl_hits_cache(self):
        c = _client()
        c._volume_cache_ttl = 60.0

        call_count = {"n": 0}
        def fake_request(path, params, method="GET"):
            if path == "/webapi/auth.cgi":
                return {"data": {"sid": "fake-sid"}}
            call_count["n"] += 1
            return {"data": {"volumes": [{
                "volume_path": "/volume1",
                "size": {"total": "1000", "free": "500"},
            }]}}

        with patch.object(c, "_request", side_effect=fake_request):
            c.get_volume_info(use_cache=False)
            self.assertEqual(call_count["n"], 1)
            # Same call again with use_cache=True → must not hit DSM
            info2 = c.get_volume_info(use_cache=True)
            self.assertEqual(call_count["n"], 1)
            self.assertEqual(info2["total_bytes"], 1000)

    def test_use_cache_false_forces_refresh(self):
        c = _client()
        call_count = {"n": 0}
        def fake_request(path, params, method="GET"):
            if path == "/webapi/auth.cgi":
                return {"data": {"sid": "fake-sid"}}
            call_count["n"] += 1
            return {"data": {"volumes": [{
                "volume_path": "/volume1",
                "size": {"total": "1000", "free": "500"},
            }]}}

        with patch.object(c, "_request", side_effect=fake_request):
            c.get_volume_info(use_cache=False)
            c.get_volume_info(use_cache=False)
        self.assertEqual(call_count["n"], 2)


class CheckDiskSpaceForDownloadTests(unittest.TestCase):
    """The severity classifier in bot.py — now uses get_unified_disk_info
    (mount-first, DSM-fallback). We patch the unified helper directly to
    keep tests focused on classification, not source selection."""

    def setUp(self):
        import bot
        self.bot = bot
        from storage import StorageInfo
        self.StorageInfo = StorageInfo

    def _fake_info(self, free_pct: float):
        total = 1_000_000_000_000
        free = int(total * free_pct / 100)
        used = total - free
        return self.StorageInfo(
            total_bytes=total, used_bytes=used, free_bytes=free,
            used_percent=100.0 - free_pct,
        )

    def test_returns_none_when_disk_healthy(self):
        with patch.object(self.bot, "get_unified_disk_info",
                          return_value=self._fake_info(40.0)):
            self.assertIsNone(self.bot._check_disk_space_for_download())

    def test_returns_warn_at_10_pct_free(self):
        with patch.object(self.bot, "get_unified_disk_info",
                          return_value=self._fake_info(10.0)):
            result = self.bot._check_disk_space_for_download()
        self.assertIsNotNone(result)
        severity, msg = result
        self.assertEqual(severity, "warn")
        self.assertIn("заканчивается", msg.lower())

    def test_returns_block_at_3_pct_free(self):
        with patch.object(self.bot, "get_unified_disk_info",
                          return_value=self._fake_info(3.0)):
            result = self.bot._check_disk_space_for_download()
        self.assertIsNotNone(result)
        severity, msg = result
        self.assertEqual(severity, "block")
        self.assertIn("Недостаточно", msg)

    def test_returns_none_when_unified_returns_none(self):
        """No source available (no mount, no DSM API) → no block."""
        with patch.object(self.bot, "get_unified_disk_info", return_value=None):
            self.assertIsNone(self.bot._check_disk_space_for_download())

    def test_returns_none_when_unified_raises(self):
        """Defensive: any unexpected exception → graceful skip."""
        with patch.object(self.bot, "get_unified_disk_info",
                          side_effect=RuntimeError("boom")):
            self.assertIsNone(self.bot._check_disk_space_for_download())


class UnifiedDiskInfoTests(unittest.TestCase):
    """get_unified_disk_info: mount-first, DSM-fallback strategy."""

    def test_mount_path_present_uses_shutil(self):
        from storage import get_unified_disk_info, StorageInfo
        ds = MagicMock()  # would be called only if mount fails
        with (
            patch("storage.Path") as path_cls,
            patch("storage.shutil.disk_usage", return_value=(1000, 400, 600)),
        ):
            path_cls.return_value.exists.return_value = True
            info = get_unified_disk_info(ds, mount_path="/storage")
        self.assertIsNotNone(info)
        self.assertEqual(info.total_bytes, 1000)
        self.assertEqual(info.free_bytes, 600)
        # DSM API NOT called when mount answered.
        ds.get_volume_info.assert_not_called()

    def test_no_mount_falls_back_to_dsm(self):
        from storage import get_unified_disk_info
        ds = MagicMock()
        ds.get_volume_info.return_value = {
            "total_bytes": 2_000_000_000_000,
            "free_bytes": 400_000_000_000,
            "used_pct": 80.0, "mount_point": "/volume1",
        }
        with patch("storage.Path") as path_cls:
            path_cls.return_value.exists.return_value = False
            info = get_unified_disk_info(ds, mount_path="/storage")
        self.assertIsNotNone(info)
        self.assertEqual(info.total_bytes, 2_000_000_000_000)
        self.assertEqual(info.free_bytes, 400_000_000_000)
        ds.get_volume_info.assert_called_once()

    def test_no_mount_no_dsm_returns_none(self):
        from storage import get_unified_disk_info
        with patch("storage.Path") as path_cls:
            path_cls.return_value.exists.return_value = False
            self.assertIsNone(get_unified_disk_info(ds_client=None))

    def test_dsm_returning_none_returns_none(self):
        from storage import get_unified_disk_info
        ds = MagicMock()
        ds.get_volume_info.return_value = None
        with patch("storage.Path") as path_cls:
            path_cls.return_value.exists.return_value = False
            self.assertIsNone(get_unified_disk_info(ds))

    def test_dsm_raising_is_swallowed(self):
        from storage import get_unified_disk_info
        ds = MagicMock()
        ds.get_volume_info.side_effect = RuntimeError("network down")
        with patch("storage.Path") as path_cls:
            path_cls.return_value.exists.return_value = False
            self.assertIsNone(get_unified_disk_info(ds))


if __name__ == "__main__":
    unittest.main()
