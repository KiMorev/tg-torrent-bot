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
    """The severity classifier in bot.py."""

    def setUp(self):
        import bot
        self.bot = bot

    def _fake_volume(self, free_pct: float) -> dict:
        total = 1_000_000_000_000
        free = int(total * free_pct / 100)
        return {
            "total_bytes": total, "free_bytes": free,
            "used_pct": 100.0 - free_pct, "mount_point": "/volume1",
        }

    def test_returns_none_when_disk_healthy(self):
        fake_ds = MagicMock()
        fake_ds.get_volume_info.return_value = self._fake_volume(40.0)
        with patch.object(self.bot, "ds_client", fake_ds):
            self.assertIsNone(self.bot._check_disk_space_for_download())

    def test_returns_warn_at_10_pct_free(self):
        fake_ds = MagicMock()
        fake_ds.get_volume_info.return_value = self._fake_volume(10.0)
        with patch.object(self.bot, "ds_client", fake_ds):
            result = self.bot._check_disk_space_for_download()
        self.assertIsNotNone(result)
        severity, msg = result
        self.assertEqual(severity, "warn")
        self.assertIn("заканчивается", msg.lower())

    def test_returns_block_at_3_pct_free(self):
        fake_ds = MagicMock()
        fake_ds.get_volume_info.return_value = self._fake_volume(3.0)
        with patch.object(self.bot, "ds_client", fake_ds):
            result = self.bot._check_disk_space_for_download()
        self.assertIsNotNone(result)
        severity, msg = result
        self.assertEqual(severity, "block")
        self.assertIn("Недостаточно", msg)

    def test_returns_none_when_volume_info_unavailable(self):
        """DSM didn't return volume info (older version, etc) → no block."""
        fake_ds = MagicMock()
        fake_ds.get_volume_info.return_value = None
        with patch.object(self.bot, "ds_client", fake_ds):
            self.assertIsNone(self.bot._check_disk_space_for_download())

    def test_returns_none_when_ds_client_none(self):
        """Bot started without DS → must not crash, must not block."""
        with patch.object(self.bot, "ds_client", None):
            self.assertIsNone(self.bot._check_disk_space_for_download())

    def test_returns_none_when_ds_check_raises(self):
        """Defensive: any unexpected exception → graceful skip, never block download."""
        fake_ds = MagicMock()
        fake_ds.get_volume_info.side_effect = RuntimeError("boom")
        with patch.object(self.bot, "ds_client", fake_ds):
            self.assertIsNone(self.bot._check_disk_space_for_download())


class DiagnosticsDiskLineTests(unittest.TestCase):
    """The new «💾 Место» line in /admin → Download Station block."""

    def test_volume_line_appears_when_dsm_exposes_info(self):
        from diagnostics import _download_station_diagnostic
        ds = MagicMock()
        ds.list_tasks.return_value = []
        ds.get_volume_info.return_value = {
            "total_bytes": 1_000_000_000_000,
            "free_bytes": 400_000_000_000,
            "used_pct": 60.0,
            "mount_point": "/volume1",
        }
        diag = _download_station_diagnostic(ds)
        joined = "\n".join(diag.details)
        self.assertIn("Место", joined)
        self.assertIn("/volume1", joined)
        self.assertEqual(diag.status, "ok")

    def test_volume_line_omitted_when_api_unavailable(self):
        """If DSM doesn't expose volume info, the rest of the diagnostic
        still works — we just don't show the line."""
        from diagnostics import _download_station_diagnostic
        ds = MagicMock()
        ds.list_tasks.return_value = []
        ds.get_volume_info.return_value = None
        diag = _download_station_diagnostic(ds)
        joined = "\n".join(diag.details)
        self.assertNotIn("Место", joined)
        self.assertEqual(diag.status, "ok")

    def test_status_becomes_warn_when_low_space(self):
        from diagnostics import _download_station_diagnostic
        ds = MagicMock()
        ds.list_tasks.return_value = []
        # 10% free → warn band
        ds.get_volume_info.return_value = {
            "total_bytes": 1000, "free_bytes": 100,
            "used_pct": 90.0, "mount_point": "/volume1",
        }
        diag = _download_station_diagnostic(ds)
        self.assertEqual(diag.status, "warn")

    def test_status_becomes_error_when_critical_space(self):
        from diagnostics import _download_station_diagnostic
        ds = MagicMock()
        ds.list_tasks.return_value = []
        # 2% free → critical
        ds.get_volume_info.return_value = {
            "total_bytes": 1000, "free_bytes": 20,
            "used_pct": 98.0, "mount_point": "/volume1",
        }
        diag = _download_station_diagnostic(ds)
        self.assertEqual(diag.status, "error")

    def test_get_volume_info_attribute_missing_doesnt_crash(self):
        """Legacy tests use a Fake without get_volume_info — must not break."""
        from diagnostics import _download_station_diagnostic

        class LegacyFake:
            def list_tasks(self):
                return []
        diag = _download_station_diagnostic(LegacyFake())
        self.assertEqual(diag.status, "ok")
        joined = "\n".join(diag.details)
        self.assertNotIn("Место", joined)


if __name__ == "__main__":
    unittest.main()
