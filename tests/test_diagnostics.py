import unittest
from datetime import datetime, timedelta, timezone

import requests

from diagnostics import format_diagnostics, format_diagnostics_section, friendly_error, run_diagnostics
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
        self.assertIn("задач: 2", text)
        self.assertIn("✅ 🔎 <b>Rutracker</b>: подключен", text)
        self.assertIn("✅ 🌐 <b>Jackett</b>: подключен · 2 индексера", text)
        self.assertIn("✅ ➕ <b>Public-трекеры</b>: кэш готов", text)
        self.assertIn("доступно: 2", text)
        self.assertNotIn("Кинопоиск", text)
        # Plex disabled when plex_client=None (default)
        self.assertIn("⛔ 🎬 <b>Plex</b>: не настроен", text)

        jackett_text = format_diagnostics_section(report, "jackett")
        trackers_text = format_diagnostics_section(report, "trackers")
        self.assertIn("Индексеры: Rutracker, NNMClub", jackett_text)
        self.assertIn("Доступно: 2", trackers_text)

    def test_jackett_diagnostics_include_warmup_status(self) -> None:
        report = run_diagnostics(
            rutracker_client=FakeRutracker(),
            jackett_client=FakeJackett(),
            jackett_warmup_status={
                "enabled": True,
                "last_state": "ok",
                "last_ok": "2026-05-28 12:00:00",
                "next_check": "2026-05-28 12:15:00",
                "last_indexers": ["rutracker", "kinozal"],
            },
            ds_client=FakeDownloadStation(),
            tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc,
        )

        text = format_diagnostics_section(report, "jackett")

        self.assertIn("Прогрев: работает", text)
        self.assertIn("последний успешный: 28.05 12:00", text)
        self.assertIn("Последняя пачка прогрева: rutracker, kinozal", text)

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

        self.assertIn("✅ ➕ <b>Public-трекеры</b>: кэш доступен", text)
        self.assertIn("доступно: 1", text)
        self.assertNotIn("⚠️ ➕ <b>Public-трекеры</b>: кэш устарел", text)

        details = format_diagnostics_section(report, "trackers")
        self.assertIn("Доступно: 1", details)
        self.assertIn("Кэш старый, но рабочий", details)

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
        downloads_text = format_diagnostics_section(report, "downloads")
        trackers_text = format_diagnostics_section(report, "trackers")

        self.assertIn("❌ 🧲 <b>Download Station</b>: недоступен", text)
        self.assertIn("DSM API вернул ошибку 119", downloads_text)
        self.assertIn("⚠️ 🔎 <b>Rutracker</b>: требуется капча", text)
        self.assertIn("❌ 🌐 <b>Jackett</b>: неверный API-ключ", text)
        self.assertIn("❌ ➕ <b>Public-трекеры</b>: кэш недоступен", text)
        self.assertIn("disk full", trackers_text)

    def test_friendly_error_escapes_raw_details(self) -> None:
        text = friendly_error("jackett", "boom <secret>")

        self.assertIn("boom &lt;secret&gt;", text)
        self.assertNotIn("boom <secret>", text)

    def test_friendly_error_can_hide_raw_details(self) -> None:
        text = friendly_error("jackett", "boom <secret>", include_detail=False)

        self.assertIn("Jackett", text)
        self.assertNotIn("boom", text)

    def test_friendly_error_hides_env_var_from_user_text(self) -> None:
        text = friendly_error("jackett", "страницу входа не принят", include_detail=False)

        self.assertIn("проверьте настройки", text)
        self.assertNotIn("JACKETT_API_KEY", text)

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
        plex_text = format_diagnostics_section(report, "plex")
        self.assertIn("✅ 🎬 <b>Plex</b>: подключен", text)
        self.assertIn("42 фильмов", text)
        self.assertIn("Фильмов в библиотеке: 42", plex_text)
        self.assertIn("Кэш обновлён: 2026-05-14 22:00", plex_text)

    def test_plex_webhook_detail_escapes_placeholder_url_for_html(self) -> None:
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc,
            plex_webhook_info={"enabled": True, "listening": True, "port": 8099},
        )

        plex_text = format_diagnostics_section(report, "plex")

        self.assertIn("http://&lt;NAS_IP&gt;:8099/plex/webhook?token=***", plex_text)
        self.assertNotIn("http://<NAS_IP>", plex_text)

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

    # --- Refresh-loop-based error reporting (Phase 1.4) ---

    def test_plex_shows_auth_error_from_refresh_state(self) -> None:
        """When the refresh loop has been failing with auth errors, /admin must show
        the auth-specific message — not run a fresh is_healthy ping."""
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = True  # ping would succeed, but state says otherwise
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
            plex_cache_info={
                "count": 0,
                "updated_at": "",
                "last_error_kind": "auth",
                "last_error_message": "Invalid Plex token (HTTP 401)",
                "last_error_at": "2026-05-15 10:00",
                "last_success_at": "2026-05-14 22:00",
                "consecutive_failures": 5,
            },
        )
        text = format_diagnostics(report)
        self.assertIn("ошибка авторизации", text)
        self.assertIn("PLEX_TOKEN", text)
        # is_healthy should NOT have been called — state takes priority
        plex.is_healthy.assert_not_called()

    def test_plex_shows_timeout_error_kind(self) -> None:
        from unittest.mock import MagicMock
        plex = MagicMock()
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
            plex_cache_info={
                "count": 0, "last_error_kind": "timeout",
                "last_error_message": "Timeout connecting to /library",
                "consecutive_failures": 3,
            },
        )
        text = format_diagnostics(report)
        self.assertIn("таймаут запроса", text)

    def test_plex_ok_when_no_failures_in_state(self) -> None:
        """When consecutive_failures == 0, fall back to live ping (existing behaviour)."""
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = True
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
            plex_cache_info={"count": 100, "updated_at": "2026-05-15 12:00",
                             "consecutive_failures": 0},
        )
        text = format_diagnostics(report)
        self.assertIn("✅ 🎬 <b>Plex</b>: подключен", text)

    def test_plex_ok_shows_show_count_alongside_movies(self) -> None:
        """When the TV-shows cache is populated, /admin shows a 'Сериалов: M' counter."""
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = True
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
            plex_cache_info={
                "count": 100,
                "show_count": 25,
                "updated_at": "2026-05-15 12:00",
                "consecutive_failures": 0,
            },
        )
        text = format_diagnostics(report)
        plex_text = format_diagnostics_section(report, "plex")
        self.assertIn("100 фильмов", text)
        self.assertIn("сериалов: 25", text)
        self.assertIn("Фильмов в библиотеке: 100", plex_text)
        self.assertIn("Сериалов: 25", plex_text)

    def test_plex_ok_shows_unmatched_line_when_any_unmatched(self) -> None:
        """When at least one Plex entry is unmatched, /admin renders a 'Не сматчено' line."""
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = True
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
            plex_cache_info={
                "count": 100, "show_count": 25,
                "consecutive_failures": 0,
                "unmatched_movies": 3,
                "unmatched_shows": 1,
            },
        )
        text = format_diagnostics(report)
        plex_text = format_diagnostics_section(report, "plex")
        self.assertIn("не сматчено", text)
        self.assertIn("Не сматчено", plex_text)
        self.assertIn("3 фильма", plex_text)
        self.assertIn("1 сериал", plex_text)

    def test_plex_ok_hides_unmatched_line_when_all_matched(self) -> None:
        """Don't add noise when there's nothing to report — all matched."""
        from unittest.mock import MagicMock
        plex = MagicMock()
        plex.is_healthy.return_value = True
        report = run_diagnostics(
            rutracker_client=None, jackett_client=None,
            ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
            display_timezone=timezone.utc, plex_client=plex,
            plex_cache_info={
                "count": 100, "show_count": 25,
                "consecutive_failures": 0,
                "unmatched_movies": 0,
                "unmatched_shows": 0,
            },
        )
        text = format_diagnostics(report)
        self.assertNotIn("Не сматчено", text)


class PlexDeeplinkDiagnosticTests(unittest.TestCase):
    """Health check for the Plex deep-link redirect page (PLEX_DEEPLINK_BASE_URL).

    If this URL goes down, ALL «▶️ Открыть/Смотреть в Plex» buttons in the bot
    become dead links — so it must be in /admin diagnostics.
    """

    def _run(self, deeplink_url: str, *, get_mock=None):
        from unittest.mock import patch
        with patch("diagnostics.requests.get", get_mock or (lambda *a, **kw: None)):
            return run_diagnostics(
                rutracker_client=None, jackett_client=None,
                ds_client=FakeDownloadStation(), tracker_service=FakeTrackerService(),
                display_timezone=timezone.utc,
                plex_deeplink_base_url=deeplink_url,
            )

    def _deeplink_service(self, report):
        return next(s for s in report.services if s.name == "Plex deep-link")

    def test_disabled_when_url_empty(self):
        report = self._run("")
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "disabled")
        self.assertIn("app.plex.tv/desktop", svc.summary)

    def test_disabled_when_url_whitespace_only(self):
        report = self._run("   ")
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "disabled")

    def test_ok_when_url_returns_200_with_plex_marker(self):
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=200, text='<html>...location.href = "plex://..."...</html>')
        report = self._run("https://example.com/plex.html",
                           get_mock=MagicMock(return_value=resp))
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "ok")
        self.assertIn("доступен", svc.summary)

    def test_warn_when_200_but_no_plex_marker(self):
        """Captive portal / wrong page / placeholder hits 200 but lacks our marker."""
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=200, text="<html><body>Default Apache page</body></html>")
        report = self._run("https://example.com/plex.html",
                           get_mock=MagicMock(return_value=resp))
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "warn")
        self.assertIn("контент не похож", svc.summary)

    def test_error_on_4xx(self):
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=404, text="not found")
        report = self._run("https://example.com/plex.html",
                           get_mock=MagicMock(return_value=resp))
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("404", svc.summary)

    def test_error_on_5xx(self):
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=502, text="bad gateway")
        report = self._run("https://example.com/plex.html",
                           get_mock=MagicMock(return_value=resp))
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("502", svc.summary)

    def test_error_on_timeout(self):
        from unittest.mock import MagicMock
        get = MagicMock(side_effect=requests.exceptions.Timeout("read timed out"))
        report = self._run("https://example.com/plex.html", get_mock=get)
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("таймаут", svc.summary)

    def test_error_on_connection_error(self):
        from unittest.mock import MagicMock
        get = MagicMock(side_effect=requests.exceptions.ConnectionError("DNS lookup failed"))
        report = self._run("https://example.com/plex.html", get_mock=get)
        svc = self._deeplink_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("недоступен", svc.summary)


class VoiceSearchDiagnosticTests(unittest.TestCase):
    """Voice-search block: key validity, usage counters, last_error surfacing."""

    def _voice_service(self, report):
        return next(s for s in report.services if s.name == "Голосовой поиск")

    def _run(
        self,
        *,
        enabled: bool,
        api_key: str,
        usage: dict | None = None,
        check_result: tuple[bool, str | None] = (True, None),
    ):
        with unittest.mock.patch(
            "voice_transcription.check_api_key",
            return_value=check_result,
        ):
            return run_diagnostics(
                rutracker_client=None,
                jackett_client=None,
                ds_client=FakeDownloadStation([]),
                tracker_service=FakeTrackerService(trackers=[]),
                display_timezone=timezone.utc,
                voice_search_enabled=enabled,
                openai_api_key=api_key,
                voice_usage=usage or {},
            )

    def test_disabled_when_feature_off(self):
        report = self._run(enabled=False, api_key="")
        svc = self._voice_service(report)
        self.assertEqual(svc.status, "disabled")
        self.assertIn("не настроен", svc.summary)

    def test_disabled_when_key_empty(self):
        report = self._run(enabled=True, api_key="")
        svc = self._voice_service(report)
        self.assertEqual(svc.status, "disabled")

    def test_ok_when_key_valid_and_no_recent_error(self):
        usage = {
            "month": "2026-05",
            "request_count": 12,
            "total_seconds": 78.5,
            "estimated_cost_usd": 0.078,
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage,
                           check_result=(True, None))
        svc = self._voice_service(report)
        self.assertEqual(svc.status, "ok")
        self.assertIn("2026-05", " ".join(svc.details))
        self.assertIn("12 ", " ".join(svc.details))  # request count

    def test_error_when_key_invalid(self):
        report = self._run(enabled=True, api_key="sk-bad",
                           check_result=(False, "auth"))
        svc = self._voice_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("ключ невалиден", svc.summary)

    def test_error_when_quota_exceeded_from_ping(self):
        report = self._run(enabled=True, api_key="sk-test",
                           check_result=(False, "quota_exceeded"))
        svc = self._voice_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("квота", svc.summary)

    def test_error_when_last_error_is_quota_even_if_key_valid(self):
        """Quota can flicker — key works now, but last actual call hit the cap.
        We still surface as error so operator tops up the balance."""
        usage = {
            "month": "2026-05",
            "request_count": 142,
            "last_error": {"ts": "2026-05-22T10:00:00", "type": "quota_exceeded"},
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage,
                           check_result=(True, None))
        svc = self._voice_service(report)
        self.assertEqual(svc.status, "error")

    def test_renders_last_request_when_present(self):
        usage = {
            "month": "2026-05",
            "request_count": 1,
            "last_request": {
                "ts": "2026-05-22T14:18:00",
                "outcome": "ok",
                "text_preview": "Дюна часть вторая",
            },
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage,
                           check_result=(True, None))
        svc = self._voice_service(report)
        joined = " ".join(svc.details)
        self.assertIn("Дюна часть вторая", joined)


class GptChatDiagnosticTests(unittest.TestCase):
    """GPT chat usage block: per-feature monthly counts, cost, last_error."""

    def _gpt_service(self, report):
        return next(s for s in report.services if s.name == "GPT chat")

    def _run(
        self,
        *,
        enabled: bool,
        api_key: str,
        usage: dict | None = None,
        model: str = "gpt-4o-mini",
    ):
        return run_diagnostics(
            rutracker_client=None,
            jackett_client=None,
            ds_client=FakeDownloadStation([]),
            tracker_service=FakeTrackerService(trackers=[]),
            display_timezone=timezone.utc,
            gpt_enabled=enabled,
            openai_api_key=api_key,
            gpt_usage=usage or {},
            gpt_model=model,
        )

    def test_disabled_when_feature_off(self):
        report = self._run(enabled=False, api_key="")
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "disabled")

    def test_disabled_when_key_empty(self):
        report = self._run(enabled=True, api_key="")
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "disabled")

    def test_ok_with_no_calls_yet(self):
        report = self._run(enabled=True, api_key="sk-test", usage={"month": "2026-05"})
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "ok")
        joined = " ".join(svc.details)
        self.assertIn("0", joined)  # 0 requests this month
        self.assertIn("gpt-4o-mini", joined)

    def test_renders_per_feature_breakdown(self):
        usage = {
            "month": "2026-05",
            "features": {
                "kp_confidence": {
                    "calls": 23, "input_tokens": 4600, "output_tokens": 690,
                    "estimated_cost_usd": 0.00112,
                },
                "did_you_mean": {
                    "calls": 5, "input_tokens": 500, "output_tokens": 400,
                    "estimated_cost_usd": 0.00033,
                },
            },
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage)
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "ok")
        joined = " ".join(svc.details)
        # Total monthly aggregate
        self.assertIn("28", joined)  # 23 + 5 = 28
        # Per-feature lines
        self.assertIn("KP confidence", joined)
        self.assertIn("Did-you-mean", joined)
        self.assertIn("23", joined)  # KP calls
        self.assertIn("5", joined)   # did-you-mean calls

    def test_omits_features_with_zero_calls(self):
        """Per-feature breakdown should hide features that never fired
        (avoid clutter as we add more features later)."""
        usage = {
            "month": "2026-05",
            "features": {
                "kp_confidence": {"calls": 10, "input_tokens": 200, "output_tokens": 50, "estimated_cost_usd": 0.0001},
                "explain_card": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
            },
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage)
        svc = self._gpt_service(report)
        joined = " ".join(svc.details)
        self.assertIn("KP confidence", joined)
        self.assertNotIn("Объяснения карточек", joined)

    def test_error_on_quota_exceeded(self):
        usage = {
            "month": "2026-05",
            "features": {"did_you_mean": {"calls": 14, "input_tokens": 1000, "output_tokens": 800, "estimated_cost_usd": 0.001}},
            "last_error": {"ts": "2026-05-22T10:00", "feature": "did_you_mean", "type": "quota_exceeded"},
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage)
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "error")
        self.assertIn("quota_exceeded", svc.summary)

    def test_warn_on_transient_error(self):
        usage = {
            "month": "2026-05",
            "features": {"kp_confidence": {"calls": 5, "input_tokens": 100, "output_tokens": 30, "estimated_cost_usd": 0.00005}},
            "last_error": {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "feature": "kp_confidence",
                "type": "timeout",
            },
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage)
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "warn")

    def test_stale_transient_error_returns_ok(self):
        usage = {
            "month": "2026-05",
            "features": {"kp_confidence": {"calls": 5, "input_tokens": 100, "output_tokens": 30, "estimated_cost_usd": 0.00005}},
            "last_error": {
                "ts": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"),
                "feature": "kp_confidence",
                "type": "network",
            },
        }
        report = self._run(enabled=True, api_key="sk-test", usage=usage)
        svc = self._gpt_service(report)
        self.assertEqual(svc.status, "ok")
        self.assertIn("статус снова зелёный", " ".join(svc.details))


if __name__ == "__main__":
    unittest.main()
