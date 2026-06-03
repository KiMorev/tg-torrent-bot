import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

import requests

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

GPT_TRANSIENT_ERROR_TTL = timedelta(hours=24)
GPT_TRANSIENT_ERROR_TYPES = {"timeout", "network", "rate_limit", "server_error"}
GPT_TERMINAL_ERROR_TYPES = {"quota_exceeded", "auth"}


DIAGNOSTICS_SECTIONS = {
    "downloads": ("🧲 <b>Загрузки</b>", ("Download Station", "Rutracker")),
    "jackett": ("🌐 <b>Jackett</b>", ("Jackett",)),
    "trackers": ("➕ <b>Public-трекеры</b>", ("Public trackers",)),
    "plex": ("🎬 <b>Plex</b>", ("Plex", "Plex deep-link", "Plex webhook")),
    "ai": ("🤖 <b>GPT / Voice</b>", ("Голосовой поиск", "GPT chat")),
}


def _summary(status: str, service_icon: str, service_name: str, message: str) -> str:
    status_icon = STATUS_ICONS.get(status, "ℹ️")
    return f"{status_icon} {service_icon} <b>{service_name}</b>: {message}"


def _raw_detail(raw: str) -> str:
    return f"<blockquote expandable>{html.escape(raw)}</blockquote>"


def _clean_detail(detail: str) -> str:
    return detail.strip()


def _short_datetime(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return html.escape(raw)
    return dt.strftime("%d.%m %H:%M")


def _is_recent_datetime(value: object, ttl: timedelta) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return True

    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return now - dt <= ttl


def _indexer_count(label: str) -> int:
    names = label.strip()
    if not names or names == "нет":
        return 0
    extra = 0
    match = re.search(r"\(\+(\d+)\)$", names)
    if match:
        extra = int(match.group(1))
        names = names[: match.start()].rstrip()
    return len([p for p in names.split(",") if p.strip()]) + extra


def friendly_error(service: str, raw: str, *, include_detail: bool = True) -> str:
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
    if not include_detail:
        return head.replace(" — проверьте <code>JACKETT_API_KEY</code>", " — проверьте настройки")
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


def _jackett_warmup_details(warmup_status: dict | None) -> list[str]:
    if not warmup_status:
        return []
    if not warmup_status.get("enabled"):
        return ["   Прогрев: выключен"]

    state = str(warmup_status.get("last_state") or "waiting")
    state_label = {
        "ok": "работает",
        "waiting": "ждёт первого запуска",
        "skipped": "пропущен: Jackett занят другим запросом",
        "failed": "ошибка",
    }.get(state, html.escape(state))
    line = f"   Прогрев: {state_label}"
    if warmup_status.get("last_ok"):
        line += f" · последний успешный: {_short_datetime(warmup_status['last_ok'])}"
    elif warmup_status.get("last_checked"):
        line += f" · последняя проверка: {_short_datetime(warmup_status['last_checked'])}"
    next_check = _short_datetime(warmup_status.get("next_check"))
    if next_check:
        line += f" · следующая: {next_check}"

    details = [line]
    indexers = warmup_status.get("last_indexers") or []
    if isinstance(indexers, list) and indexers:
        details.append(f"   Последняя пачка прогрева: {html.escape(', '.join(map(str, indexers)))}")
    if warmup_status.get("last_error"):
        details.append(f"   Ошибка прогрева: {html.escape(str(warmup_status['last_error']))}")
    return details


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


def _jackett_diagnostic(jackett_client, warmup_status: dict | None = None) -> ServiceDiagnostic:
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
        _summary("ok", "🌐", "Jackett", f"подключен · {len(indexer_names)} {_plural_ru(len(indexer_names), 'индексер', 'индексера', 'индексеров')}"),
        [f"   Индексеры: {html.escape(indexer_list)}", *_jackett_warmup_details(warmup_status)],
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

    # Note: disk-space info lives in the main /admin «📀 Хранилище» block,
    # not duplicated here. See storage.get_unified_disk_info().
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
            _summary("ok", "➕", "Public-трекеры", "кэш доступен"),
            [*details, "   Кэш старый, но рабочий; обновится при следующей загрузке BT-торрента."],
        )

    return ServiceDiagnostic("Public trackers", "ok", _summary("ok", "➕", "Public-трекеры", "кэш готов"), details)


_PLEX_ERROR_HEADINGS = {
    "auth": "ошибка авторизации — проверьте PLEX_TOKEN",
    "timeout": "таймаут запроса — сервер медленно отвечает или недоступен",
    "network": "не удалось подключиться — проверьте PLEX_URL и сеть",
    "xml": "некорректный ответ — сервер вернул не XML (возможно, страница ошибки)",
    "http": "сервер вернул ошибку HTTP",
    "other": "неизвестная ошибка",
}


def _plex_diagnostic(plex_client, plex_cache_info: dict | None) -> ServiceDiagnostic:
    """Diagnostic for Plex Media Server integration.

    Uses ``plex_cache_info`` health fields (last_error_kind, consecutive_failures,
    last_success_at) when available — gives a richer picture than a one-off
    ``is_healthy`` ping, since refresh runs every 30 min and we know its history.
    """
    if plex_client is None:
        return ServiceDiagnostic("Plex", "disabled", _summary("disabled", "🎬", "Plex", "не настроен — задайте PLEX_URL и PLEX_TOKEN в .env"))

    info = plex_cache_info or {}
    failures = int(info.get("consecutive_failures") or 0)
    last_kind = str(info.get("last_error_kind") or "")
    last_msg = str(info.get("last_error_message") or "")
    last_success = str(info.get("last_success_at") or "")
    last_error = str(info.get("last_error_at") or "")
    movie_count = info.get("count")
    updated_at = str(info.get("updated_at") or "")

    # If we have a recent failure trail from the refresh loop, trust it over a fresh ping.
    if failures > 0 and last_kind:
        heading = _PLEX_ERROR_HEADINGS.get(last_kind, "недоступен")
        details: list[str] = []
        if last_msg:
            details.append(_raw_detail(last_msg))
        details.append(f"   Подряд неудач: {failures}")
        if last_error:
            details.append(f"   Последняя ошибка: {last_error}")
        if last_success:
            details.append(f"   Последний успешный refresh: {last_success}")
        return ServiceDiagnostic(
            "Plex", "error",
            _summary("error", "🎬", "Plex", heading),
            details,
        )

    # No tracked failures yet (or stats not available) — do a live ping.
    try:
        healthy = plex_client.is_healthy()
    except Exception as exc:
        return ServiceDiagnostic(
            "Plex", "error",
            _summary("error", "🎬", "Plex", "недоступен"),
            [_raw_detail(str(exc))],
        )

    if not healthy:
        return ServiceDiagnostic("Plex", "error", _summary("error", "🎬", "Plex", "не отвечает"))

    details = []
    show_count = info.get("show_count")
    if movie_count is not None:
        line = f"   Фильмов в библиотеке: {movie_count}"
        if show_count:
            line += f" · Сериалов: {show_count}"
        details.append(line)
    elif show_count:
        details.append(f"   Сериалов в библиотеке: {show_count}")
    if updated_at:
        details.append(f"   Кэш обновлён: {updated_at}")

    # Unmatched radar: only render the line when at least one entry is unmatched
    # so a healthy library doesn't add noise.
    unmatched_movies = int(info.get("unmatched_movies") or 0)
    unmatched_shows = int(info.get("unmatched_shows") or 0)
    if unmatched_movies or unmatched_shows:
        parts = []
        if unmatched_movies:
            parts.append(f"{unmatched_movies} {_plural_ru(unmatched_movies, 'фильм', 'фильма', 'фильмов')}")
        if unmatched_shows:
            parts.append(f"{unmatched_shows} {_plural_ru(unmatched_shows, 'сериал', 'сериала', 'сериалов')}")
        details.append(f"   Не сматчено: {', '.join(parts)}")

    return ServiceDiagnostic("Plex", "ok", _summary("ok", "🎬", "Plex", "подключен"), details)


def _plex_deeplink_diagnostic(deeplink_base_url: str) -> ServiceDiagnostic:
    """Check that the Plex deep-link redirect page is reachable.

    Every Telegram «▶️ Открыть/Смотреть в Plex» button leads to this URL — if
    the redirect page is down, all Plex-open buttons in the chat become dead
    links. The check uses a short HTTP GET (timeout 5s) and looks for the
    'plex://' marker in the response body to confirm the page is the right one
    (not, e.g., a captive portal or 404 from a misconfigured proxy).

    When PLEX_DEEPLINK_BASE_URL is empty the bot falls back to the public
    `https://app.plex.tv/desktop` (Plex Web), which we don't health-check —
    it's always-up Cloudflare-hosted. We report this as 'disabled' so the
    admin knows there's no custom redirect to monitor.
    """
    name = "Plex deep-link"
    icon = "🔗"
    url = (deeplink_base_url or "").strip()
    if not url:
        return ServiceDiagnostic(
            name, "disabled",
            _summary("disabled", icon, name, "не настроен — используется https://app.plex.tv/desktop"),
            ["   PLEX_DEEPLINK_BASE_URL пуст. Plex-кнопки ведут на публичный Plex Web (всегда доступен)."],
        )

    try:
        resp = requests.get(url, timeout=5, allow_redirects=True)
    except requests.exceptions.Timeout:
        return ServiceDiagnostic(
            name, "error",
            _summary("error", icon, name, "недоступен (таймаут 5с)"),
            [f"   URL: {url}"],
        )
    except requests.exceptions.RequestException as exc:
        return ServiceDiagnostic(
            name, "error",
            _summary("error", icon, name, "недоступен"),
            [f"   URL: {url}", _raw_detail(str(exc))],
        )

    if resp.status_code != 200:
        return ServiceDiagnostic(
            name, "error",
            _summary("error", icon, name, f"HTTP {resp.status_code}"),
            [f"   URL: {url}"],
        )

    # Content sanity-check: our redirect page must contain a plex:// reference
    # in its JS. If it's missing — the file was replaced or we hit a wrong host
    # (e.g. a captive portal, default web-server page).
    body = (resp.text or "")[:4096]
    if "plex://" not in body:
        return ServiceDiagnostic(
            name, "warn",
            _summary("warn", icon, name, "отвечает, но контент не похож на redirect-страницу"),
            [
                f"   URL: {url}",
                "   В ответе нет ссылки `plex://`. Проверьте что отдаётся правильный файл plex.html.",
            ],
        )

    return ServiceDiagnostic(
        name, "ok",
        _summary("ok", icon, name, "доступен"),
        [f"   URL: {url}"],
    )


def _plex_webhook_diagnostic(webhook_info: dict | None) -> ServiceDiagnostic:
    name = "Plex webhook"
    icon = "🔔"
    info = webhook_info or {}
    if not info.get("enabled"):
        return ServiceDiagnostic(
            name, "disabled",
            _summary("disabled", icon, name, "не включён"),
            ["   PLEX_WEBHOOK_ENABLED=false. Plex polling работает как раньше."],
        )

    details = [
        f"   URL: http://<NAS_IP>:{int(info.get('port') or 0)}/plex/webhook?token=***",
    ]
    if info.get("last_received_at"):
        details.append(f"   Последний webhook: {_short_datetime(info.get('last_received_at'))}")
    if info.get("last_accepted_at"):
        details.append(f"   Последний принятый: {_short_datetime(info.get('last_accepted_at'))}")
    if info.get("last_event"):
        details.append(f"   Последнее событие: {html.escape(str(info.get('last_event')))}")
    if info.get("trigger_count") is not None:
        details.append(
            f"   Триггеров: {int(info.get('trigger_count') or 0)} · "
            f"debounce: {int(info.get('debounced_count') or 0)}"
        )
    invalid = int(info.get("invalid_token_count") or 0)
    if invalid:
        details.append(f"   Неверных token: {invalid}")
    if info.get("last_error"):
        details.append(_raw_detail(str(info.get("last_error"))))

    if info.get("listening"):
        return ServiceDiagnostic(
            name, "ok",
            _summary("ok", icon, name, f"слушает порт {int(info.get('port') or 0)}"),
            details,
        )
    return ServiceDiagnostic(
        name, "error",
        _summary("error", icon, name, "включён, но endpoint не слушает"),
        details,
    )


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian plural picker: 1 фильм / 2 фильма / 5 фильмов."""
    n_abs = abs(n) % 100
    if 10 < n_abs < 20:
        return many
    last_digit = n_abs % 10
    if last_digit == 1:
        return one
    if 2 <= last_digit <= 4:
        return few
    return many


def _voice_search_diagnostic(
    *,
    enabled: bool,
    api_key: str,
    usage: dict,
) -> ServiceDiagnostic:
    """Voice-search status: key validity (live ping to /v1/models) + monthly
    usage counter (from our local state, not OpenAI) + last error.

    States:
      disabled — feature off (no key or VOICE_SEARCH_ENABLED=false)
      ok       — key valid, no recent quota/auth error
      warn     — key valid but last_error within last 24h is non-terminal
                 (timeout/network/rate_limit)
      error    — key invalid (401), insufficient_quota seen recently, or
                 OpenAI completely unreachable
    """
    name = "Голосовой поиск"
    icon = "🎙"

    if not enabled or not api_key:
        return ServiceDiagnostic(
            name, "disabled",
            _summary("disabled", icon, name, "не настроен"),
            ["   Установите OPENAI_API_KEY в .env, чтобы включить голосовой поиск."],
        )

    # Lazy import — avoid pulling voice_transcription into diagnostics test runs
    # that don't need it.
    from voice_transcription import check_api_key

    is_valid, key_error = check_api_key(api_key)

    details: list[str] = []

    # Usage block — always rendered when the feature is configured, even if the
    # key check failed. Operator wants to know "what did I spend this month".
    month = str(usage.get("month") or "—")
    count = int(usage.get("request_count") or 0)
    seconds = float(usage.get("total_seconds") or 0.0)
    cost = float(usage.get("estimated_cost_usd") or 0.0)
    details.append(
        f"   За {month}: {count} {_plural_ru(count, 'запрос', 'запроса', 'запросов')} · "
        f"{seconds:.1f}с · ~${cost:.2f}"
    )

    last_request = usage.get("last_request") if isinstance(usage.get("last_request"), dict) else None
    if last_request:
        ts = str(last_request.get("ts") or "—")
        outcome = str(last_request.get("outcome") or "—")
        preview = str(last_request.get("text_preview") or "")
        outcome_label = "✅" if outcome == "ok" else "❌"
        if preview:
            details.append(f"   Последний: {ts} {outcome_label} «{preview}»")
        else:
            details.append(f"   Последний: {ts} {outcome_label}")

    last_error = usage.get("last_error") if isinstance(usage.get("last_error"), dict) else None

    # Determine overall status
    if not is_valid:
        # Key check failed — terminal.
        if key_error == "auth":
            summary_msg = "ключ невалиден"
        elif key_error == "quota_exceeded":
            summary_msg = "превышена квота / нет баланса"
        elif key_error == "timeout":
            summary_msg = "OpenAI недоступен (таймаут)"
        elif key_error == "network":
            summary_msg = "OpenAI недоступен (сеть)"
        else:
            summary_msg = f"ошибка ({key_error})"
        if last_error:
            details.append(
                f"   Последняя ошибка ({last_error.get('ts', '—')}): {last_error.get('type', '—')}"
            )
        return ServiceDiagnostic(
            name, "error",
            _summary("error", icon, name, summary_msg),
            details,
        )

    # Key valid — check whether last_error is recent and severe.
    if last_error:
        err_type = str(last_error.get("type") or "")
        details.append(
            f"   Последняя ошибка ({last_error.get('ts', '—')}): {err_type}"
        )
        if err_type in ("quota_exceeded", "auth"):
            # Terminal types — surface as error even if current ping succeeded
            # (quota can flicker, balance topped up between calls).
            return ServiceDiagnostic(
                name, "error",
                _summary("error", icon, name, f"последняя ошибка: {err_type}"),
                details,
            )

    return ServiceDiagnostic(
        name, "ok",
        _summary("ok", icon, name, "настроен · ключ валиден"),
        details,
    )


_GPT_FEATURE_LABELS = {
    "kp_confidence": "🎯 KP confidence",
    "did_you_mean": "🔎 Did-you-mean",
    "explain_card": "📝 Объяснения карточек",
    "quality_parse": "🏷 Парсинг качества",
    "plex_unmatched": "🧹 Plex unmatched fix",
}


def _gpt_chat_diagnostic(
    *,
    enabled: bool,
    api_key: str,
    usage: dict,
    model: str = "gpt-4o-mini",
) -> ServiceDiagnostic:
    """GPT chat usage status: per-feature monthly call counts + estimated
    cost. Mirrors `_voice_search_diagnostic` for the OpenAI Whisper side.

    Key validity is NOT re-pinged here (voice diagnostic already does that
    against the same OPENAI_API_KEY — pinging twice would just double the
    network noise). Instead we infer health from our own usage record:
      ok       — feature configured, no recent quota/auth error
      error    — last_error is `auth` or `quota_exceeded` (terminal)
      warn     — last_error is transient (timeout/network/rate_limit)
      disabled — feature off or no key
    """
    name = "GPT chat"
    icon = "🧠"

    if not enabled or not api_key:
        return ServiceDiagnostic(
            name, "disabled",
            _summary("disabled", icon, name, "не настроен"),
            ["   Установите OPENAI_API_KEY и GPT_ENABLED=true для GPT-улучшений поиска."],
        )

    details: list[str] = [f"   Модель: <code>{model}</code>"]

    month = str(usage.get("month") or "—")
    features = usage.get("features") if isinstance(usage.get("features"), dict) else {}

    total_calls = 0
    total_cost = 0.0
    total_in_tokens = 0
    total_out_tokens = 0
    total_real_usage_calls = 0
    total_estimate_calls = 0
    total_cost_unknown_calls = 0
    unknown_models: set[str] = set()
    for f_data in features.values():
        if not isinstance(f_data, dict):
            continue
        total_calls += int(f_data.get("calls") or 0)
        total_cost += float(f_data.get("estimated_cost_usd") or 0.0)
        total_in_tokens += int(f_data.get("input_tokens") or 0)
        total_out_tokens += int(f_data.get("output_tokens") or 0)
        total_real_usage_calls += int(f_data.get("real_usage_calls") or 0)
        total_estimate_calls += int(f_data.get("estimate_calls") or 0)
        total_cost_unknown_calls += int(f_data.get("cost_unknown_calls") or 0)
        for m in (f_data.get("unknown_models") or []):
            if isinstance(m, str) and m:
                unknown_models.add(m)

    details.append(
        f"   За {month}: {total_calls} "
        f"{_plural_ru(total_calls, 'запрос', 'запроса', 'запросов')} · "
        f"~${total_cost:.3f} (in {total_in_tokens}, out {total_out_tokens} ток.)"
    )
    # Show real-usage vs estimate ratio — operators want to know when /admin
    # numbers come from API-reported tokens vs hardcoded fallbacks.
    if total_calls > 0:
        details.append(
            f"   Учёт: {total_real_usage_calls} реальных · "
            f"{total_estimate_calls} оценочных"
        )
    if total_cost_unknown_calls > 0:
        models_str = ", ".join(sorted(unknown_models)) or "?"
        details.append(
            f"   ⚠️ Cost unknown для {total_cost_unknown_calls} вызовов "
            f"(модели: {models_str}) — токены посчитаны, доллары нет"
        )

    # Per-feature breakdown — only show features that actually fired.
    for feature_key, feature_data in sorted(features.items()):
        if not isinstance(feature_data, dict):
            continue
        calls = int(feature_data.get("calls") or 0)
        if calls == 0:
            continue
        cost = float(feature_data.get("estimated_cost_usd") or 0.0)
        label = _GPT_FEATURE_LABELS.get(feature_key, feature_key)
        details.append(
            f"     • {label}: {calls} "
            f"{_plural_ru(calls, 'вызов', 'вызова', 'вызовов')} · ~${cost:.3f}"
        )

    last_error = usage.get("last_error") if isinstance(usage.get("last_error"), dict) else None
    if last_error:
        ts = str(last_error.get("ts") or "—")
        err_type = str(last_error.get("type") or "—")
        feature = str(last_error.get("feature") or "—")
        details.append(f"   Последняя ошибка ({ts}, {feature}): {err_type}")

        if err_type in GPT_TERMINAL_ERROR_TYPES:
            return ServiceDiagnostic(
                name, "error",
                _summary("error", icon, name, f"последняя ошибка: {err_type}"),
                details,
            )
        if err_type in GPT_TRANSIENT_ERROR_TYPES and _is_recent_datetime(ts, GPT_TRANSIENT_ERROR_TTL):
            return ServiceDiagnostic(
                name, "warn",
                _summary("warn", icon, name, f"временная ошибка: {err_type}"),
                details,
            )
        if err_type in GPT_TRANSIENT_ERROR_TYPES:
            details.append("   Временная ошибка старше 24 ч: статус снова зелёный.")

    return ServiceDiagnostic(
        name, "ok",
        _summary("ok", icon, name, "настроен · GPT-улучшения активны"),
        details,
    )


def run_diagnostics(
    *,
    rutracker_client,
    jackett_client,
    ds_client,
    tracker_service,
    display_timezone: tzinfo,
    jackett_warmup_status: dict | None = None,
    plex_client=None,
    plex_cache_info: dict | None = None,
    plex_deeplink_base_url: str = "",
    plex_webhook_info: dict | None = None,
    voice_search_enabled: bool = False,
    openai_api_key: str = "",
    voice_usage: dict | None = None,
    gpt_enabled: bool = False,
    gpt_model: str = "gpt-4o-mini",
    gpt_usage: dict | None = None,
) -> DiagnosticsReport:
    return DiagnosticsReport(
        [
            _download_station_diagnostic(ds_client),
            _rutracker_diagnostic(rutracker_client),
            _jackett_diagnostic(jackett_client, jackett_warmup_status),
            _public_trackers_diagnostic(tracker_service, display_timezone),
            _plex_diagnostic(plex_client, plex_cache_info),
            _plex_deeplink_diagnostic(plex_deeplink_base_url),
            _plex_webhook_diagnostic(plex_webhook_info),
            _voice_search_diagnostic(
                enabled=voice_search_enabled,
                api_key=openai_api_key,
                usage=voice_usage or {},
            ),
            _gpt_chat_diagnostic(
                enabled=gpt_enabled,
                api_key=openai_api_key,
                usage=gpt_usage or {},
                model=gpt_model,
            ),
        ]
    )


def format_diagnostics(report: DiagnosticsReport) -> str:
    lines = ["🔍 <b>Диагностика</b>", ""]
    issues = [s for s in report.services if s.status in {"warn", "error"}]
    if issues:
        lines.append(f"⚠️ Требует внимания: {len(issues)}")
        for service in issues[:4]:
            lines.append(f"• {service.summary}")
        if len(issues) > 4:
            lines.append(f"• …ещё {len(issues) - 4}")
    else:
        lines.append("✅ Критичных проблем не видно.")

    lines.append("")
    lines.append("<b>Сервисы</b>")
    for service in report.services:
        lines.append(_format_service_line(service))
    lines.append("")
    lines.append("Подробности открываются кнопками ниже.")
    return "\n".join(lines)


def format_diagnostics_section(report: DiagnosticsReport, section: str) -> str:
    title, service_names = DIAGNOSTICS_SECTIONS.get(section, DIAGNOSTICS_SECTIONS["downloads"])
    services = [s for s in report.services if s.name in service_names]
    lines = [title]
    for service in services:
        lines.append("")
        lines.append(service.summary)
        if service.details:
            lines.extend(service.details)
        else:
            lines.append("   Подробностей нет.")
    return "\n".join(lines)


def _format_service_line(service: ServiceDiagnostic) -> str:
    suffixes: list[str] = []
    for detail in service.details:
        text = _clean_detail(detail)
        if text.startswith("<blockquote"):
            continue
        if text.startswith("Задач: "):
            suffixes.append(f"задач: {text.removeprefix('Задач: ').strip()}")
        elif text.startswith("Индексеры: "):
            count = _indexer_count(text.removeprefix("Индексеры: "))
            if "индексер" not in service.summary:
                suffixes.append(f"{count} {_plural_ru(count, 'индексер', 'индексера', 'индексеров')}")
        elif text.startswith("Прогрев: "):
            state = text.removeprefix("Прогрев: ").split(" · ", 1)[0].strip()
            suffixes.append(f"прогрев: {state}")
        elif text.startswith("Доступно: "):
            suffixes.append(f"доступно: {text.removeprefix('Доступно: ').strip()}")
        elif text.startswith("Фильмов в библиотеке: "):
            counts = text.removeprefix("Фильмов в библиотеке: ").strip()
            if " · Сериалов: " in counts:
                suffixes.append(counts.replace(" · Сериалов: ", " фильмов · сериалов: "))
            else:
                suffixes.append(f"{counts} фильмов")
        elif text.startswith("Сериалов в библиотеке: "):
            suffixes.append(f"сериалов: {text.removeprefix('Сериалов в библиотеке: ').strip()}")
        elif text.startswith("Не сматчено: "):
            suffixes.append(text.lower())
    if not suffixes:
        return service.summary
    return f"{service.summary} · {' · '.join(suffixes[:2])}"
