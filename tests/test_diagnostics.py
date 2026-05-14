import unittest
from datetime import timezone

from diagnostics import format_diagnostics, friendly_error, run_diagnostics
from download_station import DownloadStationError


class FakeRutracker:
    def __init__(self, status: dict | None = None, exc: Exception | None = None) -> None:
        self.status = status or {"login_ok": True}
        self.exc = exc

    def diagnose(self) -> dict:
        if self.exc:
            raise self.exc
        return self.status


class FakeJackett:
    def __init__(self, diag: dict | None = None, exc: Exception | None = None) -> None:
        self.diag = diag or {"api_ok": True, "indexers": [{"name": "Rutracker"}, {"name": "NNMClub"}]}
        self.exc = exc

    def test_connection(self) -> dict:
        if self.exc:
            raise self.exc
        return self.diag


class FakeDownloadStation:
    def __init__(self, tasks: list[dict] | None = None, exc: Exception | None = None) -> None:
        self.tasks = tasks or []
        self.exc = exc

    def list_tasks(self) -> list[dict]:
        if self.exc:
            raise self.exc
        return self.tasks


class FakeTrackerService:
    def __init__(
        self,
        *,
        enabled: bool = True,
        trackers: list[str] | None = None,
        fresh_trackers: list[str] | None = None,
        cache_time: float | None = 0,
        exc: Exception | None = None,
    ) -> None:
        self.enabled = enabled
        self.trackers = trackers if trackers is not None else ["udp://tracker.one/announce"]
        self.fresh_trackers = self.trackers if fresh_trackers is None else fresh_trackers
        self.cache_time = cache_time
        self.exc = exc

    def public_trackers_enabled(self) -> bool:
        return self.enabled

    def read_cache(self, require_fresh: bool = True) -> tuple[list[str], float | None]:
        if self.exc:
            raise self.exc
        if require_fresh:
            return self.fresh_trackers, self.cache_time
        return self.trackers, self.cache_time


class DiagnosticsTests(unittest.TestCase):
    def test_run_diagnostics_collects_core_statuses(self) -> None:
        report = run_diagnostics(
            rutracker_client=FakeRutracker(),
            jackett_client=FakeJackett(),
            ds_client=FakeDownloadStation([{"id": "1"}, {"id": "2"}]),
            tracker_service=FakeTrackerService(trackers=["udp://one", "udp://two"]),
            display_timezone=timezone.utc,
        )

        text = format_diagnostics(report)

        self.assertIn("✅ 🧲 <b>Download Station</b>: подключен", text)
        self.assertIn("Задач: 2", text)
        self.assertIn("✅ 🔎 <b>Rutracker</b>: подключен", text)
        self.assertIn("✅ 🌐 <b>Jackett</b>: подключен", text)
        self.assertIn("Индексеры: Rutracker, NNMClub", text)
        self.assertIn("✅ ➕ <b>Public-трекеры</b>: кэш готов", text)
        self.assertIn("Доступно: 2", text)
        self.assertNotIn("Кинопоиск", text)
        # Plex disabled when plex_client=None (default)
        self.assertIn("⛔ 🎬 <b>Plex</b>: не настроен", text)

    def test_run_diagnostics_reports_disabled_optional_services(self) -> None:
        report = run_diagnostics(
            rutracker_client=None,
            jackett_client=None,
            ds_client=FakeDownloadStation(),
            tracker_service=FakeTrackerService(enabled=False),
            display_timezone=timezone.utc,
        )

        text = format_diagnostics(report)

        self.assertIn("Rutracker</b>: не настроен", text)
        self.assertIn("Jackett</b>: не настроен", text)
        self.assertIn("Public-трекеры</b>: выключены", text)
        self.assertNotIn("Кинопоиск", text)
        self.assertIn("⛔ 🎬 <b>Plex</b>: не настроен", text)

    def test_stale_tracker_cache_reports_ok_not_warning(self) -> None:
        """A stale but non-empty cache is still functional — must show ✅, not ⚠️."""
        report = run_diagnostics(
            rutracker_client=None,
            jackett_client=None,
            ds_client=FakeDownloadStation(),
            tracker_service=FakeTrackerService(trackers=["udp://stale"], fresh_trackers=[]),
            display_timezone=timezone.utc,
        )

        text = format_diagnostics(report)

        self.assertIn("✅ ➕ <b>Public-трекеры</b>: кэш доступен (устарел)", text)
        self.assertIn("Доступно: 1", text)
        self.assertNotIn("⚠️ ➕ <b>Public-трекеры</b>: кэш устарел", text)

    def test_empty_tracker_cache_reports_warning(self) -> None:
        """An empty cache means trackers won't be added until first on-demand load — must show ⚠️."""
        report = run_diagnostics(
            rutracker_client=None,
            jackett_client=None,
            ds_client=FakeDownloadStation(),
            tracker_service=FakeTrackerService(trackers=[], fresh_trackers=[]),
            display_timezone=timezone.utc,
        )

        text = format_diagnostics(report)

        self.assertIn("⚠️ ➕ <b>Public-трекеры</b>: кэш пуст", text)
        self.assertIn("загрузится при первом BT-торренте", text)

    def test_run_diagnostics_keeps_external_errors_readable(self) -> None:
        report = run_diagnostics(
            rutracker_client=FakeRutracker({"login_ok": False, "error": "captcha required"}),
            jackett_client=FakeJackett({"api_ok": False, "error": "API-ключ неверный"}),
            ds_client=FakeDownloadStation(exc=DownloadStationError("DSM API вернул ошибку 119")),
            tracker_service=FakeTrackerService(exc=OSError("disk full")),
            display_timezone=timezone.utc,
        )

        text = format_diagnostics(report)

        self.assertIn("❌ 🧲 <b>Download Station</b>: недоступен", text)
        self.assertIn("DSM API вернул ошибку 119", text)
        self.assertIn("⚠️ 🔎 <b>Rutracker</b>: требуется капча", text)
        self.assertIn("❌ 🌐 <b>Jackett</b>: неверный API-ключ", text)
        self.assertIn("❌ ➕ <b>Public-трекеры</b>: кэш недоступен", text)
        self.assertIn("disk full", text)

    def test_friendly_error_escapes_raw_details(self) -> None:
        text = friendly_error("jackett", "boom <secret>")

        self.assertIn("boom &lt;secret&gt;", text)
        self.assertNotIn("boom <secret>", text)

    # --- Plex diagnostics ---

    def test_plex_disabled_when_client_is_none(self) -> None:
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=None,
        )
        text = format_diagnostics(report)
        self.assertIn("⛔ 🎬 <b>Plex</b>: не настроен", text)

    def test_plex_ok_when_healthy(self) -> None:
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = True
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc,
            plex_client=plex,
            plex_cache_info={"count": 42, "updated_at": "2026-05-14 22:00"},
        )
        text = format_diagnostics(report)
        self.assertIn("✅ 🎬 <b>Plex</b>: подключен", text)
        self.assertIn("Фильмов в библиотеке: 42", text)
        self.assertIn("Кэш обновлён: 2026-05-14 22:00", text)

    def test_plex_error_when_unhealthy(self) -> None:
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = False
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
        )
        text = format_diagnostics(report)
        self.assertIn("❌ 🎬 <b>Plex</b>: не отвечает", text)

    def test_plex_error_on_exception(self) -> None:
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.side_effect = Exception("connection refused")
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
        )
        text = format_diagnostics(report)
        self.assertIn("❌ 🎬 <b>Plex</b>: недоступен", text)


if __name__ == "__main__":
    unittest.main()
