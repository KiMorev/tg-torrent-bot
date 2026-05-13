import html
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from download_station import DownloadStationError


@dataclass(frozen=True)
class ServiceDiagnostic:
    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiagnosticsReport:
    services: list[ServiceDiagnostic]


STATUS_ICONS = {
    "ok": "✅",
    "warn": "⚠️",
    "error": "❌",
    "disabled": "⛔",
}


def _summary(status: str, service_icon: str, service_name: str, message: str) -> str:
    status_icon = STATUS_ICONS.get(status, "ℹ️")
    return f"{status_icon} {service_icon} <b>{service_name}</b>: {message}"


def _raw_detail(raw: str) -> str:
    return f"<blockquote expandable>{html.escape(raw)}</blockquote>"


def friendly_error(service: str, raw: str) -> str:
    """Return an HTML-formatted user-friendly error string."""
    rl = raw.lower()
    if service == "rutracker":
        name = "<b>Rutracker</b>"
        if "запускается" in rl:
            return f"⏱ {name}: ещё запускается"
        if "captcha" in rl or "капч" in rl:
            head = f"🤖 {name}: требуется капча"
        elif "авторизация не удалась" in rl or "username" in rl or "password" in rl:
            head = f"🔑 {name}: ошибка авторизации — проверьте настройки"
        else:
            head = f"❌ {name}: недоступен"
    else:
        name = "🌐 <b>Jackett</b>"
        if "запускается" in rl:
            return f"{name}: ⏱ ещё запускается — подождите ~1 мин"
        if "неверный" in rl or "api-ключ" in rl:
            return f"{name}: 🔑 неверный API-ключ — проверьте настройки"
        if "страницу входа" in rl or "не принят" in rl:
            head = f"{name}: 🔑 API ключ не принят — проверьте <code>JACKETT_API_KEY</code>"
        else:
            head = f"{name}: ❌ недоступен"
    return f"{head}\n<blockquote expandable>{html.escape(raw)}</blockquote>"


def _rutracker_error(raw: str) -> ServiceDiagnostic:
    rl = raw.lower()
    if "запускается" in rl:
        return ServiceDiagnostic("Rutracker", "warn", _summary("warn", "🔎", "Rutracker", "ещё запускается"))
    if "captcha" in rl or "капч" in rl:
        return ServiceDiagnostic("Rutracker", "warn", _summary("warn", "🔎", "Rutracker", "требуется капча"))
    if "авторизация не удалась" in rl or "username" in rl or "password" in rl:
        return ServiceDiagnostic(
            "Rutracker",
            "error",
            _summary("error", "🔎", "Rutracker", "ошибка авторизации — проверьте настройки"),
        )
    return ServiceDiagnostic("Rutracker", "error", _summary("error", "🔎", "Rutracker", "недоступен"), [_raw_detail(raw)])


def _jackett_error(raw: str) -> ServiceDiagnostic:
    rl = raw.lower()
    if "запускается" in rl:
        return ServiceDiagnostic(
            "Jackett",
            "warn",
            _summary("warn", "🌐", "Jackett", "ещё запускается — подождите ~1 мин"),
        )
    if "неверный" in rl or "api-ключ" in rl:
        return ServiceDiagnostic(
            "Jackett",
            "error",
            _summary("error", "🌐", "Jackett", "неверный API-ключ — проверьте настройки"),
        )
    if "страницу входа" in rl or "не принят" in rl:
        return ServiceDiagnostic(
            "Jackett",
            "error",
            _summary("error", "🌐", "Jackett", "API ключ не принят — проверьте JACKETT_API_KEY"),
        )
    return ServiceDiagnostic("Jackett", "error", _summary("error", "🌐", "Jackett", "недоступен"), [_raw_detail(raw)])


def _rutracker_diagnostic(rutracker_client) -> ServiceDiagnostic:
    if rutracker_client is None:
        return ServiceDiagnostic(
            name="Rutracker",
            status="disabled",
            summary=_summary("disabled", "🔎", "Rutracker", "не настроен — задайте RUTRACKER_USERNAME в .env"),
        )

    try:
        status = rutracker_client.diagnose()
    except Exception as exc:
        return _rutracker_error(str(exc))

    if status.get("login_ok"):
        return ServiceDiagnostic("Rutracker", "ok", _summary("ok", "🔎", "Rutracker", "подключен"))

    return _rutracker_error(str(status.get("error", "Неизвестная ошибка")))


def _jackett_diagnostic(jackett_client) -> ServiceDiagnostic:
    if jackett_client is None:
        return ServiceDiagnostic("Jackett", "disabled", _summary("disabled", "🌐", "Jackett", "не настроен"))

    try:
        diag = jackett_client.test_connection()
    except Exception as exc:
        return _jackett_error(str(exc))

    if not diag.get("api_ok"):
        error = diag.get("error", "Неизвестная ошибка")
        return _jackett_error(str(error))

    indexer_names = [i["name"] if isinstance(i, dict) else i for i in diag.get("indexers", [])]
    indexer_list = ", ".join(indexer_names[:10]) or "нет"
    if len(indexer_names) > 10:
        indexer_list += f" (+{len(indexer_names) - 10})"

    return ServiceDiagnostic(
        "Jackett",
        "ok",
        _summary("ok", "🌐", "Jackett", "подключен"),
        [f"   Индексеры: {html.escape(indexer_list)}"],
    )


def _download_station_diagnostic(ds_client) -> ServiceDiagnostic:
    try:
        tasks = ds_client.list_tasks()
    except DownloadStationError as exc:
        return ServiceDiagnostic(
            "Download Station",
            "error",
            _summary("error", "🧲", "Download Station", "недоступен"),
            [_raw_detail(str(exc))],
        )
    except Exception as exc:
        return ServiceDiagnostic(
            "Download Station",
            "error",
            _summary("error", "🧲", "Download Station", "проверка не удалась"),
            [_raw_detail(str(exc))],
        )

    return ServiceDiagnostic(
        "Download Station",
        "ok",
        _summary("ok", "🧲", "Download Station", "подключен"),
        [f"   Задач: {len(tasks)}"],
    )


def _format_cache_time(cache_time: float | None, display_timezone: tzinfo) -> str:
    if cache_time is None:
        return ""
    return datetime.fromtimestamp(cache_time, display_timezone).strftime("%d.%m.%Y %H:%M")


def _public_trackers_diagnostic(tracker_service, display_timezone: tzinfo) -> ServiceDiagnostic:
    if tracker_service is None or not tracker_service.public_trackers_enabled():
        return ServiceDiagnostic("Public trackers", "disabled", _summary("disabled", "➕", "Public-трекеры", "выключены"))

    try:
        trackers, cache_time = tracker_service.read_cache(require_fresh=True)
        cache_is_stale = False
        if not trackers:
            trackers, cache_time = tracker_service.read_cache(require_fresh=False)
            cache_is_stale = bool(trackers)
    except Exception as exc:
        return ServiceDiagnostic(
            "Public trackers",
            "error",
            _summary("error", "➕", "Public-трекеры", "кэш недоступен"),
            [_raw_detail(str(exc))],
        )

    if not trackers:
        return ServiceDiagnostic(
            "Public trackers",
            "warn",
            _summary("warn", "➕", "Public-трекеры", "кэш пуст — загрузится при первом BT-торренте"),
        )

    details = [f"   Доступно: {len(trackers)}"]
    cache_time_text = _format_cache_time(cache_time, display_timezone)
    if cache_time_text:
        details.append(f"   Обновлён: {cache_time_text}")

    if cache_is_stale:
        # Stale ≠ broken: the list is still fully functional, trackers change rarely.
        # Show ok so the admin panel stays green in normal on-demand-refresh usage.
        return ServiceDiagnostic(
            "Public trackers",
            "ok",
            _summary("ok", "➕", "Public-трекеры", "кэш доступен (устарел)"),
            details,
        )

    return ServiceDiagnostic("Public trackers", "ok", _summary("ok", "➕", "Public-трекеры", "кэш готов"), details)


def run_diagnostics(
    *,
    rutracker_client,
    jackett_client,
    ds_client,
    tracker_service,
    display_timezone: tzinfo,
) -> DiagnosticsReport:
    return DiagnosticsReport(
        [
            _download_station_diagnostic(ds_client),
            _rutracker_diagnostic(rutracker_client),
            _jackett_diagnostic(jackett_client),
            _public_trackers_diagnostic(tracker_service, display_timezone),
        ]
    )


def format_diagnostics(report: DiagnosticsReport) -> str:
    lines = ["🔍 <b>Диагностика бота</b>"]
    for service in report.services:
        lines.append("")
        lines.append(service.summary)
        lines.extend(service.details)
    return "\n".join(lines)
