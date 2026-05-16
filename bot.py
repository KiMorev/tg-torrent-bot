import asyncio
import html as html_module
import json
import logging
import os
import random
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from access_control import (
    access_request_user_label,
    all_allowed_chat_ids,
    is_admin_chat,
    is_allowed_chat,
)
from app_context import build_app_context
from config import load_settings, parse_chat_ids
from diagnostics import friendly_error as _friendly_error, format_diagnostics, run_diagnostics
from download_station import DownloadStationError
from formatters import (
    _extract_season_from_query,
    _extract_series_base_query,
    _filter_by_season,
    _format_hours,
    _format_size,
    _format_sub_title,
    _magnet_wait_bar,
    _magnet_wait_text,
    _normalize_season_in_query,
    _parse_episode_info,
    _progress_percent,
    _quality_to_query_suffix,
    _seasons_available_in_results,
    _score_result,
    _short_title,
    _status_label,
    _tracker_abbr,
)
from keyboards import (
    ACCESS_CALLBACK_PREFIX,
    ADMIN_CALLBACK_PREFIX,
    JACKETT_SELECT_PREFIX,
    SEARCH_CALLBACK_PREFIX,
    TASK_CALLBACK_PREFIX,
    TASK_LIST_PAGE_SIZE,
    TASK_LIST_SCOPE_ALL,
    TASK_LIST_SCOPE_DEFAULT,
    TASK_LIST_SCOPE_MY,
    _SRCH_DEFAULT_SETTINGS,
    _SRCH_QUALITY_OPTIONS,
    _admin_diagnostics_keyboard,
    _admin_kp_cache_cleared_keyboard,
    _admin_kp_cache_confirm_keyboard,
    _admin_kp_force_refresh_keyboard,
    _admin_panel_keyboard,
    _access_approval_keyboard,
    _access_callback,
    _delete_confirm_keyboard,
    _delete_finished_confirm_keyboard,
    _download_list_keyboard,
    _final_notification_keyboard,
    _finished_task_ids,
    _plex_confirm_keyboard,
    _jackett_select_keyboard,
    _new_task_keyboard,
    _search_advanced_keyboard,
    _search_after_add_keyboard,
    _no_quality_keyboard,
    _search_error_keyboard,
    _search_options_keyboard,
    _search_results_keyboard,
    tracker_selection_label,
    _season_select_keyboard,
    _season_back_to_picker_keyboard,
    SEARCH_PAGE_SIZE,
    SUB_CALLBACK_PREFIX,
    _task_callback,
    _task_keyboard,
    _task_reply_markup,
    _tasks_keyboard,
    users_keyboard,
    movie_trackers_keyboard,
)
from jackett import JackettError, JackettMagnetRedirect, JackettResult
from jackett_subscriptions import (
    JACKETT_SUBSCRIPTION_SCHEMA,
    apply_jackett_subscription_match,
    build_jackett_subscription,
    select_jackett_subscription_candidate,
)
from kinopoisk import KinopoiskError, KinopoiskInfo, KP_URL_RE, extract_kp_id
from plex import (
    PlexMovie,
    PlexShow,
    PlexSeason,
    PlexSeriesCheckResult,
    PlexAPIError,
    PlexAuthError,
    PlexTimeoutError,
    PlexConnectionError,
    PlexParseError,
    check_before_download as _plex_check_before_download,
    check_before_download_season as _plex_check_before_download_season,
    _normalise_resolution as _plex_normalise_resolution,
)
from movie_discovery import (
    build_cards as _movie_build_cards,
    detect_quality as _movie_detect_quality,
    discovery_queries as _movie_discovery_queries,
    discovery_years as _movie_discovery_years,
    evaluate_result as _movie_evaluate_result,
    extract_year as _movie_extract_year,
    is_recent_published_at as _movie_is_recent_published_at,
    movie_key as _movie_card_key,
    normalize_movie_title as _normalize_movie_title,
    parse_published_at as _movie_parse_published_at,
    parse_qualities as _movie_parse_qualities,
    prune_kp_cache as _movie_prune_kp_cache,
    prune_seen_fingerprints as _movie_prune_seen_fingerprints,
    prune_tracker_data as _movie_prune_tracker_data,
)
from rutracker import RutrackerError, RutrackerResult, RutrackerTopicUnavailable
from task_policies import (
    auto_delete_notice as _policy_auto_delete_notice,
    format_task_notification as _policy_format_task_notification,
    is_auto_delete_candidate as _policy_is_auto_delete_candidate,
    notification_recipients as _policy_notification_recipients,
    notification_status_key as _policy_notification_status_key,
)
from task_views import (
    ACTIVE_STATUSES as _ACTIVE_STATUSES,
    default_list_scope as _view_default_list_scope,
    filter_tasks_for_scope as _view_filter_tasks_for_scope,
    find_task as _view_find_task,
    format_task_card as _view_format_task_card,
    format_tasks as _view_format_tasks,
    has_active_tasks as _view_has_active_tasks,
    normalize_list_scope as _view_normalize_list_scope,
)
from torrent_utils import (
    RawBencode,
    bdecode_torrent as _bdecode_torrent,
    bdecode_value as _bdecode_value,
    find_magnet as _find_magnet,
    find_magnet_task_id as _find_magnet_task_id,
    looks_like_torrent as _looks_like_torrent,
    magnet_info_hash as _magnet_info_hash,
    safe_filename as _safe_filename,
    task_matches_magnet as _task_matches_magnet,
    temp_path as _make_temp_path,
    torrent_file_is_private as _torrent_file_is_private,
    torrent_is_private as _torrent_is_private,
)
from tracker_service import (
    TrackerApplyResult,
    TrackerConfig,
    TrackerService,
    is_tracker_task_candidate as _tracker_is_task_candidate,
    parse_trackers_text as _tracker_parse_text,
    public_trackers_enabled as _tracker_public_enabled,
    tracker_attempt_is_final as _tracker_is_final,
    tracker_background_enabled as _tracker_background_is_enabled,
    tracker_button_visible as _tracker_is_button_visible,
    format_tracker_cache_time as _tracker_format_cache_time,
    tracker_key as _tracker_normalized_key,
    tracker_result_lines as _tracker_lines,
)


settings = load_settings()
app_context = build_app_context(settings)

BOT_TOKEN = settings.bot_token
ALLOWED_CHAT_IDS = settings.allowed_chat_ids
ADMIN_CHAT_IDS = settings.admin_chat_ids
ACCESS_APPROVALS_ENABLED = settings.access_approvals_enabled

TMP_DIR = settings.tmp_dir
STATE_DIR = settings.state_dir

DS_URL = settings.ds_url
DS_ACCOUNT = settings.ds_account
DS_PASSWORD = settings.ds_password
DS_DESTINATION = settings.ds_destination
DS_VERIFY_SSL = settings.ds_verify_ssl
BOT_TIMEZONE = settings.bot_timezone
MAX_TORRENT_FILE_MB = settings.max_torrent_file_mb
MAX_TORRENT_FILE_BYTES = settings.max_torrent_file_bytes
TRACKERS_MODE = settings.trackers_mode
TRACKERS_URL = settings.trackers_url
TRACKERS_MAX = settings.trackers_max
TRACKERS_CACHE_TTL_HOURS = settings.trackers_cache_ttl_hours
TRACKERS_CACHE_FILE = settings.trackers_cache_file
TRACKERS_BACKGROUND_ENABLED = settings.trackers_background_enabled
TRACKERS_BACKGROUND_INTERVAL_SECONDS = settings.trackers_background_interval_seconds
TRACKERS_PROCESSED_FILE = settings.trackers_processed_file
TASK_NOTIFICATIONS_ENABLED = settings.task_notifications_enabled
TASK_NOTIFICATION_STATUSES = settings.task_notification_statuses
TASK_NOTIFY_EXTERNAL_TASKS = settings.task_notify_external_tasks
NOTIFY_CHAT_IDS_RAW = settings.notify_chat_ids_raw
AUTO_DELETE_FINISHED_AFTER_HOURS = settings.auto_delete_finished_after_hours
AUTO_DELETE_FINISHED_STATUSES = settings.auto_delete_finished_statuses
APPROVED_CHAT_IDS_FILE = settings.approved_chat_ids_file
TASK_OWNERS_FILE = settings.task_owners_file
NOTIFIED_TASKS_FILE = settings.notified_tasks_file
AUTO_DELETE_TASKS_FILE = settings.auto_delete_tasks_file
MAGNET_POLL_ATTEMPTS = settings.magnet_poll_attempts
MAGNET_POLL_INTERVAL_SECONDS = settings.magnet_poll_interval_seconds
DS_RETRY_ATTEMPTS = settings.ds_retry_attempts
DS_RETRY_DELAY = settings.ds_retry_delay
RUTRACKER_USERNAME = settings.rutracker_username
RUTRACKER_PASSWORD = settings.rutracker_password
RUTRACKER_ENABLED = settings.rutracker_enabled
RUTRACKER_MAX_RESULTS = settings.rutracker_max_results
KINOPOISK_API_KEY = settings.kinopoisk_api_key
KINOPOISK_ENABLED = settings.kinopoisk_enabled
PLEX_ENABLED = settings.plex_enabled
PLEX_URL = settings.plex_url
PLEX_TOKEN = settings.plex_token
TOPIC_SUBSCRIPTIONS_FILE = settings.topic_subscriptions_file
SUBSCRIPTION_CHECK_INTERVAL_HOURS = settings.subscription_check_interval_hours
JACKETT_URL = settings.jackett_url
JACKETT_API_KEY = settings.jackett_api_key
JACKETT_ENABLED = settings.jackett_enabled
JACKETT_INDEXERS = settings.jackett_indexers
JACKETT_MAX_RESULTS = settings.jackett_max_results
JACKETT_FETCH_LIMIT = settings.jackett_fetch_limit
MOVIE_DISCOVERY_ENABLED = settings.movie_discovery_enabled
MOVIE_DISCOVERY_INTERVAL_HOURS = settings.movie_discovery_interval_hours
MOVIE_DISCOVERY_DEBUG_FILE = settings.movie_discovery_debug_file
MOVIE_DISCOVERY_RUTRACKER_TM = settings.movie_discovery_rutracker_tm
MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE = settings.movie_discovery_jackett_require_date
MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS = settings.movie_discovery_jackett_max_age_days
MOVIE_DISCOVERY_LIMIT = settings.movie_discovery_limit
MOVIE_DISCOVERY_MIN_KP_RATING = settings.movie_discovery_min_kp_rating
MOVIE_DISCOVERY_QUALITIES = settings.movie_discovery_qualities

# kinopoiskapiunofficial.tech free tier: 500 requests/day
_KP_DAILY_LIMIT = 500
# Max stale KP entries refreshed per discovery run (mirrors movie_discovery._KP_MAX_STALE_REFRESH_PER_RUN)
_KP_MAX_STALE_REFRESH = 15

KP_URL_FILTER = filters.Regex(KP_URL_RE)
SEARCH_OPTIONS, SEARCH_ADVANCED, SEARCH_RESULTS, SEARCH_SEASON_SELECT, SEARCH_JACKETT_SELECT = range(5)
SEARCH_PLEX_CONFIRM = 5  # Waiting for user to confirm/cancel Plex duplicate warning
BOT_COMMANDS = [
    BotCommand("new", "Новинки фильмов"),
    BotCommand("subs", "Подписки на обновления"),
    BotCommand("status", "Список загрузок"),
    BotCommand("help", "Справка по боту"),
    BotCommand("id", "Показать мой chat_id"),
    BotCommand("ping", "Проверка связи"),
]
TELEGRAM_ALLOWED_UPDATES = ["message", "callback_query", "message_reaction"]
DOWNLOAD_PANEL_MESSAGES: dict[int, int] = {}
DOWNLOAD_PANEL_PAGES: dict[int, int] = {}
DOWNLOAD_PANEL_SCOPES: dict[int, str] = {}
DOWNLOAD_PANEL_HAD_ACTIVE: dict[int, bool] = {}
# chat_id → имя пользователя (заполняется при запросе доступа)
ACCESS_PENDING_USERS: dict[int, str] = {}
BACKGROUND_MONITOR_TASK: asyncio.Task | None = None
TRACKER_BACKGROUND_TASK: asyncio.Task | None = None
PROGRESS_UPDATE_TASK: asyncio.Task | None = None
SUBSCRIPTION_MONITOR_TASK: asyncio.Task | None = None
MOVIE_DISCOVERY_TASK: asyncio.Task | None = None
MOVIE_NOTIFY_PENDING_TASK: asyncio.Task | None = None
PROGRESS_UPDATE_INTERVAL_SECONDS = 30
# Seconds to wait after DS task creation before injecting public trackers.
# DS may not have fully initialised the task metadata immediately after create_torrent_file /
# create_magnet returns, so attempting tracker injection at t=0 often results in
# "добавление не подтвердилось".  The background monitor never needs this delay because
# tasks have been running for at least one check interval by the time it processes them.
_TRACKER_INJECT_INITIAL_DELAY = 3.0
# (chat_id, message_id) → running refresh task for that task card
TASK_CARD_REFRESH_TASKS: dict[tuple[int, int], asyncio.Task] = {}
# task_id → task-card messages that can be removed after a final notification
TASK_CARD_MESSAGES: dict[str, set[tuple[int, int]]] = {}
MAX_TASK_NOTIFICATION_FAILURES = 3
# Unix timestamp of next scheduled subscription check (set by the loop)
_next_subscription_check_at: float | None = None


logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tg_torrent_drop")


def _display_timezone() -> timezone:
    try:
        return ZoneInfo(BOT_TIMEZONE)
    except ZoneInfoNotFoundError:
        pass

    if BOT_TIMEZONE.lower() in {"europe/moscow", "msk", "moscow"}:
        return timezone(timedelta(hours=3), "MSK")

    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", BOT_TIMEZONE)
    if match:
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        if sign == "-":
            offset = -offset
        return timezone(offset, BOT_TIMEZONE)

    return timezone.utc


DISPLAY_TIMEZONE = _display_timezone()


ds_client = app_context.ds_client
state_store = app_context.state_store
rutracker_client = app_context.rutracker_client
jackett_client = app_context.jackett_client
kinopoisk_client = app_context.kinopoisk_client
plex_client = app_context.plex_client

# In-memory Plex library cache: (normalized_title, year) → PlexMovie
# Updated every 30 minutes by _plex_cache_background_loop.
_plex_library: dict[tuple[str, int], "PlexMovie"] = {}
_plex_library_updated_at: float = 0.0
_PLEX_CACHE_INTERVAL = 30 * 60  # seconds

# In-memory Plex TV shows cache: (normalized_title, year) → PlexShow.
# Shows are loaded with empty seasons{}; per-show season metadata is fetched
# lazily on demand via _plex_ensure_show_seasons.
_plex_shows_library: dict[tuple[str, int], "PlexShow"] = {}
_plex_shows_updated_at: float = 0.0

# Quiet hours: /new notifications are deferred outside [START, END) window (local display timezone)
_NOTIFY_WINDOW_START_HOUR = 9   # 09:00 inclusive
_NOTIFY_WINDOW_END_HOUR = 22    # 22:00 exclusive

# Plex server machine identifier — fetched once at startup, used in deep links.
_plex_machine_id: str = ""

# Polling tasks waiting for a downloaded file to appear in Plex.
# task_id → asyncio.Task while polling is active; → None after polling completed.
# Keeping the key (even as None) prevents re-launching a second poll after the first finishes.
_PLEX_POLLING_TASKS: dict[str, "asyncio.Task[None] | None"] = {}

# Single-flight refresh: serialise concurrent `_refresh_plex_library` calls so
# the 30-min background loop + N polling loops don't hit Plex API in parallel.
# Created lazily inside the function to avoid binding to a stale event loop.
_plex_refresh_lock: "asyncio.Lock | None" = None
# Coalesce window — if a refresh completed less than this many seconds ago, skip the next one.
_PLEX_REFRESH_COALESCE_SECONDS = 5.0

# Plex health tracking — shown in /admin diagnostics.
_plex_last_error_kind: str = ""        # "auth"/"timeout"/"network"/"xml"/"http"/"other"/""
_plex_last_error_message: str = ""
_plex_last_error_at: float = 0.0
_plex_last_success_at: float = 0.0
_plex_consecutive_failures: int = 0


def _tracker_config() -> TrackerConfig:
    return TrackerConfig(
        mode=TRACKERS_MODE,
        url=TRACKERS_URL,
        max_count=TRACKERS_MAX,
        cache_ttl_hours=TRACKERS_CACHE_TTL_HOURS,
        cache_file=TRACKERS_CACHE_FILE,
        background_enabled=TRACKERS_BACKGROUND_ENABLED,
    )


def _tracker_service() -> TrackerService:
    return TrackerService(_tracker_config(), ds_client, logger)

def _load_approved_chat_ids() -> set[int]:
    return state_store.load_approved_chat_ids()


def _save_approved_chat_ids(chat_ids: set[int]) -> None:
    state_store.save_approved_chat_ids(chat_ids)


def _all_allowed_chat_ids() -> set[int]:
    return all_allowed_chat_ids(ALLOWED_CHAT_IDS, ADMIN_CHAT_IDS, _load_approved_chat_ids())


def _is_admin_chat(chat_id: int | None) -> bool:
    return is_admin_chat(chat_id, ADMIN_CHAT_IDS)


def _is_allowed(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    return is_allowed_chat(chat_id, ALLOWED_CHAT_IDS, ADMIN_CHAT_IDS, _load_approved_chat_ids())


def _chat_id(update: Update) -> str:
    if not update.effective_chat:
        return "unknown"

    return str(update.effective_chat.id)


def _access_request_user_label(update: Update) -> str:
    user = update.effective_user
    if not user:
        return access_request_user_label(None, None, None)

    return access_request_user_label(user.full_name, user.username, user.id)


async def _send_access_request_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not ACCESS_APPROVALS_ENABLED or not ADMIN_CHAT_IDS or not update.effective_chat:
        return False

    chat_id = update.effective_chat.id
    if chat_id in ACCESS_PENDING_USERS or chat_id in _all_allowed_chat_ids():
        return True

    user_label = _access_request_user_label(update)
    text = (
        "Запрос доступа к боту\n"
        f"Пользователь: {user_label}\n"
        f"chat_id: {chat_id}"
    )

    sent = False
    for admin_chat_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=text,
                reply_markup=_access_approval_keyboard(chat_id),
            )
            sent = True
        except Exception:
            logger.warning("Failed to send access request to admin %s", admin_chat_id, exc_info=True)

    if sent:
        ACCESS_PENDING_USERS[chat_id] = user_label

    return sent


async def _reply_access_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    sent_to_admin = await _send_access_request_to_admins(update, context)
    tail = (
        "Запрос отправлен администратору."
        if sent_to_admin
        else "Передайте этот chat_id администратору."
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            "Доступ пока не настроен.\n"
            f"Ваш chat_id: {chat_id}\n"
            f"{tail}"
        )


def _temp_path(filename: str) -> Path:
    return _make_temp_path(TMP_DIR, filename)


def _public_trackers_enabled() -> bool:
    return _tracker_public_enabled(_tracker_config())


def _tracker_background_enabled() -> bool:
    return _tracker_background_is_enabled(_tracker_config())


def _task_notifications_enabled() -> bool:
    return TASK_NOTIFICATIONS_ENABLED and bool(TASK_NOTIFICATION_STATUSES)


def _auto_delete_finished_enabled() -> bool:
    return AUTO_DELETE_FINISHED_AFTER_HOURS > 0 and bool(AUTO_DELETE_FINISHED_STATUSES)


def _background_monitor_enabled() -> bool:
    return (
        _tracker_background_enabled()
        or _task_notifications_enabled()
        or _auto_delete_finished_enabled()
    )


def _task_maintenance_enabled() -> bool:
    return _background_monitor_enabled()


def _subscription_monitor_enabled() -> bool:
    return bool(rutracker_client or jackett_client)


def _tracker_key(tracker: str) -> str:
    return _tracker_normalized_key(tracker)


def _parse_trackers_text(text: str) -> list[str]:
    return _tracker_parse_text(text)


def _read_trackers_cache(require_fresh: bool = True) -> tuple[list[str], float | None]:
    return _tracker_service().read_cache(require_fresh=require_fresh)


def _write_trackers_cache(text: str) -> None:
    _tracker_service().write_cache(text)


def _load_public_trackers() -> tuple[list[str], float | None]:
    return _tracker_service().load_public_trackers()


def _format_tracker_cache_time(cache_time: float | None) -> str:
    return _tracker_format_cache_time(cache_time, DISPLAY_TIMEZONE)


def _tracker_result_lines(result: TrackerApplyResult | None) -> list[str]:
    return _tracker_lines(
        result,
        enabled=_public_trackers_enabled(),
        display_timezone=DISPLAY_TIMEZONE,
    )


def _add_public_trackers_to_download_task(task_id: str) -> TrackerApplyResult:
    return _tracker_service().add_public_trackers_to_download_task(task_id)


def _load_tracker_processed_ids() -> set[str]:
    return state_store.load_tracker_processed_ids()


def _save_tracker_processed_ids(task_ids: set[str]) -> None:
    state_store.save_tracker_processed_ids(task_ids)


def _add_tracker_processed_ids(task_ids: set[str]) -> None:
    task_ids = {str(task_id) for task_id in task_ids if task_id}
    if not task_ids:
        return

    state_store.add_tracker_processed_ids(task_ids)


def _is_tracker_task_candidate(task: dict, processed_ids: set[str]) -> bool:
    return _tracker_is_task_candidate(task, processed_ids)


def _tracker_attempt_is_final(result: TrackerApplyResult) -> bool:
    return _tracker_is_final(result)


def _mark_tracker_processed_if_final(task_id: str, result: TrackerApplyResult) -> None:
    if not task_id or not _tracker_attempt_is_final(result):
        return

    _add_tracker_processed_ids({task_id})


def _tracker_button_visible(task_id: str, status: str, task_type: str) -> bool:
    return _tracker_is_button_visible(
        task_id,
        status,
        task_type,
        background_enabled=_tracker_background_enabled(),
        processed_ids=_load_tracker_processed_ids(),
    )


async def _run_tracker_background_once() -> None:
    if not _tracker_background_enabled():
        return

    processed_ids = _load_tracker_processed_ids()
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError:
        logger.warning("Background tracker scan failed to list tasks", exc_info=True)
        return

    changed = False
    for task in tasks:
        if not _is_tracker_task_candidate(task, processed_ids):
            continue

        task_id = task["id"]
        result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
        logger.debug(
            "Background trackers for %s: added=%s available=%s skipped=%s",
            task_id,
            result.added_count,
            result.available_count,
            result.skipped_reason,
        )

        if _tracker_attempt_is_final(result):
            if task_id not in processed_ids:
                processed_ids.add(task_id)
                changed = True

    if changed:
        _add_tracker_processed_ids(processed_ids)


async def _run_background_step(label: str, step) -> None:
    try:
        await step()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Background step failed: %s", label)


async def _run_background_monitor_cycle(app: Application) -> None:
    await _run_background_step("public tracker scan", _run_tracker_background_once)
    await _run_task_maintenance_cycle(app)


async def _run_task_maintenance_cycle(app: Application) -> None:
    await _run_background_step("task notifications", lambda: _run_task_notifications_once(app))
    await _run_background_step("auto-delete finished tasks", _run_auto_delete_finished_once)
    await _run_background_step("stale state pruning", _run_prune_stale_state_once)


async def _tracker_background_loop() -> None:
    if not _tracker_background_enabled():
        logger.info("Tracker background monitor disabled")
        return

    logger.info(
        "Tracker background monitor enabled, interval=%ss",
        TRACKERS_BACKGROUND_INTERVAL_SECONDS,
    )

    try:
        await asyncio.sleep(10)
        while True:
            await _run_background_step("public tracker scan", _run_tracker_background_once)
            await asyncio.sleep(TRACKERS_BACKGROUND_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Tracker background monitor stopped")
        raise


async def _task_maintenance_loop(app: Application) -> None:
    if not _task_maintenance_enabled():
        logger.info("Task maintenance monitor disabled")
        return

    logger.info(
        "Task maintenance monitor enabled, interval=%ss",
        TRACKERS_BACKGROUND_INTERVAL_SECONDS,
    )

    try:
        await asyncio.sleep(10)
        while True:
            await _run_task_maintenance_cycle(app)
            await asyncio.sleep(TRACKERS_BACKGROUND_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Task maintenance monitor stopped")
        raise


def _format_updated_at() -> str:
    return datetime.now(DISPLAY_TIMEZONE).strftime("%H:%M:%S")


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Return the correct Russian plural form for *n*.

    ``one``  — 1, 21, 31 … (одна запись)
    ``few``  — 2-4, 22-24 … (две записи)
    ``many`` — 0, 5-20, 25-30 … (пять записей)
    """
    n = abs(n)
    rem100 = n % 100
    rem10 = n % 10
    if 11 <= rem100 <= 19:
        return many
    if rem10 == 1:
        return one
    if 2 <= rem10 <= 4:
        return few
    return many


def _format_admin_configured_integrations() -> str:
    integrations = [
        ("Rutracker", RUTRACKER_ENABLED),
        ("Jackett", JACKETT_ENABLED),
        ("Кинопоиск", KINOPOISK_ENABLED),
        ("Plex", PLEX_ENABLED),
        ("Трекеры", _public_trackers_enabled()),
    ]
    parts = [f"{'🟢' if enabled else '🔴'} {name}" for name, enabled in integrations]
    return "• " + " · ".join(parts)


def _format_admin_tasks_line(tasks: list[dict] | None, error: str = "") -> str:
    if error:
        return f"• Загрузки: ошибка получения списка ({html_module.escape(error)})"
    if tasks is None:
        return "• Загрузки: нет данных"

    active = sum(1 for task in tasks if (task.get("status") or "").lower() in _ACTIVE_STATUSES)
    finished = sum(1 for task in tasks if (task.get("status") or "").lower() == "finished")
    failed = sum(1 for task in tasks if (task.get("status") or "").lower() == "error")
    return f"• Загрузки: {len(tasks)} всего · {active} активных · {finished} завершённых · {failed} ошибок"


def _format_admin_subscriptions_line() -> str:
    subs = state_store.load_topic_subscriptions()
    rutracker_count = sum(1 for sub in subs.values() if sub.get("type") != "jackett")
    jackett_count = sum(1 for sub in subs.values() if sub.get("type") == "jackett")
    total = rutracker_count + jackett_count

    next_check = ""
    if _next_subscription_check_at is not None:
        next_dt = datetime.fromtimestamp(_next_subscription_check_at, DISPLAY_TIMEZONE)
        next_check = f"\n  Следующая проверка: {next_dt.strftime('%d.%m %H:%M')}"

    return f"• Подписки: {total} всего · Rutracker {rutracker_count} · Jackett {jackett_count}{next_check}"


def _subscription_owner_label(chat_id: int | None, approved_users: dict[int, dict] | None = None) -> str:
    if chat_id is None:
        return "владелец неизвестен"

    label = str(chat_id)
    approved_users = approved_users if approved_users is not None else state_store.load_approved_users()
    user_info = approved_users.get(chat_id, {})
    name = user_info.get("name") if isinstance(user_info, dict) else ""
    if name:
        label = f"{label} ({html_module.escape(str(name))})"

    if _is_admin_chat(chat_id):
        label += ", админ"

    return label


def _can_manage_subscription(chat_id: int | None, sub: dict | None) -> bool:
    if not sub or chat_id is None:
        return False
    return _is_admin_chat(chat_id) or sub.get("chat_id") == chat_id


def _admin_subscriptions_keyboard(subs: dict[str, dict]) -> InlineKeyboardMarkup:
    rows = []
    for index, (key, sub) in enumerate(subs.items(), 1):
        if sub.get("type") == "jackett":
            rows.append([
                InlineKeyboardButton(
                    f"🗑️ {index}. Jackett",
                    callback_data=f"{SUB_CALLBACK_PREFIX}:admin_jackett_unsub:{key}",
                )
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"🗑️ {index}. Rutracker",
                    callback_data=f"{SUB_CALLBACK_PREFIX}:admin_unsub:{key}",
                )
            ])

    rows.append([
        InlineKeyboardButton("🔄 Обновить", callback_data=f"{ADMIN_CALLBACK_PREFIX}:subscriptions"),
        InlineKeyboardButton("⬅️ Админ-панель", callback_data=f"{ADMIN_CALLBACK_PREFIX}:home"),
    ])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"{ADMIN_CALLBACK_PREFIX}:close")])
    return InlineKeyboardMarkup(rows)


def _build_admin_subscriptions_view() -> tuple[str, InlineKeyboardMarkup]:
    subs = state_store.load_topic_subscriptions()
    approved_users = state_store.load_approved_users()

    if not subs:
        return (
            "🔔 <b>Подписки</b>\n\nАктивных подписок нет.",
            _admin_subscriptions_keyboard({}),
        )

    lines = [f"🔔 <b>Подписки</b> ({len(subs)})"]

    for index, (key, sub) in enumerate(subs.items(), 1):
        owner = _subscription_owner_label(sub.get("chat_id"), approved_users)
        if sub.get("type") == "jackett":
            query_text = html_module.escape(str(sub.get("query") or key))
            last_check = html_module.escape(str(sub.get("last_check") or "—"))
            tracker = html_module.escape(str(sub.get("tracker") or "—"))
            title = html_module.escape(_format_sub_title(str(sub.get("title") or "")))
            ep_end = html_module.escape(str(sub.get("last_episode_end", "?")))
            total = html_module.escape(str(sub.get("total_episodes", "?")))
            lines.append(
                f"\n{index}. 🌐 <b>Jackett</b>\n"
                f"   Владелец: {owner}\n"
                f"   Трекер: {tracker}\n"
                f"   Тема: {title or query_text}\n"
                f"   Серии: {ep_end} из {total}\n"
                f"   Запрос: {query_text}\n"
                f"   Проверено: {last_check}"
            )
            continue

        title = html_module.escape(_format_sub_title(sub.get("title", "") or key))
        ep_end = html_module.escape(str(sub.get("last_episode_end", "?")))
        total = html_module.escape(str(sub.get("total_episodes", "?")))
        lines.append(
            f"\n{index}. 🔎 <b>Rutracker</b>\n"
            f"   Владелец: {owner}\n"
            f"   Тема: {title}\n"
            f"   Серии: {ep_end} из {total}"
        )
        if sub.get("unavailable_at"):
            reason = html_module.escape(str(sub.get("unavailable_reason") or "тема недоступна"))
            lines.append(f"   ⚠️ Проверка приостановлена: {reason}")

    if _next_subscription_check_at is not None:
        next_dt = datetime.fromtimestamp(_next_subscription_check_at, DISPLAY_TIMEZONE)
        lines.append(f"\n🕐 Следующая проверка: {next_dt.strftime('%d.%m %H:%M')}")

    return "\n".join(lines), _admin_subscriptions_keyboard(subs)


def _format_admin_auto_delete_line() -> str:
    if not _auto_delete_finished_enabled():
        return "• Автоудаление: выключено"

    statuses = ", ".join(sorted(AUTO_DELETE_FINISHED_STATUSES)) or "нет статусов"
    return f"• Автоудаление: через {AUTO_DELETE_FINISHED_AFTER_HOURS:g} ч · {statuses}"


def _format_admin_notifications_line() -> str:
    if not _task_notifications_enabled():
        return "• Уведомления: выключены"

    statuses = ", ".join(sorted(TASK_NOTIFICATION_STATUSES)) or "нет статусов"
    return f"• Уведомления: {statuses}"


def _format_admin_search_defaults_line() -> str:
    quality = _SRCH_DEFAULT_SETTINGS.get("quality", "1080p")
    audio = "да" if _SRCH_DEFAULT_SETTINGS.get("audio") else "нет"
    subs = "да" if _SRCH_DEFAULT_SETTINGS.get("subs") else "нет"
    return f"• Поиск: {quality} · оригинальная дорожка: {audio} · субтитры: {subs}"


def _format_kp_api_stats_line(cache: dict) -> str:
    """Format the KP API budget line for the admin panel Новинки section."""
    if not KINOPOISK_ENABLED:
        return ""
    stats = cache.get("kp_api_stats")
    today = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d")
    if not isinstance(stats, dict) or stats.get("date") != today:
        return "• KP API сегодня: нет данных"
    searches = int(stats.get("searches") or 0)
    http_est = round(searches * 1.5)
    remaining = max(0, _KP_DAILY_LIMIT - http_est)
    return (
        f"• KP API сегодня: {searches} {_plural(searches, 'поиск', 'поиска', 'поисков')} "
        f"(~{http_est} HTTP-вызовов) · ~{remaining} из {_KP_DAILY_LIMIT} осталось"
    )


def _format_admin_movie_discovery_line() -> str:
    cache = state_store.load_movie_discovery_cache()
    cards = cache.get("cards") if isinstance(cache.get("cards"), list) else []
    updated_at = str(cache.get("updated_at") or "ещё не обновлялись")
    qualities = ", ".join(_movie_parse_qualities(MOVIE_DISCOVERY_QUALITIES))
    sources = []
    if rutracker_client is not None:
        sources.append("Rutracker")
    if jackett_client is not None:
        sources.append("Jackett")
    source_text = ", ".join(sources) if sources else "нет источников"

    if not MOVIE_DISCOVERY_ENABLED:
        return "• Статус: выключены"

    status = "включены" if sources else "включены, но нет источников"
    kp_stats_line = _format_kp_api_stats_line(cache)
    lines = [
        f"• Статус: {status}",
        f"• Источники: {source_text}",
        f"• Общие фильтры: {qualities} · КП от {MOVIE_DISCOVERY_MIN_KP_RATING:g}",
        f"• Rutracker: {_movie_rutracker_tm_label(MOVIE_DISCOVERY_RUTRACKER_TM)}",
        f"• Jackett: {'только с датой' if MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE else 'без строгой даты'} · "
        f"до {MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS} дн.",
        f"• Автообновление: раз в {MOVIE_DISCOVERY_INTERVAL_HOURS} ч",
        f"• Кэш: {html_module.escape(updated_at)} · карточек: {len(cards)}",
    ]
    if kp_stats_line:
        lines.append(kp_stats_line)

    # Subscriber count
    movie_sub_count = len(_get_movie_subscriptions())
    if movie_sub_count:
        lines.append(f"• Подписок на /new: {movie_sub_count}")

    # Tracker rating status
    md_settings = _load_movie_discovery_settings()
    known_ids: list[str] = md_settings.get("jackett_trackers_known") or []
    if known_ids:
        enabled_ids_raw = md_settings.get("jackett_trackers_enabled")
        enabled_set = set(enabled_ids_raw) if enabled_ids_raw is not None else set(known_ids)
        enabled_sorted = sorted(t for t in known_ids if t in enabled_set)
        disabled_sorted = sorted(t for t in known_ids if t not in enabled_set)
        tracker_parts = [f"🟢 {_tracker_abbr(t)}" for t in enabled_sorted]
        tracker_parts += [f"🔴 {_tracker_abbr(t)}" for t in disabled_sorted]
        tracker_line = " ".join(tracker_parts)
        if enabled_ids_raw is not None:
            tracker_line += f" ({len(enabled_sorted)}/{len(known_ids)})"
        lines.append(f"• Трекеры рейтинга: {tracker_line}")

    return "\n".join(lines)


async def _build_admin_panel_text() -> str:
    tasks = None
    task_error = ""
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError as exc:
        task_error = str(exc)

    now = datetime.now(DISPLAY_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    lines = [
        "🛠️ <b>Админ-панель</b>",
        f"Время бота: {now}",
        "",
        "📊 <b>Состояние</b>",
        _format_admin_tasks_line(tasks, task_error),
        _format_admin_subscriptions_line(),
        "",
        "⚙️ <b>Правила и интеграции</b>",
        _format_admin_configured_integrations(),
        _format_admin_auto_delete_line(),
        _format_admin_notifications_line(),
        _format_admin_search_defaults_line(),
        "<i>Живой статус сервисов — в разделе «Диагностика».</i>",
        "",
        "🎬 <b>Новинки</b>",
        _format_admin_movie_discovery_line(),
    ]
    return "\n".join(lines)


def _default_list_scope(chat_id: int | None) -> str:
    return _view_default_list_scope(
        _is_admin_chat(chat_id),
        scope_all=TASK_LIST_SCOPE_ALL,
        scope_my=TASK_LIST_SCOPE_MY,
    )


def _normalize_list_scope(scope: str | None, chat_id: int | None) -> str:
    return _view_normalize_list_scope(
        scope,
        _is_admin_chat(chat_id),
        scope_all=TASK_LIST_SCOPE_ALL,
        scope_my=TASK_LIST_SCOPE_MY,
        scope_default=TASK_LIST_SCOPE_DEFAULT,
    )


def _chat_id_from_query(query) -> int | None:
    if query and query.message and query.message.chat:
        return query.message.chat.id

    return None


def _message_id_from_message(message) -> int | None:
    message_id = getattr(message, "message_id", None)
    return message_id if isinstance(message_id, int) else None


def _chat_id_from_message(message) -> int | None:
    chat_id = getattr(message, "chat_id", None)
    if isinstance(chat_id, int):
        return chat_id

    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    return chat_id if isinstance(chat_id, int) else None


def _task_owner(task_id: str | None, owners: dict[str, int] | None = None) -> int | None:
    if not task_id:
        return None

    return (owners or _load_task_owners()).get(str(task_id))


def _can_access_task_id(chat_id: int | None, task_id: str) -> bool:
    if _is_admin_chat(chat_id):
        return True

    return _task_owner(task_id) == chat_id


def _filter_tasks_for_scope(tasks: list[dict], chat_id: int | None, scope: str) -> list[dict]:
    scope = _normalize_list_scope(scope, chat_id)
    return _view_filter_tasks_for_scope(
        tasks,
        chat_id,
        scope,
        owners=_load_task_owners(),
        is_admin=_is_admin_chat(chat_id),
        scope_all=TASK_LIST_SCOPE_ALL,
    )


def _format_tasks(
    tasks: list[dict],
    scope: str = TASK_LIST_SCOPE_ALL,
    total_count: int | None = None,
    page: int = 0,
) -> str:
    return _view_format_tasks(
        tasks,
        scope=scope,
        updated_at=_format_updated_at(),
        owners=_load_task_owners() if scope == TASK_LIST_SCOPE_ALL else {},
        total_count=total_count,
        page=page,
        page_size=TASK_LIST_PAGE_SIZE,
        scope_all=TASK_LIST_SCOPE_ALL,
    )


def _load_movie_discovery_cache() -> dict:
    return state_store.load_movie_discovery_cache()


def _save_movie_discovery_cache(cache: dict) -> None:
    state_store.save_movie_discovery_cache(cache)


def _load_movie_discovery_settings() -> dict:
    return state_store.load_movie_discovery_settings()


def _save_movie_discovery_settings(settings: dict) -> None:
    state_store.save_movie_discovery_settings(settings)


# --- Movie discovery subscription helpers ---

def _get_movie_subscriptions() -> dict:
    """Return {chat_id_str: {subscribed_at: str}} dict of /new subscribers."""
    return _load_movie_discovery_settings().get("movie_subscriptions") or {}


def _is_movie_subscribed(chat_id: int) -> bool:
    return str(chat_id) in _get_movie_subscriptions()


def _set_movie_subscription(chat_id: int, subscribed: bool) -> None:
    settings = _load_movie_discovery_settings()
    subs = settings.setdefault("movie_subscriptions", {})
    if subscribed:
        subs[str(chat_id)] = {
            "subscribed_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
        }
    else:
        subs.pop(str(chat_id), None)
    _save_movie_discovery_settings(settings)


def _save_movie_discovery_debug(report: dict) -> None:
    state_store.save_json_file(MOVIE_DISCOVERY_DEBUG_FILE, report, "movie discovery debug")


def _movie_discovery_enabled() -> bool:
    return MOVIE_DISCOVERY_ENABLED and bool(rutracker_client or jackett_client)


def _movie_rutracker_tm_label(value: int) -> str:
    labels = {
        -1: "за всё время",
        1: "за сегодня",
        3: "за последние 3 дня",
        7: "за неделю",
        14: "за 2 недели",
        32: "за месяц",
    }
    return labels.get(value, "за месяц")


def _movie_jackett_date_filter_reason(release: dict | None, now: datetime) -> str:
    if release is None or not MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE:
        return "accepted"

    published_at = str(release.get("published_at") or "")
    if not published_at.strip():
        return "published_missing"
    if _movie_parse_published_at(published_at) is None:
        return "published_invalid"
    if not _movie_is_recent_published_at(
        published_at,
        now=now,
        max_age_days=MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS,
    ):
        return "published_too_old"
    return "accepted"


def _movie_release_to_search_result(release: dict) -> dict:
    return {
        "source": release.get("source") or "jackett",
        "topic_id": release.get("topic_id") or "",
        "title": release.get("title") or "",
        "url": release.get("url") or release.get("topic_url") or "",
        "category": release.get("tracker") or "",
        "size": release.get("size") or "",
        "seeders": release.get("seeders") or 0,
        "partial": False,
        "ep_str": "",
        "magnet_url": release.get("magnet_url"),
        "torrent_url": release.get("torrent_url"),
        "published_at": release.get("published_at"),
        "tracker_name": release.get("tracker") or ("rutracker" if release.get("source") == "rutracker" else ""),
    }


def _movie_discovery_audit_row(search_query: str, source: str, result, release: dict | None, reason: str) -> dict:
    return {
        "query": search_query,
        "source": source,
        "decision": reason,
        "title": str(getattr(result, "title", "") or ""),
        "category": str(getattr(result, "category", "") or getattr(result, "tracker", "") or ""),
        "tracker": str(getattr(result, "tracker", "") or ""),
        "size": str(getattr(result, "size", "") or ""),
        "seeders": int(getattr(result, "seeders", 0) or 0),
        "published_at": str(getattr(result, "published_at", "") or ""),
        "movie_title": release.get("movie_title") if release else "",
        "year": release.get("year") if release else None,
        "quality": release.get("quality") if release else "",
        "score": release.get("score") if release else None,
    }


def _movie_discovery_keyboard(cards: list[dict], chat_id: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    for index, card in enumerate(cards[:10], 1):
        main = str(card.get("title") or "Новинка")
        alt = str(card.get("alt_title") or "")
        display = f"{main} / {alt}" if alt else main
        title = _short_title({"title": display}, limit=42)
        rows.append([InlineKeyboardButton(
            f"🎬 {index}. {title}",
            callback_data=f"new:show:{index - 1}",
        )])
    is_subscribed = chat_id is not None and _is_movie_subscribed(chat_id)
    sub_label = "🔕 Отписаться от /new" if is_subscribed else "🔔 Подписаться на /new"
    sub_cb = "new:unsubscribe" if is_subscribed else "new:subscribe"
    rows.append([InlineKeyboardButton(sub_label, callback_data=sub_cb)])
    rows.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="new:refresh"),
        InlineKeyboardButton("✖️ Закрыть", callback_data="new:close"),
    ])
    return InlineKeyboardMarkup(rows)


def _movie_card_tracker_labels(card: dict) -> str:
    labels = []
    seen = set()
    for release in card.get("releases") or []:
        tracker = str(release.get("tracker") or "")
        source = str(release.get("source") or "")
        tracker_id = tracker or ("rutracker" if source == "rutracker" else source)
        if not tracker_id:
            continue
        label = _tracker_abbr(tracker_id)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return ", ".join(labels)


def _format_kp_votes(votes: int | None) -> str:
    """Format KP vote count as a compact string: 1 234 → '1.2K', 1 500 000 → '1.5M'."""
    if votes is None or not isinstance(votes, int) or votes <= 0:
        return ""
    if votes >= 1_000_000:
        return f"{votes / 1_000_000:.1f}M"
    if votes >= 1_000:
        return f"{votes / 1_000:.0f}K"
    return str(votes)


def _format_movie_discovery_cache(cache: dict) -> str:
    cards = cache.get("cards") if isinstance(cache.get("cards"), list) else []
    updated_at = cache.get("updated_at") or "—"
    qualities = ", ".join(_movie_parse_qualities(MOVIE_DISCOVERY_QUALITIES))
    years = ", ".join(str(year) for year in sorted(_movie_discovery_years(datetime.now(DISPLAY_TIMEZONE)), reverse=True))
    lines = [
        "🎬 <b>Новинки</b>",
        f"Обновлено: {html_module.escape(str(updated_at))}",
        f"Фильтр: годы {years}; качество {html_module.escape(qualities)}; КП от {MOVIE_DISCOVERY_MIN_KP_RATING:g}",
    ]
    if not cards:
        lines.append("\nПока нет подходящих фильмов. Кэш обновится в фоне, можно попробовать позже.")
        return "\n".join(lines)

    for index, card in enumerate(cards[:10], 1):
        main_title = html_module.escape(str(card.get("title") or "Без названия"))
        alt_title = html_module.escape(str(card.get("alt_title") or ""))
        title = f"{main_title} / {alt_title}" if alt_title else main_title
        year = html_module.escape(str(card.get("year") or ""))
        rating = card.get("rating")
        votes_fmt = _format_kp_votes(card.get("kp_votes"))
        votes_text = f" ({votes_fmt})" if votes_fmt else ""
        rating_text = f" · КП {rating:.1f}{votes_text}" if isinstance(rating, (int, float)) else ""
        genres = ", ".join(card.get("genres") or [])
        genres_text = f"\n   Жанры: {html_module.escape(genres)}" if genres else ""
        kp_url = card.get("kp_url")
        kp_text = f"\n   <a href=\"{html_module.escape(str(kp_url))}\">Кинопоиск</a>" if kp_url else ""
        new_mark = " 🆕" if card.get("is_new") else ""
        if card.get("in_plex"):
            plex_res = card.get("plex_resolution") or ""
            plex_mark = f" ✅ {html_module.escape(plex_res)}" if plex_res else " ✅"
        else:
            plex_mark = ""
        tracker_labels = _movie_card_tracker_labels(card)
        tracker_text = f" · {html_module.escape(tracker_labels)}" if tracker_labels else ""
        lines.append(
            f"\n{index}. <b>{title}</b>{plex_mark}{new_mark}\n"
            f"   {year}{rating_text}\n"
            f"   Лучшее: {html_module.escape(str(card.get('best_quality') or '?'))}, "
            f"{html_module.escape(str(card.get('best_size') or '?'))}, "
            f"сидов {html_module.escape(str(card.get('best_seeders') or 0))}\n"
            f"   Раздач: {html_module.escape(str(card.get('release_count') or len(card.get('releases') or [])))}{tracker_text}"
            f"{genres_text}{kp_text}"
        )
    return "\n".join(lines)


def _restore_first_seen_from_previous(
    new_cards: list[dict],
    previous_cards: list[dict],
) -> None:
    """Carry forward the ``first_seen_at`` timestamp for cards that already existed.

    build_cards always stamps ``first_seen_at=now`` for every card it constructs,
    so without this restoration step every bot restart would make all top-10 films
    look brand new to the push-notification logic.

    Lookup uses **three layered indexes**, queried in order. The first non-empty
    hit wins:

      1. **By card key** — primary, exact and fastest.
      2. **By kp_id** — same Kinopoisk ID across refreshes means same film, even
         if the title text changed (e.g. KP overwrote the raw release name with
         a canonical RU name on the next run).
      3. **By (normalised title, year)** — catches the case where the card's key
         flipped between refreshes because KP enrichment status changed:
            was:  ``movie_key("project hail mary", 2026)``   (KP not resolved yet)
            now:  ``"kp:12345"``                             (KP resolved this run)
         Both the new card's ``title`` and ``alt_title`` are probed, against
         either ``title`` or ``alt_title`` from previous cards — so a transition
         like raw English → canonical Russian (or vice versa) still matches.

    If multiple previous cards collide on the same lookup bucket we keep the
    earliest timestamp — that's the true first sighting of the film.

    Mutates ``new_cards`` in place; no return value.
    """
    prev_by_key: dict[str, str] = {}
    prev_by_kp_id: dict[str, str] = {}
    prev_by_title: dict[str, str] = {}

    def _remember_earliest(idx: dict[str, str], k: str, ts: str) -> None:
        existing = idx.get(k)
        if existing is None or ts < existing:
            idx[k] = ts

    for c in previous_cards:
        seen_at = str(c.get("first_seen_at") or "")
        if not seen_at:
            continue
        if c.get("key"):
            _remember_earliest(prev_by_key, str(c["key"]), seen_at)
        kp_id = c.get("kp_id")
        if kp_id:
            _remember_earliest(prev_by_kp_id, str(kp_id), seen_at)
        year = c.get("year") or 0
        if year:
            # Index against BOTH title and alt_title — KP enrichment may have
            # swapped the canonical title between refreshes (en ↔ ru).
            for title_field in (c.get("title"), c.get("alt_title")):
                t = str(title_field or "")
                if not t:
                    continue
                try:
                    _remember_earliest(prev_by_title, _movie_card_key(t, int(year)), seen_at)
                except (TypeError, ValueError):
                    continue

    for card in new_cards:
        # 1. Exact-key match (fastest).
        old_ts = prev_by_key.get(card.get("key") or "")
        # 2. Same KP id — definitive proof it's the same film.
        if not old_ts:
            kp_id = card.get("kp_id")
            if kp_id:
                old_ts = prev_by_kp_id.get(str(kp_id))
        # 3. Title+year fallback (probe both title and alt_title).
        if not old_ts:
            year = card.get("year") or 0
            if year:
                for title_field in (card.get("title"), card.get("alt_title")):
                    t = str(title_field or "")
                    if not t:
                        continue
                    try:
                        hit = prev_by_title.get(_movie_card_key(t, int(year)))
                    except (TypeError, ValueError):
                        continue
                    if hit:
                        old_ts = hit
                        break
        if old_ts:
            card["first_seen_at"] = old_ts


async def _refresh_movie_discovery_cache(max_stale_kp_refresh: int | None = _KP_MAX_STALE_REFRESH) -> dict:
    now = datetime.now(DISPLAY_TIMEZONE)
    now_text = now.strftime("%Y-%m-%d %H:%M")

    # --- Tracker monitoring: detect removed Jackett trackers ---
    md_settings = _load_movie_discovery_settings()
    known_tracker_ids: set[str] = set(md_settings.get("jackett_trackers_known") or [])
    current_tracker_ids: set[str] = set()
    if jackett_client is not None:
        try:
            indexers = await asyncio.to_thread(jackett_client.get_indexers)
            current_tracker_ids = {idx["id"] for idx in indexers if idx.get("id")}
        except Exception:
            logger.warning("Failed to get Jackett indexers for tracker monitoring", exc_info=True)
            current_tracker_ids = known_tracker_ids
    removed_tracker_ids = known_tracker_ids - current_tracker_ids
    if removed_tracker_ids:
        logger.info("Jackett trackers removed, pruning: %s", removed_tracker_ids)
        enabled = md_settings.get("jackett_trackers_enabled")
        if enabled is not None:
            updated_enabled = [t for t in enabled if t not in removed_tracker_ids]
            md_settings["jackett_trackers_enabled"] = updated_enabled if updated_enabled else None
    if current_tracker_ids or known_tracker_ids:
        md_settings["jackett_trackers_known"] = sorted(current_tracker_ids or known_tracker_ids)
        _save_movie_discovery_settings(md_settings)

    enabled_ids: set[str] | None = (
        set(md_settings["jackett_trackers_enabled"])
        if md_settings.get("jackett_trackers_enabled") is not None
        else None
    )

    qualities = _movie_parse_qualities(MOVIE_DISCOVERY_QUALITIES)
    allowed_years = _movie_discovery_years(now)
    queries = _movie_discovery_queries(now, qualities)
    releases = []
    audit_rows = []
    reason_counts = Counter()
    source_counts = Counter()

    for search_query in queries:
        if jackett_client is not None:
            try:
                results = await asyncio.to_thread(
                    jackett_client.search,
                    search_query,
                    fetch_limit=JACKETT_FETCH_LIMIT,
                    categories="2000",
                )
                source_counts["jackett_raw"] += len(results)
                for result in results:
                    release, reason = _movie_evaluate_result(
                        result,
                        source="jackett",
                        allowed_years=allowed_years,
                        qualities=set(qualities),
                    )
                    if release:
                        date_reason = _movie_jackett_date_filter_reason(release, now)
                        if date_reason != "accepted":
                            reason = date_reason
                            release = None
                    reason_counts[f"jackett:{reason}"] += 1
                    audit_rows.append(_movie_discovery_audit_row(search_query, "jackett", result, release, reason))
                    if release:
                        releases.append(release)
            except JackettError:
                logger.warning("Movie discovery Jackett search failed: %s", search_query, exc_info=True)
                reason_counts["jackett:error"] += 1

        if rutracker_client is not None:
            try:
                results = await asyncio.to_thread(
                    rutracker_client.search,
                    search_query,
                    torrent_age_days=MOVIE_DISCOVERY_RUTRACKER_TM,
                )
                source_counts["rutracker_raw"] += len(results)
                for result in results:
                    release, reason = _movie_evaluate_result(
                        result,
                        source="rutracker",
                        allowed_years=allowed_years,
                        qualities=set(qualities),
                    )
                    reason_counts[f"rutracker:{reason}"] += 1
                    audit_rows.append(_movie_discovery_audit_row(search_query, "rutracker", result, release, reason))
                    if release:
                        releases.append(release)
            except RutrackerError:
                logger.warning("Movie discovery Rutracker search failed: %s", search_query, exc_info=True)
                reason_counts["rutracker:error"] += 1

    by_fingerprint = {}
    for release in releases:
        key = "|".join(str(release.get(part, "") or "") for part in ("source", "tracker", "topic_id", "topic_url", "title", "size"))
        by_fingerprint[key] = release

    all_releases = list(by_fingerprint.values())

    previous = _load_movie_discovery_cache()
    # Migrate legacy list format → dict[fingerprint, timestamp]
    raw_seen = previous.get("seen_fingerprints", [])
    known: dict[str, str] = (
        {fp: "" for fp in raw_seen} if isinstance(raw_seen, list) else dict(raw_seen)
    )
    known = _movie_prune_seen_fingerprints(known, now=now)
    # Prune fingerprints for removed trackers
    if removed_tracker_ids:
        _, known = _movie_prune_tracker_data([], known, removed_tracker_ids)
    prev_kp_cache: dict = (
        previous["kp_cache"] if isinstance(previous.get("kp_cache"), dict) else {}
    )
    prev_kp_cache = _movie_prune_kp_cache(prev_kp_cache, now=now)

    # Filter releases for card building by enabled trackers
    if enabled_ids is not None:
        build_releases = [r for r in all_releases if r.get("source") != "jackett" or r.get("tracker") in enabled_ids]
    else:
        build_releases = all_releases

    cache = await asyncio.to_thread(
        _movie_build_cards,
        build_releases,
        now_text=now_text,
        known_fingerprints=known,
        limit=MOVIE_DISCOVERY_LIMIT,
        min_kp_rating=MOVIE_DISCOVERY_MIN_KP_RATING,
        kinopoisk_client=kinopoisk_client,
        kp_cache=prev_kp_cache,
        max_stale_refresh=max_stale_kp_refresh,
    )
    # Restore first_seen_at from the previous cache for cards that already existed.
    # See _restore_first_seen_from_previous for full rationale.
    _restore_first_seen_from_previous(
        cache.get("cards") or [],
        previous.get("cards") or [],
    )
    _enrich_cards_with_plex(cache.get("cards") or [])
    cache["all_releases"] = all_releases

    # Accumulate daily KP API search counter
    kp_searches_this_run = int(cache.get("kp_searches") or 0)
    today_str = now.strftime("%Y-%m-%d")
    prev_stats = previous.get("kp_api_stats")
    if isinstance(prev_stats, dict) and prev_stats.get("date") == today_str:
        searches_today = int(prev_stats.get("searches") or 0) + kp_searches_this_run
    else:
        searches_today = kp_searches_this_run
    cache["kp_api_stats"] = {"date": today_str, "searches": searches_today}

    _save_movie_discovery_cache(cache)
    debug_report = {
        "updated_at": now_text,
        "freshness_model": (
            "Поиск сейчас ищет свежие фильмы по году выпуска, а не гарантированно новые "
            "раздачи по дате публикации. Если источник отдаёт published_at, дата сохраняется в аудите."
        ),
        "filters": {
            "years": sorted(allowed_years, reverse=True),
            "qualities": qualities,
            "min_kp_rating": MOVIE_DISCOVERY_MIN_KP_RATING,
            "limit": MOVIE_DISCOVERY_LIMIT,
            "rutracker_tm": MOVIE_DISCOVERY_RUTRACKER_TM,
            "rutracker_tm_label": _movie_rutracker_tm_label(MOVIE_DISCOVERY_RUTRACKER_TM),
            "jackett_require_date": MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE,
            "jackett_max_age_days": MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS,
        },
        "queries": queries,
        "counts": {
            **dict(source_counts),
            "accepted_before_dedup": len(releases),
            "accepted_after_dedup": len(by_fingerprint),
            "cards": len(cache.get("cards", [])),
        },
        "decision_counts": dict(sorted(reason_counts.items())),
        "cards": [
            {
                "title": card.get("title"),
                "year": card.get("year"),
                "kp_id": card.get("kp_id"),
                "rating": card.get("rating"),
                "score": card.get("score"),
                "release_count": card.get("release_count"),
                "best_quality": card.get("best_quality"),
                "best_size": card.get("best_size"),
                "best_seeders": card.get("best_seeders"),
            }
            for card in cache.get("cards", [])
        ],
        "audit": audit_rows[:1000],
        "audit_truncated": len(audit_rows) > 1000,
    }
    _save_movie_discovery_debug(debug_report)
    logger.info("Movie discovery refreshed: cards=%d releases=%d", len(cache.get("cards", [])), len(releases))
    return cache


async def _recompute_movie_discovery_from_cache() -> None:
    """Rebuild movie cards from stored releases using current tracker settings (no network calls)."""
    cache = _load_movie_discovery_cache()
    all_releases = cache.get("all_releases")
    if not isinstance(all_releases, list):
        return

    settings = _load_movie_discovery_settings()
    enabled_ids_raw = settings.get("jackett_trackers_enabled")
    if enabled_ids_raw is not None:
        enabled_ids: set[str] | None = set(enabled_ids_raw)
        build_releases = [r for r in all_releases if r.get("source") != "jackett" or r.get("tracker") in enabled_ids]
    else:
        build_releases = all_releases

    now = datetime.now(DISPLAY_TIMEZONE)
    now_text = cache.get("updated_at") or now.strftime("%Y-%m-%d %H:%M")
    raw_seen = cache.get("seen_fingerprints", {})
    known = dict(raw_seen) if isinstance(raw_seen, dict) else {}
    prev_kp_cache = cache.get("kp_cache") if isinstance(cache.get("kp_cache"), dict) else {}

    new_cache = await asyncio.to_thread(
        _movie_build_cards,
        build_releases,
        now_text=now_text,
        known_fingerprints=known,
        limit=MOVIE_DISCOVERY_LIMIT,
        min_kp_rating=MOVIE_DISCOVERY_MIN_KP_RATING,
        kinopoisk_client=kinopoisk_client,
        kp_cache=prev_kp_cache,
        max_stale_refresh=0,
    )
    _enrich_cards_with_plex(new_cache.get("cards") or [])
    new_cache["all_releases"] = all_releases
    new_cache["kp_api_stats"] = cache.get("kp_api_stats")
    _save_movie_discovery_cache(new_cache)
    logger.info("Movie discovery recomputed from cache: cards=%d", len(new_cache.get("cards", [])))


async def _movie_trackers_panel() -> tuple[str, "InlineKeyboardMarkup"]:
    """Build text + keyboard for the tracker selection admin screen."""
    all_trackers: list[dict] = []
    if jackett_client is not None:
        try:
            all_trackers = await asyncio.to_thread(jackett_client.get_indexers)
        except Exception:
            logger.warning("Failed to get Jackett indexers for admin panel", exc_info=True)

    settings = _load_movie_discovery_settings()
    known_ids: list[str] = settings.get("jackett_trackers_known") or []
    if not all_trackers and known_ids:
        all_trackers = [{"id": t, "name": _tracker_abbr(t)} for t in known_ids]
    elif all_trackers:
        fresh_ids = sorted(t.get("id", "") for t in all_trackers if t.get("id"))
        if fresh_ids != known_ids:
            settings["jackett_trackers_known"] = fresh_ids
            _save_movie_discovery_settings(settings)

    enabled_ids_raw = settings.get("jackett_trackers_enabled")
    enabled_ids: set[str] | None = set(enabled_ids_raw) if enabled_ids_raw is not None else None

    total = len(all_trackers)
    if total == 0:
        text = "🎬 Трекеры новинок\n\nJackett не настроен или нет доступных трекеров."
    else:
        selected = total if enabled_ids is None else sum(1 for t in all_trackers if t.get("id") in enabled_ids)
        text = (
            f"🎬 Трекеры новинок\n\n"
            f"Выбраны: {selected} из {total}\n\n"
            f"Отмеченные трекеры участвуют в рейтинге /new."
        )

    return text, movie_trackers_keyboard(all_trackers, enabled_ids)


def _is_in_notification_window() -> bool:
    """Return True when current local time is within the quiet-hours window."""
    hour = datetime.now(DISPLAY_TIMEZONE).hour
    return _NOTIFY_WINDOW_START_HOUR <= hour < _NOTIFY_WINDOW_END_HOUR


def _movie_notification_stub(card: dict) -> dict:
    """Minimal snapshot of a card for pending-notification storage."""
    return {
        "title": card.get("title") or "",
        "alt_title": card.get("alt_title") or "",
        "year": card.get("year"),
        "rating": card.get("rating"),
    }


def _merge_notification_stubs(existing: list, new: list) -> list:
    """Merge two stub lists, deduplicating by title+year. Preserves insertion order."""
    seen: set[str] = {f"{c.get('title')}|{c.get('year')}" for c in existing}
    merged = list(existing)
    for stub in new:
        key = f"{stub.get('title')}|{stub.get('year')}"
        if key not in seen:
            seen.add(key)
            merged.append(stub)
    return merged


async def _send_movie_notification_push(cards: list, subs: dict, app: "Application") -> None:
    """Format and send a /new notification to all subscribers."""
    import html as _html

    lines = ["🎬 <b>Новые фильмы в /new:</b>", ""]
    for card in cards[:5]:
        title_str = _html.escape(str(card.get("title") or ""))
        alt = card.get("alt_title") or ""
        if alt:
            title_str = f"{title_str} / {_html.escape(str(alt))}"
        year = card.get("year") or ""
        rating = card.get("rating")
        rating_text = f" · КП {rating:.1f}" if isinstance(rating, (int, float)) else ""
        lines.append(f"• {title_str} ({year}){rating_text}")
    if len(cards) > 5:
        lines.append(f"и ещё {len(cards) - 5}…")

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Открыть /new", callback_data="new:open"),
        InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:new_unsub"),
    ]])

    for chat_id_str in list(subs.keys()):
        try:
            await app.bot.send_message(
                chat_id=int(chat_id_str),
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info("Sent /new notification to chat_id=%s (%d films)", chat_id_str, len(cards))
        except Exception as exc:
            logger.warning("Failed to send /new notification to %s: %s", chat_id_str, exc)


async def _run_movie_discovery_notifications(cache: dict, app: "Application") -> None:
    """Send push notifications to /new subscribers about newly appeared top-10 films.

    Respects the quiet-hours window (_NOTIFY_WINDOW_START_HOUR – _NOTIFY_WINDOW_END_HOUR).
    Outside the window, new-film stubs are saved to ``movie_notify_pending`` in settings and
    delivered the next time the window opens (see ``_flush_pending_movie_notifications``).
    """
    top_cards = (cache.get("cards") or [])[:10]
    settings = _load_movie_discovery_settings()
    last_run_at: str = settings.get("movie_notify_last_run_at") or ""

    # First run: baseline the timestamp so we don't spam with all existing films
    if not last_run_at:
        settings["movie_notify_last_run_at"] = (
            cache.get("updated_at") or datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        )
        _save_movie_discovery_settings(settings)
        return

    # Find cards whose first_seen_at is strictly after the last notification run.
    # Both timestamps use "%Y-%m-%d %H:%M" format so lexicographic comparison is safe.
    new_cards = [
        card for card in top_cards
        if str(card.get("first_seen_at") or "") > last_run_at
    ]

    # Always advance the marker so stale cards are never re-notified on the next run
    now_str = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    settings["movie_notify_last_run_at"] = now_str
    _save_movie_discovery_settings(settings)

    if not new_cards:
        return

    subs: dict = settings.get("movie_subscriptions") or {}
    if not subs:
        return

    if _is_in_notification_window():
        # Merge any previously deferred stubs with the freshly-found cards and send now.
        pending_stubs: list = settings.get("movie_notify_pending") or []
        new_stubs = [_movie_notification_stub(c) for c in new_cards]
        all_stubs = _merge_notification_stubs(pending_stubs, new_stubs)

        await _send_movie_notification_push(all_stubs, subs, app)

        # Clear the pending queue now that it has been flushed
        settings["movie_notify_pending"] = []
        _save_movie_discovery_settings(settings)
    else:
        # Outside quiet hours: queue stubs for the next window opening
        pending_stubs = settings.get("movie_notify_pending") or []
        new_stubs = [_movie_notification_stub(c) for c in new_cards]
        settings["movie_notify_pending"] = _merge_notification_stubs(pending_stubs, new_stubs)
        _save_movie_discovery_settings(settings)
        logger.debug(
            "Out of notification window — deferred %d new film(s) to pending queue",
            len(new_cards),
        )


async def _flush_pending_movie_notifications(app: "Application") -> None:
    """Deliver any pending /new notifications that were deferred outside quiet hours."""
    settings = _load_movie_discovery_settings()
    pending_stubs: list = settings.get("movie_notify_pending") or []
    if not pending_stubs:
        return

    subs: dict = settings.get("movie_subscriptions") or {}
    # Clear pending regardless — even if there are no subscribers — so stale entries
    # don't accumulate indefinitely.
    settings["movie_notify_pending"] = []
    _save_movie_discovery_settings(settings)

    if not subs:
        return

    await _send_movie_notification_push(pending_stubs, subs, app)


async def _movie_notification_pending_loop(app: "Application") -> None:
    """Background loop that wakes up at the start of the notification window (09:00 local)
    and flushes any /new notifications that were deferred during quiet hours.
    """
    try:
        while True:
            now = datetime.now(DISPLAY_TIMEZONE)
            # Target: next 09:00 in local time
            target = now.replace(
                hour=_NOTIFY_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
            )
            if target <= now:
                # Already past 09:00 today — aim for 09:00 tomorrow
                target += timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            logger.debug(
                "Pending notification loop: sleeping %.0fs until %s",
                sleep_seconds,
                target.strftime("%Y-%m-%d %H:%M"),
            )
            await asyncio.sleep(sleep_seconds)
            await _flush_pending_movie_notifications(app)
    except asyncio.CancelledError:
        logger.info("Movie notification pending loop stopped")
        raise


async def _movie_discovery_loop(app: "Application") -> None:
    if not _movie_discovery_enabled():
        logger.info("Movie discovery disabled")
        return

    interval = MOVIE_DISCOVERY_INTERVAL_HOURS * 3600

    async def _refresh_and_notify() -> None:
        cache = await _refresh_movie_discovery_cache()
        await _run_movie_discovery_notifications(cache, app)

    try:
        await _run_background_step("movie discovery refresh", _refresh_and_notify)
        while True:
            await asyncio.sleep(interval)
            await _run_background_step("movie discovery refresh", _refresh_and_notify)
    except asyncio.CancelledError:
        logger.info("Movie discovery loop stopped")
        raise


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    return _view_find_task(tasks, task_id)


# ---------------------------------------------------------------------------
# Plex library cache helpers
# ---------------------------------------------------------------------------

def _plex_cache_key(title: str, year: int) -> tuple[str, int]:
    return (_normalize_movie_title(title).lower(), year)


def _plex_library_find(title: str, year: int) -> "PlexMovie | None":
    """Look up a movie in the in-memory Plex cache with ±1 year tolerance.

    For ``year=0`` (unknown year, e.g. when extraction failed) we look up ONLY
    the bucket ``year=0`` rather than spreading the search across years -1, 0, 1,
    because that would silently match any movie whose Plex year is also missing.
    """
    norm = _normalize_movie_title(title).lower()
    if not norm:
        return None
    if year == 0:
        # Unknown year: only consider entries that are also year=0 in Plex.
        return _plex_library.get((norm, 0))
    for dy in (0, 1, -1):
        hit = _plex_library.get((norm, year + dy))
        if hit is not None:
            return hit
    return None


def _plex_show_find(series_query: str, year: int = 0) -> "PlexShow | None":
    """Look up a TV show in the in-memory Plex shows cache.

    Mirrors :func:`_plex_library_find` but for shows. When ``year > 0`` uses
    ±1-year tolerance via direct dict lookup. When ``year == 0`` (unknown),
    falls back to a linear scan by normalised title across all years —
    necessary because for series the user often doesn't supply an air-year.
    """
    norm = _normalize_movie_title(series_query).lower()
    if not norm:
        return None
    if year > 0:
        for dy in (0, 1, -1):
            hit = _plex_shows_library.get((norm, year + dy))
            if hit is not None:
                return hit
        return None
    # No year known — pick the first show matching by normalised title.
    for (cached_title, _cached_year), show in _plex_shows_library.items():
        if cached_title == norm:
            return show
    return None


async def _plex_ensure_show_seasons(show: "PlexShow") -> dict[int, "PlexSeason"]:
    """Lazily populate ``show.seasons`` via :meth:`PlexClient.get_show_seasons`.

    First call hits the network (two HTTP requests per show: seasons +
    episode files). Subsequent calls reuse the cached dict on the show
    instance. Returns an empty dict on any failure.
    """
    if show.seasons:
        return show.seasons
    if plex_client is None:
        return {}
    try:
        seasons = await asyncio.to_thread(plex_client.get_show_seasons, show.rating_key)
    except Exception as exc:
        logger.debug("Plex show seasons fetch failed for %r: %s", show.title, exc)
        return {}
    if seasons:
        show.seasons = seasons
    return seasons


def _plex_cache_info() -> dict:
    """Return metadata dict for diagnostics, including health state."""
    import time, datetime

    def _fmt(ts: float) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""

    return {
        "count": len(_plex_library),
        "show_count": len(_plex_shows_library),
        "updated_at": _fmt(_plex_library_updated_at),
        "shows_updated_at": _fmt(_plex_shows_updated_at),
        "last_error_kind": _plex_last_error_kind,
        "last_error_message": _plex_last_error_message,
        "last_error_at": _fmt(_plex_last_error_at),
        "last_success_at": _fmt(_plex_last_success_at),
        "consecutive_failures": _plex_consecutive_failures,
    }


def _classify_plex_exception(exc: BaseException) -> tuple[str, str]:
    """Return (error_kind, short_message) for an exception raised during a Plex call."""
    if isinstance(exc, PlexAPIError):
        return exc.error_kind, str(exc) or exc.__class__.__name__
    # Fallback for any unexpected exception type
    return "other", f"{exc.__class__.__name__}: {exc}"


def _record_plex_failure(error_kind: str, message: str) -> None:
    global _plex_last_error_kind, _plex_last_error_message, _plex_last_error_at, _plex_consecutive_failures
    import time
    _plex_last_error_kind = error_kind
    _plex_last_error_message = message
    _plex_last_error_at = time.time()
    _plex_consecutive_failures += 1


def _record_plex_success() -> None:
    global _plex_last_error_kind, _plex_last_error_message, _plex_last_success_at, _plex_consecutive_failures
    import time
    _plex_last_error_kind = ""
    _plex_last_error_message = ""
    _plex_last_success_at = time.time()
    _plex_consecutive_failures = 0


async def _refresh_plex_library() -> None:
    """Fetch all movies from Plex and rebuild the in-memory cache.

    Single-flight: serialised by ``_plex_refresh_lock`` so concurrent callers
    (the 30-min background loop and N polling loops) don't bombard Plex with
    parallel ``get_all_movies`` calls. If a refresh completed less than
    ``_PLEX_REFRESH_COALESCE_SECONDS`` ago, subsequent callers skip the actual
    fetch and reuse the freshly-loaded cache.
    """
    global _plex_library, _plex_library_updated_at, _plex_machine_id, _plex_refresh_lock
    import time

    if plex_client is None:
        return

    # Create the lock lazily on the active event loop (avoids "got Future attached
    # to a different loop" issues in tests that recreate the loop).
    if _plex_refresh_lock is None:
        _plex_refresh_lock = asyncio.Lock()

    async with _plex_refresh_lock:
        # Coalesce: if another caller just refreshed the cache, reuse it.
        now = time.time()
        if _plex_library_updated_at and now - _plex_library_updated_at < _PLEX_REFRESH_COALESCE_SECONDS:
            return

        try:
            movies = await asyncio.to_thread(plex_client.get_all_movies)
        except Exception as exc:
            kind, msg = _classify_plex_exception(exc)
            _record_plex_failure(kind, msg)
            logger.warning(
                "Plex library refresh failed (kind=%s): %s",
                kind,
                msg,
                exc_info=isinstance(exc, PlexAPIError) is False,  # only full trace for unexpected types
            )
            return

        new_cache: dict[tuple[str, int], PlexMovie] = {}
        for movie in movies:
            key = _plex_cache_key(movie.title, movie.year)
            new_cache[key] = movie

        _plex_library = new_cache
        _plex_library_updated_at = time.time()
        _record_plex_success()
        logger.debug("Plex library cache refreshed: %d movies", len(_plex_library))

        # Fetch machine ID — retry on every refresh while empty (lightweight call).
        if not _plex_machine_id:
            try:
                _plex_machine_id = await asyncio.to_thread(plex_client.get_machine_id)
                if _plex_machine_id:
                    logger.info("Plex machine ID: %s", _plex_machine_id)
            except Exception as exc:
                # Non-fatal — deep-link button won't appear until next successful fetch.
                logger.debug("Failed to fetch Plex machine ID: %s", exc)

        # Refresh TV shows cache. Non-fatal: a Plex instance without a 'show'
        # section will simply return an empty list — keep the old cache intact
        # only on hard failure, otherwise overwrite (even with []).
        global _plex_shows_library, _plex_shows_updated_at
        try:
            shows = await asyncio.to_thread(plex_client.get_all_shows)
        except Exception as exc:
            # Don't touch the existing cache — show refresh failures shouldn't
            # mask earlier successful state.
            logger.debug("Plex shows refresh skipped: %s", exc)
        else:
            new_shows_cache: dict[tuple[str, int], PlexShow] = {}
            for show in shows:
                key = _plex_cache_key(show.title, show.year)
                new_shows_cache[key] = show
            _plex_shows_library = new_shows_cache
            _plex_shows_updated_at = time.time()
            logger.debug("Plex shows cache refreshed: %d shows", len(_plex_shows_library))


async def _plex_cache_loop() -> None:
    if plex_client is None:
        logger.info("Plex not configured — cache loop disabled")
        return
    try:
        await _run_background_step("initial Plex library cache", _refresh_plex_library)
        while True:
            await asyncio.sleep(_PLEX_CACHE_INTERVAL)
            await _run_background_step("Plex library cache refresh", _refresh_plex_library)
    except asyncio.CancelledError:
        logger.info("Plex cache loop stopped")
        raise


def _enrich_cards_with_plex(cards: list[dict]) -> None:
    """Add ``in_plex`` and ``plex_resolution`` fields to each card in-place.

    Checks both ``title`` (Russian) and ``alt_title`` (English).
    No-op when Plex is not configured or the cache is empty.
    """
    if not PLEX_ENABLED or not _plex_library:
        return
    for card in cards:
        year = int(card.get("year") or 0)
        match = _plex_library_find(str(card.get("title") or ""), year)
        if match is None and card.get("alt_title"):
            match = _plex_library_find(str(card["alt_title"]), year)
        card["in_plex"] = match is not None
        card["plex_resolution"] = match.resolution if match else None


# Accepts "S01E02", "1x02", "Сезон 3", "Сезон: 3", "Сезон:3", "СЕЗОН 3" — the
# colon-form is the most common on Rutracker. Case-insensitive matching is
# applied via re.IGNORECASE; the literal "сезон" is enough since the flag
# covers Latin/Cyrillic case mixing.
_SERIES_RE = re.compile(r"s\d+e\d+|\d+x\d+|сезон[:\s]+\d+", re.IGNORECASE)


def _plex_is_series(title: str) -> bool:
    """Return True if *title* looks like a TV series episode (skip Plex check for those)."""
    return bool(_SERIES_RE.search(title))


def _plex_quality_from_result(result: dict) -> str:
    """Return a Plex-normalised quality string from a search result dict."""
    q = result.get("quality") or ""
    if q:
        return _plex_normalise_resolution(q)
    return _plex_normalise_resolution(_movie_detect_quality(result.get("title") or "") or "")


def _plex_quality_from_title(title: str) -> str:
    """Return a Plex-normalised quality string extracted from a raw file/torrent name."""
    return _plex_normalise_resolution(_movie_detect_quality(title) or "")


def _plex_pre_check(title: str, year: int, requested_quality: str) -> "PlexCheckResult | None":
    """Return a PlexCheckResult when a Plex duplicate is found, else None.

    Returns None when:
    - Plex integration is disabled
    - The Plex library cache is empty (not yet loaded)
    - The title looks like a TV series
    - The movie is not found in the Plex cache
    - The requested quality couldn't be determined (we cannot compare → no warning)
    """
    if not PLEX_ENABLED or not _plex_library:
        return None
    if _plex_is_series(title):
        return None
    if not requested_quality:
        # Without a known target quality we cannot decide between same/better/upgrade.
        # Silently skip the pre-check so the user isn't shown a misleading
        # "same quality" warning based on the unknown-vs-unknown comparison.
        return None
    match = _plex_library_find(title, year)
    if match is None:
        return None
    return _plex_check_before_download(match, requested_quality)


async def _plex_pre_check_series(
    series_query: str,
    season_num: int | None,
    requested_quality: str,
) -> "PlexSeriesCheckResult | None":
    """Return a PlexSeriesCheckResult when a season is already in Plex, else None.

    Mirrors :func:`_plex_pre_check` but for TV seasons. Returns None when:
      • Plex is disabled or the shows cache is empty
      • series_query/season_num are missing or invalid
      • requested_quality is unknown (can't compare → no warning)
      • The show isn't in Plex, or the show is there but this season isn't
    """
    if not PLEX_ENABLED or not _plex_shows_library:
        return None
    if not series_query or not season_num or season_num <= 0:
        return None
    if not requested_quality:
        return None
    show = _plex_show_find(series_query)
    if show is None:
        return None
    seasons = await _plex_ensure_show_seasons(show)
    season = seasons.get(season_num)
    if season is None:
        return None
    return _plex_check_before_download_season(show, season, requested_quality)


def _plex_find_by_ds_title(ds_title: str) -> "PlexMovie | None":
    """Find a Plex movie whose file path has *ds_title* as a complete component.

    A "complete component" means the DS task title equals either:
      • a full folder/file name in the path (between separators), OR
      • the same name with its file extension stripped.

    This avoids the substring-collision pitfall of naïve ``name in fp``
    matching — e.g. a DS title ``Movie.2024`` no longer matches
    ``/archive/Movie.2024.backup/whatever.mkv``.

    Uses the global in-memory library; no network call.
    Returns the first matching PlexMovie or None.
    """
    name = ds_title.strip()
    if not name:
        return None
    # Snapshot the dict ref to avoid mid-iteration replacement by a concurrent refresh.
    library_snapshot = _plex_library
    for movie in library_snapshot.values():
        for fp in movie.file_paths:
            # Split on both POSIX and Windows separators so behaviour is the same
            # whether Plex is on Linux/Synology or running on Windows.
            parts = [p for p in re.split(r"[\\/]", fp) if p]
            if not parts:
                continue
            # Full-component match anywhere in the path (folder or file name).
            if name in parts:
                return movie
            # Also allow matching the filename with extension stripped — but ONLY
            # for the last component (the file), since folder names like
            # "Movie.2024.backup" must not be reduced to "Movie.2024".
            file_part = parts[-1]
            file_stem = re.sub(r"\.[^.]+$", "", file_part)
            if name == file_stem:
                return movie
    return None


async def _plex_poll_lookup_target(task_title: str, meta: dict | None) -> tuple[object, str, str]:
    """Attempt to locate the just-finished task in the Plex library.

    Returns a tuple ``(target, metadata_type, found_title)``:
      • ``target`` — the matched PlexMovie or PlexSeason (has ``.rating_key``), or None.
      • ``metadata_type`` — Plex API content type for the deep link
        (``"1"`` for movie, ``"3"`` for season).
      • ``found_title`` — human-readable label for the notification text
        (movie title or 'Сезон N «Show»').

    Match strategy:
      • If ``meta`` says ``kind=="series"`` — look up the show, ensure its
        seasons are cached, return ``seasons[meta["season_num"]]``.
      • If ``meta`` says ``kind=="movie"`` — try canonical title+year lookup
        first, then fall back to ``_plex_find_by_ds_title`` substring match.
      • If ``meta`` is None — legacy path: substring match by ``task_title``,
        then fall back to extracted title+year.
    """
    found_title = task_title

    if meta and meta.get("kind") == "series":
        series_query = str(meta.get("series_query") or "").strip()
        try:
            season_num = int(meta.get("season_num") or -1)
        except (TypeError, ValueError):
            season_num = -1
        if series_query and season_num > 0:
            show = _plex_show_find(series_query, int(meta.get("year") or 0))
            if show is not None:
                seasons = await _plex_ensure_show_seasons(show)
                season = seasons.get(season_num)
                if season is not None:
                    found_title = f"Сезон {season_num} «{show.title or series_query}»"
                    return season, "3", found_title
        # Series meta is present but couldn't find a match — no fallback to movies.
        return None, "1", found_title

    # Movie path — either explicit kind="movie" or legacy (meta is None).
    if meta:
        canonical_title = str(meta.get("title") or "").strip()
        year = int(meta.get("year") or 0)
        if canonical_title:
            hit = _plex_library_find(canonical_title, year)
            if hit is not None:
                return hit, "1", hit.title or canonical_title

    # Substring match against file paths (handles the case where the bot's meta is
    # missing or the title doesn't match Plex's canonical naming).
    hit = _plex_find_by_ds_title(task_title)
    if hit is not None:
        return hit, "1", hit.title or task_title

    # Last-ditch: try to extract title+year from the raw DS task title
    # (covers legacy tasks created before task_meta was introduced).
    year = _movie_extract_year(task_title) or 0
    fallback_title = re.sub(r"[_.]+", " ", task_title)
    fallback_title = re.sub(r"\s{2,}", " ", fallback_title).strip()
    if fallback_title:
        hit = _plex_library_find(fallback_title, year)
        if hit is not None:
            return hit, "1", hit.title or task_title

    return None, "1", found_title


async def _plex_poll_after_finish(
    app: "Application",
    task_id: str,
    task_title: str,
    chat_ids: list[int],
    *,
    meta: dict | None = None,
    hint_msg_ids: dict[int, int] | None = None,
    max_attempts: int = 20,
    interval_seconds: float = 30.0,
) -> None:
    """Poll Plex after a DS task finishes. Sends a push notification when found.

    Polls every *interval_seconds* for up to *max_attempts* (default: 10 min).
    Sends a NEW message (not edit) so iOS users get a push notification.
    When done (found or timeout), deletes the hint messages sent earlier so the
    chat doesn't accumulate stale «indexing…» banners.

    When *meta* (from ``_get_task_meta``) is provided, it routes the lookup:
      • ``kind="series"`` → builds a season deep link (metadataType=3).
      • ``kind="movie"`` → canonical (title, year) lookup with substring fallback.
      • ``None`` → legacy substring path, with a best-effort series-detection
        fallback so old tasks without meta still get a chance.
    """
    async def _delete_hint_messages() -> None:
        for cid, mid in (hint_msg_ids or {}).items():
            try:
                await app.bot.delete_message(chat_id=cid, message_id=mid)
            except Exception:
                pass

    # Backfill meta from the task title for legacy tasks that pre-date task_meta.json.
    if meta is None and _plex_is_series(task_title):
        meta = _build_task_meta_from_title(task_title, source="legacy_ds")

    logger.info(
        "Plex polling started task_id=%s title=%r kind=%s chat_ids=%s",
        task_id, task_title, (meta or {}).get("kind", "movie"), chat_ids,
    )
    # Track whether at least one refresh succeeded so we can distinguish
    # "movie genuinely not in Plex" from "Plex was unreachable the whole time".
    refresh_succeeded_at_least_once = False
    try:
        for attempt in range(max_attempts):
            if attempt > 0:
                await asyncio.sleep(interval_seconds)

            # Refresh Plex library, then look for the file
            await _refresh_plex_library()
            # Heuristic: if the global failure counter is at 0,
            # this refresh succeeded.
            if _plex_consecutive_failures == 0:
                refresh_succeeded_at_least_once = True
            target, metadata_type, found_title = await _plex_poll_lookup_target(task_title, meta)

            if target is not None:
                # Build deep link
                machine_id = _plex_machine_id
                rating_key = getattr(target, "rating_key", "")
                deep_link = (
                    f"plex://preplay/?metadataKey=/library/metadata/{rating_key}"
                    f"&metadataType={metadata_type}&server={machine_id}"
                ) if machine_id and rating_key else ""

                text = f"✅ <b>{html_module.escape(found_title)}</b> добавлен в Plex."
                keyboard = (
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("▶️ Смотреть в Plex (iOS)", url=deep_link)
                    ]])
                    if deep_link else None
                )
                await _delete_hint_messages()
                for cid in chat_ids:
                    try:
                        await app.bot.send_message(
                            chat_id=cid,
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                    except Exception:
                        logger.warning(
                            "Plex poll: failed to send found-notification chat_id=%s", cid, exc_info=True
                        )
                logger.info(
                    "Plex polling: found %r after %d attempt(s)", found_title, attempt + 1
                )
                return

        # Exhausted all attempts — choose message based on whether we ever reached Plex.
        timeout_min = int(max_attempts * interval_seconds // 60)
        title_esc = html_module.escape(task_title)
        if refresh_succeeded_at_least_once:
            text = (
                f"⚠️ <b>{title_esc}</b> скачан, "
                f"но не появился в Plex за {timeout_min} мин."
            )
            log_reason = "not found"
        else:
            text = (
                f"⚠️ <b>{title_esc}</b> скачан, но проверить Plex не удалось — "
                f"сервер был недоступен. Файл, возможно, уже в библиотеке."
            )
            log_reason = "Plex unreachable"
        await _delete_hint_messages()
        for cid in chat_ids:
            try:
                await app.bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
            except Exception:
                logger.warning(
                    "Plex poll: failed to send timeout-notification chat_id=%s", cid, exc_info=True
                )
        logger.info(
            "Plex polling: gave up on %r after %d attempt(s) — reason=%s",
            task_title, max_attempts, log_reason,
        )
    except asyncio.CancelledError:
        await _delete_hint_messages()
        logger.info("Plex polling task cancelled for task_id=%s", task_id)
        raise
    finally:
        _PLEX_POLLING_TASKS[task_id] = None  # Mark as done; key stays to prevent re-launch
        _mark_plex_poll_done(task_id)  # Persist so restart doesn't re-launch polling


def _plex_confirm_text(check: "PlexCheckResult", display_title: str, requested_quality: str) -> str:
    """Format the pre-download Plex warning message (HTML)."""
    plex_res = check.plex_movie.resolution
    plex_res_display = plex_res.upper() if plex_res else "неизвестное качество"
    title_esc = html_module.escape(display_title)

    if check.action == "warn_same":
        verb = f"уже есть в Plex ({plex_res_display})"
    elif check.action == "warn_better":
        req_display = requested_quality.upper() if requested_quality else "неизвестное качество"
        verb = f"уже есть в Plex в лучшем качестве ({plex_res_display} &gt; {req_display})"
    else:  # offer_upgrade
        req_display = requested_quality.upper() if requested_quality else "неизвестное качество"
        verb = f"есть в Plex в худшем качестве ({plex_res_display}), запрошено {req_display}"

    return (
        f"⚠️ <b>{title_esc}</b> {verb}.\n"
        "Скачать всё равно?"
    )


def _plex_series_confirm_text(
    check: "PlexSeriesCheckResult",
    display_title: str,
    requested_quality: str,
) -> str:
    """Format the pre-download Plex warning for a TV season (HTML)."""
    plex_res = check.season.resolution
    plex_res_display = plex_res.upper() if plex_res else "неизвестное качество"
    show_title_esc = html_module.escape(check.show.title or "")
    season_num = check.season.season_number
    title_esc = html_module.escape(display_title)

    head = (
        f"Сезон {season_num} «{show_title_esc}»"
        if show_title_esc
        else f"Сезон {season_num}"
    )

    if check.action == "warn_same":
        verb = f"уже есть в Plex ({plex_res_display})"
    elif check.action == "warn_better":
        req_display = requested_quality.upper() if requested_quality else "неизвестное качество"
        verb = f"уже есть в Plex в лучшем качестве ({plex_res_display} &gt; {req_display})"
    else:  # offer_upgrade
        req_display = requested_quality.upper() if requested_quality else "неизвестное качество"
        verb = f"есть в Plex в худшем качестве ({plex_res_display}), запрошено {req_display}"

    return (
        f"⚠️ <b>{head}</b> {verb}.\n"
        f"<i>Из раздачи: {title_esc}</i>\n"
        "Скачать всё равно?"
    )


def _make_task_keyboard(task_id: str, status: str = "", task_type: str = "") -> InlineKeyboardMarkup:
    """Bot-level wrapper: injects tracker-button visibility state into the stateless _task_keyboard."""
    return _task_keyboard(
        task_id, status, task_type,
        show_trackers=_tracker_button_visible(task_id, status, task_type),
    )


def _notification_keyboard(task_id: str, status: str = "", task_type: str = "") -> InlineKeyboardMarkup:
    if (status or "").lower() in {"finished", "seeding"}:
        # Use plex:// scheme so the button opens the iOS/Android Plex app directly
        # rather than an HTTP URL that opens in the browser.  The specific deep-link
        # (plex://preplay/…) is sent later by _plex_poll_after_finish once Plex
        # has indexed the downloaded file.
        return _final_notification_keyboard(task_id, show_plex=PLEX_ENABLED, plex_url="plex://")

    return _make_task_keyboard(task_id, status, task_type)


def _format_task_card(task: dict) -> str:
    return _view_format_task_card(task)


def _load_task_owners() -> dict[str, int]:
    return state_store.load_task_owners()


def _save_task_owners(owners: dict[str, int]) -> None:
    state_store.save_task_owners(owners)


def _remember_task_owner(task_id: str, chat_id: int | None) -> None:
    state_store.remember_task_owner(task_id, chat_id)


def _load_task_meta() -> dict[str, dict]:
    return state_store.load_task_meta()


def _save_task_meta(meta: dict[str, dict]) -> None:
    state_store.save_task_meta(meta)


def _get_task_meta(task_id: str) -> dict | None:
    """Return canonical metadata captured at task creation, or None."""
    if not task_id:
        return None
    return _load_task_meta().get(task_id)


def _remember_task_meta(task_id: str, entry: dict | None) -> None:
    """Persist canonical metadata for a DS task.

    ``entry`` should be built via :func:`_build_task_meta_from_result` or
    :func:`_build_task_meta_from_title`. Silently no-ops on falsy task_id /
    entry so callers can pass through optional values.
    """
    if not task_id or not entry:
        return
    state_store.remember_task_meta(task_id, entry)


def _build_task_meta_from_result(result: dict, source: str = "search") -> dict:
    """Build canonical task metadata from a search result dict.

    Uses ``movie_title`` (KP-normalised, when KP enrichment ran) or falls back
    to the raw release ``title``. Detects whether the release is a TV series
    via :func:`_plex_is_series` and extracts ``series_query``+``season_num``
    when applicable. Quality is normalised via :func:`_plex_quality_from_result`.
    """
    raw_title = result.get("movie_title") or result.get("title") or ""
    quality = _plex_quality_from_result(result)
    try:
        year = int(result.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    if not year:
        year = _movie_extract_year(raw_title) or 0

    if _plex_is_series(raw_title):
        season_num = _extract_season_from_query(raw_title) or -1
        series_query = _extract_series_base_query(raw_title) or ""
        return {
            "kind": "series",
            "title": raw_title,
            "year": year,
            "quality": quality,
            "series_query": series_query,
            "season_num": season_num,
            "source": source,
        }
    return {
        "kind": "movie",
        "title": raw_title,
        "year": year,
        "quality": quality,
        "source": source,
    }


def _normalize_torrent_filename_for_match(safe_name: str) -> str:
    """Convert a `safe_filename` form (underscores, dots, `.torrent` suffix) into
    a human-readable title suitable for Plex / season detection.

    Example: ``"Klinika_Sezon_3_1080p.torrent"`` → ``"Klinika Sezon 3 1080p"``.
    """
    stripped = safe_name.removesuffix(".torrent")
    cleaned = re.sub(r"[_.]+", " ", stripped)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _extract_magnet_dn(magnet_uri: str) -> str:
    """Return the URL-decoded ``dn=`` parameter from a magnet URI, or empty string."""
    import urllib.parse
    try:
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(magnet_uri).query))
        return urllib.parse.unquote_plus(qs.get("dn", ""))
    except Exception:
        return ""


def _build_task_meta_from_title(title: str, *, source: str) -> dict:
    """Build canonical task metadata from a plain title string.

    Used by direct ``.torrent`` and magnet flows where the only signal is the
    filename / ``dn=`` parameter. Detects series via :func:`_plex_is_series`
    and falls back to ``_movie_extract_year`` for year detection.
    """
    quality = _plex_quality_from_title(title)
    year = _movie_extract_year(title) or 0

    if _plex_is_series(title):
        season_num = _extract_season_from_query(title) or -1
        series_query = _extract_series_base_query(title) or ""
        return {
            "kind": "series",
            "title": title,
            "year": year,
            "quality": quality,
            "series_query": series_query,
            "season_num": season_num,
            "source": source,
        }
    return {
        "kind": "movie",
        "title": title,
        "year": year,
        "quality": quality,
        "source": source,
    }


def _load_notified_tasks() -> dict[str, object]:
    return state_store.load_notified_tasks()


def _save_notified_tasks(tasks: dict[str, object]) -> None:
    state_store.save_notified_tasks(tasks)


def _mark_plex_poll_done(task_id: str) -> None:
    """Persist a plex_done marker so polling is not restarted after a bot restart."""
    notified = _load_notified_tasks()
    raw = notified.get(task_id)
    if isinstance(raw, dict):
        raw["plex_done"] = True
    else:
        # No existing entry yet — create a minimal one just to hold the marker.
        notified[task_id] = {"status": "", "sent": [], "failures": {}, "plex_done": True}
    _save_notified_tasks(notified)


def _plex_poll_is_done(task_id: str, notified: dict) -> bool:
    """Return True if Plex polling already completed for *task_id* (persisted marker)."""
    raw = notified.get(task_id)
    return isinstance(raw, dict) and bool(raw.get("plex_done"))


def _load_auto_delete_tasks() -> dict[str, float]:
    return state_store.load_auto_delete_tasks()


def _save_auto_delete_tasks(tasks: dict[str, float]) -> None:
    state_store.save_auto_delete_tasks(tasks)


def _forget_task_state(task_ids: list[str]) -> None:
    state_store.forget_task_state(task_ids)
    for task_id in task_ids:
        for chat_id, message_id in TASK_CARD_MESSAGES.pop(str(task_id), set()):
            _cancel_task_card_refresh(chat_id, message_id)


def _revoke_chat_runtime_state(chat_id: int) -> None:
    owners = _load_task_owners()
    revoked_task_ids = [task_id for task_id, owner in owners.items() if owner == chat_id]
    if revoked_task_ids:
        for task_id in revoked_task_ids:
            owners.pop(task_id, None)
        _save_task_owners(owners)

    subs = state_store.load_topic_subscriptions()
    revoked_subs = [key for key, sub in subs.items() if sub.get("chat_id") == chat_id]
    if revoked_subs:
        for key in revoked_subs:
            subs.pop(key, None)
        state_store.save_topic_subscriptions(subs)

    DOWNLOAD_PANEL_MESSAGES.pop(chat_id, None)
    DOWNLOAD_PANEL_PAGES.pop(chat_id, None)
    DOWNLOAD_PANEL_SCOPES.pop(chat_id, None)
    DOWNLOAD_PANEL_HAD_ACTIVE.pop(chat_id, None)

    for key in list(TASK_CARD_REFRESH_TASKS):
        card_chat_id, message_id = key
        if card_chat_id == chat_id:
            _cancel_task_card_refresh(card_chat_id, message_id)

    for task_id, messages in list(TASK_CARD_MESSAGES.items()):
        remaining = {message for message in messages if message[0] != chat_id}
        if remaining:
            TASK_CARD_MESSAGES[task_id] = remaining
        else:
            TASK_CARD_MESSAGES.pop(task_id, None)


def _explicit_notification_chat_ids() -> set[int]:
    return parse_chat_ids(NOTIFY_CHAT_IDS_RAW)


def _notification_recipients(task_id: str) -> set[int]:
    return _policy_notification_recipients(
        task_id,
        explicit_chat_ids=_explicit_notification_chat_ids(),
        task_owners=_load_task_owners(),
        notify_external_tasks=TASK_NOTIFY_EXTERNAL_TASKS,
        fallback_chat_ids=_all_allowed_chat_ids(),
        allowed_chat_ids=_all_allowed_chat_ids(),
    )


def _notification_status_key(status: str) -> str:
    return _policy_notification_status_key(status)


def _auto_delete_notice(status: str) -> str:
    return _policy_auto_delete_notice(
        status,
        enabled=_auto_delete_finished_enabled(),
        finished_statuses=AUTO_DELETE_FINISHED_STATUSES,
        delete_after_hours=AUTO_DELETE_FINISHED_AFTER_HOURS,
    )


def _format_task_notification(task: dict) -> str:
    return _policy_format_task_notification(
        task,
        auto_delete_enabled=_auto_delete_finished_enabled(),
        auto_delete_statuses=AUTO_DELETE_FINISHED_STATUSES,
        auto_delete_after_hours=AUTO_DELETE_FINISHED_AFTER_HOURS,
    )


def _notification_delivery_state(raw_state: object, notification_key: str) -> tuple[set[str], dict[str, int], bool]:
    if raw_state == notification_key:
        return set(), {}, True

    if not isinstance(raw_state, dict) or raw_state.get("status") != notification_key:
        return set(), {}, False

    sent = {str(chat_id) for chat_id in raw_state.get("sent", []) if chat_id}
    failures = {}
    raw_failures = raw_state.get("failures", {})
    if isinstance(raw_failures, dict):
        for chat_id, count in raw_failures.items():
            try:
                failures[str(chat_id)] = max(0, int(count))
            except (TypeError, ValueError):
                continue

    return sent, failures, False


def _make_notification_delivery_state(
    notification_key: str,
    sent: set[str],
    failures: dict[str, int],
) -> dict:
    return {
        "status": notification_key,
        "sent": sorted(sent),
        "failures": {
            chat_id: count
            for chat_id, count in sorted(failures.items())
            if count > 0
        },
    }


_RU_MONTHS = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


def _format_unix_ts(unix_ts: int | None) -> str:
    """Format a Unix timestamp as 'D Mon в HH:MM' in the display timezone."""
    if not unix_ts:
        return ""
    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=DISPLAY_TIMEZONE)
        return f"{dt.day} {_RU_MONTHS[dt.month]} в {dt.strftime('%H:%M')}"
    except Exception:
        return ""


async def _handle_duplicate_task(
    app: Application,
    dup_task: dict,
    all_tasks: list[dict],
) -> None:
    """Handle a task that DS rejected as torrent_duplicate.

    Finds the original task already in DS, auto-deletes the rejected duplicate,
    and sends a context-aware notification to the user who tried to add it.
    """
    dup_id = task_id = dup_task.get("id", "")
    dup_title = dup_task.get("title", "")
    owner_id = (_load_task_owners() or {}).get(dup_id)

    # Find the original task — same title, different ID, not itself an error.
    original = next(
        (t for t in all_tasks
         if t.get("title") == dup_title
         and t.get("id") != dup_id
         and (t.get("status") or "").lower() != "error"),
        None,
    )

    # Auto-delete the duplicate task so it doesn't clutter DS.
    try:
        await asyncio.to_thread(ds_client.delete_task, dup_id)
        logger.info("Auto-deleted duplicate task %s (%s)", dup_id, dup_title)
    except Exception:
        logger.warning("Failed to auto-delete duplicate task %s", dup_id, exc_info=True)

    if not owner_id:
        return

    # Build notification text and keyboard based on original task state.
    if original:
        orig_id = original.get("id", "")
        orig_status = (original.get("status") or "").lower()
        orig_title = original.get("title", dup_title)
        transfer = original.get("additional", {}).get("transfer", {})
        detail = original.get("additional", {}).get("detail", {})

        # Owner and date of the original.
        orig_owner_id = (_load_task_owners() or {}).get(orig_id)
        orig_owner_name = ""
        if orig_owner_id and orig_owner_id != owner_id:
            users = state_store.load_approved_users()
            orig_owner_name = (users.get(orig_owner_id) or {}).get("name", "")
        added_at = _format_unix_ts(detail.get("create_time"))

        meta_parts = []
        if orig_owner_name:
            meta_parts.append(f"👤 {orig_owner_name}")
        if added_at:
            meta_parts.append(f"🕐 {added_at}")
        meta_line = "  •  ".join(meta_parts)

        if orig_status in {"finished", "seeding"}:
            size_str = _format_size(original.get("size"))
            status_str = "раздаётся" if orig_status == "seeding" else "скачан"
            text = (
                f"✅ Этот файл уже загружен\n\n"
                f"🎬 {orig_title}\n"
                f"📦 {size_str}  •  {status_str}"
            )
            if meta_line:
                text += f"\n{meta_line}"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Открыть задачу", callback_data=_task_callback("info", orig_id))],
                [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))],
            ])

        elif orig_status in {"downloading", "waiting", "finishing", "hash_checking"}:
            downloaded = transfer.get("size_downloaded", 0)
            total = original.get("size") or 0
            percent = _progress_percent(downloaded, total)
            if percent is not None:
                progress_str = f"⬇️ {percent:.0f}%  ({_format_size(downloaded)} из {_format_size(total)})"
            else:
                progress_str = "⬇️ Скачивается…"
            text = (
                f"📌 Этот файл уже скачивается\n\n"
                f"🎬 {orig_title}\n"
                f"{progress_str}"
            )
            if meta_line:
                text += f"\n{meta_line}"
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📋 Открыть задачу", callback_data=_task_callback("info", orig_id)),
                    InlineKeyboardButton("🔔 Уведомить когда готово", callback_data=_task_callback("sub_notify", orig_id)),
                ],
                [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))],
            ])

        else:
            # paused / error / unknown
            status_str = _status_label(orig_status)
            text = (
                f"⚠️ Такой файл уже добавлен, но остановлен\n\n"
                f"🎬 {orig_title}\n"
                f"Статус: {status_str}"
            )
            if meta_line:
                text += f"\n{meta_line}"
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📋 Открыть задачу", callback_data=_task_callback("info", orig_id)),
                    InlineKeyboardButton("▶️ Запустить", callback_data=_task_callback("resume", orig_id)),
                ],
                [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))],
            ])
    else:
        # Original not found — it may have been deleted already.
        text = (
            f"📌 Такой торрент уже был добавлен ранее\n\n"
            f"🎬 {dup_title}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))],
        ])

    try:
        await app.bot.send_message(chat_id=owner_id, text=text, reply_markup=kb)
    except Exception:
        logger.warning("Failed to send duplicate notification to %s", owner_id, exc_info=True)


async def _run_task_notifications_once(app: Application) -> None:
    if not _task_notifications_enabled():
        return

    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError:
        logger.warning("Task notification scan failed to list tasks", exc_info=True)
        return

    notified = _load_notified_tasks()
    changed = False
    for task in tasks:
        task_id = task.get("id")
        status = (task.get("status") or "").lower()
        if not task_id or status not in TASK_NOTIFICATION_STATUSES:
            continue

        # Intercept torrent_duplicate errors before the normal notification flow.
        error_detail = task.get("additional", {}).get("detail", {}).get("error_detail", "")
        if status == "error" and error_detail == "torrent_duplicate":
            raw = notified.get(task_id)
            already_handled = (
                raw == "error:torrent_duplicate"
                or (isinstance(raw, dict) and raw.get("status") == "error:torrent_duplicate")
            )
            if not already_handled:
                await _handle_duplicate_task(app, task, tasks)
                owner_id = (_load_task_owners() or {}).get(task_id)
                sent: set[str] = {str(owner_id)} if owner_id else set()
                notified[task_id] = _make_notification_delivery_state(
                    "error:torrent_duplicate", sent, {}
                )
                changed = True
            continue

        notification_key = _notification_status_key(status)
        sent_recipients, failed_recipients, legacy_done = _notification_delivery_state(
            notified.get(task_id),
            notification_key,
        )
        if legacy_done:
            continue

        recipients = _notification_recipients(task_id)

        # Also notify any users who subscribed via "🔔 Уведомить когда готово".
        if status in {"finished", "seeding"}:
            raw_state = notified.get(task_id)
            if isinstance(raw_state, dict):
                for sub_id_str in raw_state.get("subscribers", []):
                    try:
                        recipients.add(int(sub_id_str))
                    except (TypeError, ValueError):
                        pass

        if not recipients:
            continue

        # Determine if Plex polling should start for this task. We must atomically
        # reserve the _PLEX_POLLING_TASKS slot BEFORE the first await below — otherwise
        # two overlapping _run_task_notifications_once() invocations could both see
        # `task_id not in _PLEX_POLLING_TASKS` and spawn duplicate polling tasks.
        # The check-and-reserve here runs synchronously (no awaits between them),
        # so it's safe under cooperative concurrency.
        # Series no longer blocked here — _plex_poll_after_finish branches on
        # task_meta and handles both kinds.
        plex_should_poll = (
            PLEX_ENABLED
            and status == "finished"
            and task_id not in _PLEX_POLLING_TASKS
            and not _plex_poll_is_done(task_id, notified)
        )
        if plex_should_poll:
            _PLEX_POLLING_TASKS[task_id] = None  # placeholder; real task assigned below

        task_changed = False
        plex_hint_msgs: dict[int, int] = {}
        for chat_id in sorted(recipients):
            recipient_key = str(chat_id)
            if recipient_key in sent_recipients:
                continue
            if failed_recipients.get(recipient_key, 0) >= MAX_TASK_NOTIFICATION_FAILURES:
                continue

            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=_format_task_notification(task),
                    reply_markup=_notification_keyboard(task_id, status, task.get("type", "")),
                )
                await _delete_task_card_messages(app, task_id, chat_id=chat_id)
                sent_recipients.add(recipient_key)
                failed_recipients.pop(recipient_key, None)
                task_changed = True
                # Send an indexing-in-progress hint so the user knows Plex polling started.
                if plex_should_poll:
                    try:
                        hint = await app.bot.send_message(
                            chat_id=chat_id,
                            text="🔄 Ищем файл в библиотеке Plex — пришлём ссылку как только появится.",
                        )
                        plex_hint_msgs[chat_id] = hint.message_id
                    except Exception:
                        pass
            except Exception:
                failure_count = failed_recipients.get(recipient_key, 0) + 1
                failed_recipients[recipient_key] = failure_count
                task_changed = True
                logger.warning(
                    "Failed to send task status notification chat_id=%s task_id=%s attempt=%s/%s",
                    chat_id,
                    task_id,
                    failure_count,
                    MAX_TASK_NOTIFICATION_FAILURES,
                    exc_info=True,
                )

        if task_changed:
            notified[task_id] = _make_notification_delivery_state(
                notification_key,
                sent_recipients,
                failed_recipients,
            )
            changed = True

        # Start Plex polling after sending notifications so hint_msg_ids are available.
        if plex_should_poll:
            task_meta = _get_task_meta(task_id)
            _PLEX_POLLING_TASKS[task_id] = asyncio.create_task(
                _plex_poll_after_finish(
                    app,
                    task_id,
                    task.get("title") or "",
                    sorted(recipients),
                    meta=task_meta,
                    hint_msg_ids=plex_hint_msgs,
                )
            )

    if changed:
        _save_notified_tasks(notified)


def _is_auto_delete_candidate(task: dict) -> bool:
    return _policy_is_auto_delete_candidate(task, AUTO_DELETE_FINISHED_STATUSES)


async def _run_auto_delete_finished_once() -> None:
    if not _auto_delete_finished_enabled():
        return

    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError:
        logger.warning("Auto-delete scan failed to list tasks", exc_info=True)
        return

    now = time.time()
    threshold_seconds = AUTO_DELETE_FINISHED_AFTER_HOURS * 3600
    known_task_ids = {str(task["id"]) for task in tasks if task.get("id")}
    watched = _load_auto_delete_tasks()
    changed = False
    task_ids_to_delete: list[str] = []

    for task in tasks:
        task_id = task.get("id")
        if not task_id:
            continue

        task_id = str(task_id)
        if not _is_auto_delete_candidate(task):
            if task_id in watched:
                watched.pop(task_id, None)
                changed = True
            continue

        first_seen_at = watched.get(task_id)
        if first_seen_at is None:
            watched[task_id] = now
            changed = True
            continue

        if now - first_seen_at >= threshold_seconds:
            task_ids_to_delete.append(task_id)

    for task_id in list(watched):
        if task_id not in known_task_ids:
            watched.pop(task_id, None)
            changed = True

    if task_ids_to_delete:
        try:
            await asyncio.to_thread(ds_client.delete_tasks, task_ids_to_delete)
        except DownloadStationError:
            logger.warning("Auto-delete failed for tasks %s", task_ids_to_delete, exc_info=True)
        else:
            logger.info("Auto-deleted finished tasks: %s", task_ids_to_delete)
            _forget_task_state(task_ids_to_delete)
            for task_id in task_ids_to_delete:
                if task_id in watched:
                    watched.pop(task_id, None)
                    changed = True

    if changed:
        _save_auto_delete_tasks(watched)


# ---------------------------------------------------------------------------
# Task-card auto-refresh
# ---------------------------------------------------------------------------


def _register_task_card_message(chat_id: int | None, message_id: int | None, task_id: str | None) -> None:
    if not task_id or not isinstance(chat_id, int) or not isinstance(message_id, int):
        return

    TASK_CARD_MESSAGES.setdefault(str(task_id), set()).add((chat_id, message_id))


def _register_task_card_from_message(message, task_id: str | None, fallback_chat_id: int | None = None) -> None:
    chat_id = _chat_id_from_message(message) or fallback_chat_id
    _register_task_card_message(chat_id, _message_id_from_message(message), task_id)


def _register_task_card_from_query(query, task_id: str | None) -> None:
    message = getattr(query, "message", None)
    if not message:
        return

    _register_task_card_message(_chat_id_from_query(query), _message_id_from_message(message), task_id)


def _forget_task_card_message(chat_id: int | None, message_id: int | None, task_id: str | None = None) -> None:
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return

    _cancel_task_card_refresh(chat_id, message_id)

    if task_id:
        task_ids = [str(task_id)]
    else:
        task_ids = list(TASK_CARD_MESSAGES)

    for current_task_id in task_ids:
        messages = TASK_CARD_MESSAGES.get(current_task_id)
        if not messages:
            continue

        messages.discard((chat_id, message_id))
        if not messages:
            TASK_CARD_MESSAGES.pop(current_task_id, None)


async def _delete_task_card_messages(app, task_id: str, chat_id: int | None = None) -> None:
    targets = list(TASK_CARD_MESSAGES.get(str(task_id), set()))
    for target_chat_id, message_id in targets:
        if chat_id is not None and target_chat_id != chat_id:
            continue

        try:
            await app.bot.delete_message(chat_id=target_chat_id, message_id=message_id)
        except Exception:
            logger.debug(
                "Failed to delete task card chat_id=%s message_id=%s task_id=%s",
                target_chat_id,
                message_id,
                task_id,
                exc_info=True,
            )
        finally:
            _forget_task_card_message(target_chat_id, message_id, task_id)


def _cancel_task_card_refresh(chat_id: int, message_id: int) -> None:
    """Cancel the auto-refresh loop for a specific task-card message."""
    key = (chat_id, message_id)
    task = TASK_CARD_REFRESH_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()


def _start_task_card_refresh(app, chat_id: int, message_id: int, task_id: str) -> None:
    """Start (or restart) the 30-second auto-refresh loop for a task card."""
    _cancel_task_card_refresh(chat_id, message_id)
    _register_task_card_message(chat_id, message_id, task_id)
    key = (chat_id, message_id)
    TASK_CARD_REFRESH_TASKS[key] = app.create_task(
        _task_card_refresh_loop(app, chat_id, message_id, task_id)
    )


async def _task_card_refresh_loop(app, chat_id: int, message_id: int, task_id: str) -> None:
    """Refresh the task card every 30 s while the task is actively downloading."""
    try:
        while True:
            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL_SECONDS)
            if not _can_access_task_id(chat_id, task_id):
                return
            try:
                tasks = await asyncio.to_thread(ds_client.list_tasks)
            except DownloadStationError:
                continue

            task = _find_task(tasks, task_id)
            if not task:
                return  # task deleted from DS

            status = (task.get("status") or "").lower()
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=_format_task_card(task),
                    reply_markup=_make_task_keyboard(task_id, status, task.get("type", "")),
                )
            except BadRequest as e:
                err = str(e).lower()
                if "message to edit not found" in err or "chat not found" in err:
                    _forget_task_card_message(chat_id, message_id, task_id)
                    return  # user navigated away
                # "message is not modified" is fine — just continue
            except Exception:
                logger.debug("Task card auto-refresh edit error", exc_info=True)

            if status not in _ACTIVE_STATUSES:
                return  # final refresh is done — stop updating this card
    except asyncio.CancelledError:
        pass
    finally:
        TASK_CARD_REFRESH_TASKS.pop((chat_id, message_id), None)


def _has_active_tasks(tasks: list[dict]) -> bool:
    return _view_has_active_tasks(tasks, _ACTIVE_STATUSES)


async def _run_progress_panel_update_once(app) -> None:
    if not DOWNLOAD_PANEL_MESSAGES:
        return

    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError:
        logger.warning("Progress panel update failed to list tasks", exc_info=True)
        return

    active_now = _has_active_tasks(tasks)
    if not active_now and not any(DOWNLOAD_PANEL_HAD_ACTIVE.values()):
        return

    for chat_id, message_id in list(DOWNLOAD_PANEL_MESSAGES.items()):
        scope = _normalize_list_scope(DOWNLOAD_PANEL_SCOPES.get(chat_id), chat_id)
        page = DOWNLOAD_PANEL_PAGES.get(chat_id, 0)
        visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
        visible_active = _has_active_tasks(visible_tasks)
        if not active_now and not DOWNLOAD_PANEL_HAD_ACTIVE.get(chat_id, False):
            continue

        total_count = len(tasks) if _is_admin_chat(chat_id) else None
        text = _format_tasks(visible_tasks, scope=scope, total_count=total_count, page=page)
        keyboard = _tasks_keyboard(visible_tasks, scope=scope, is_admin=_is_admin_chat(chat_id), page=page)
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                pass
            elif "message to edit not found" in err or "chat not found" in err:
                DOWNLOAD_PANEL_MESSAGES.pop(chat_id, None)
                DOWNLOAD_PANEL_PAGES.pop(chat_id, None)
                DOWNLOAD_PANEL_SCOPES.pop(chat_id, None)
                DOWNLOAD_PANEL_HAD_ACTIVE.pop(chat_id, None)
            else:
                logger.warning("Failed to update progress panel chat_id=%s: %s", chat_id, e)
        else:
            DOWNLOAD_PANEL_HAD_ACTIVE[chat_id] = visible_active


async def _progress_update_loop(app) -> None:
    try:
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL_SECONDS)
        while True:
            await _run_background_step("progress panel update", lambda: _run_progress_panel_update_once(app))
            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Progress update loop stopped")
        raise


async def _run_prune_stale_state_once() -> None:
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError:
        logger.warning("Stale state prune failed to list tasks", exc_info=True)
        return

    active_ids = {str(task["id"]) for task in tasks if task.get("id")}
    await asyncio.to_thread(state_store.prune_stale_task_state, active_ids)


def _task_added_message(
    task_type: str,
    title: str = "",
    task_id: str = "",
    tracker_result: TrackerApplyResult | None = None,
) -> str:
    lines = [
        "Задача добавлена в Download Station.",
        f"Тип: {task_type}",
    ]

    if title:
        lines.append(f"Имя: {title}")
    if task_id:
        lines.append(f"ID: {task_id}")

    lines.extend(_tracker_result_lines(tracker_result))

    return "\n".join(lines)


def _is_message_not_modified(error: BadRequest) -> bool:
    return "message is not modified" in str(error).lower()


async def _safe_edit_message(
    message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    link_preview_options: "LinkPreviewOptions | None" = None,
) -> None:
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            link_preview_options=link_preview_options,
        )
    except BadRequest as e:
        if not _is_message_not_modified(e):
            raise


async def _send_auto_delete(bot, chat_id: int, text: str, delay: float = 3.0) -> None:
    """Send *text* to *chat_id* then delete the message after *delay* seconds."""
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text)
        await asyncio.sleep(delay)
        await msg.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Easter-egg: reaction to download-notification messages
# ---------------------------------------------------------------------------

_REACTION_EASTER_EGG: dict[str, list[str]] = {
    "👍": ["Стараюсь! 💪", "Всегда рад помочь 🤖", "На здоровье!"],
    "❤": ["Тоже тебя люблю 🤖❤️", "Приятно! 😊", "Взаимно!"],
    "🔥": ["Огонь раздача! 🎉", "Горячий контент 🔥", "Я знаю толк в хороших файлах 😎"],
    "🤩": ["Согласен, хороший выбор! 🎬", "Я тоже рад за тебя 🤩", "Отличный вкус!"],
    "👎": ["Ой, что-то не так? 😅", "Попробуй другую раздачу, подберём!", "Понимаю..."],
    "😂": ["Хорошо качать с хорошим настроением 😄", "Ха! 🤖"],
    "🎉": ["Ура! Праздник загрузки! 🎊", "Вечеринка началась! 🎬🍿"],
    "🤔": ["Задумался о жизни? Или о качестве раздачи? 🤖", "Выбор непростой, да?"],
    "😱": ["Всё хорошо? 😅 Надеюсь в хорошем смысле!", "Я тоже иногда удивляюсь 🤖"],
    "💯": ["Именно! 💯", "В точку 🎯"],
    "🫡": ["Есть! Выполнено! 🤖", "Служу верой и правдой 🫡"],
    "🍿": ["О, кино-вечер намечается? 🎬", "Приятного просмотра! 🍿"],
    "❤‍🔥": ["Страсть к хорошему кино — это правильно! 🔥❤️"],
    "🥰": ["Спасибо! 🤖🥰", "Такой приятный пользователь!"],
    "👏": ["Стараюсь! 👏", "Спасибо, буду и дальше в том же духе!"],
    "🤖": ["Привет коллеге! 🤖", "Свои! 🤜🤛"],
}
_REACTION_EASTER_EGG_DEFAULT = [
    "Спасибо за реакцию! 🤖",
    "Приятно получать отзывы 😊",
    "🤖❤️",
    "Понял, принял!",
]


async def reaction_easter_egg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to any message reaction with a fun auto-deleting reply."""
    rct = update.message_reaction
    if not rct or not rct.new_reaction:
        return  # reaction removed — skip

    chat_id = rct.chat.id
    if chat_id not in _all_allowed_chat_ids():
        return

    first = rct.new_reaction[0]
    emoji: str = getattr(first, "emoji", "")
    responses = _REACTION_EASTER_EGG.get(emoji, _REACTION_EASTER_EGG_DEFAULT)
    text = random.choice(responses)
    asyncio.create_task(_send_auto_delete(context.bot, chat_id, text, delay=5.0))


async def _delayed_delete_message(bot, chat_id: int, message_id: int, delay: float = 4.0) -> None:
    """Delete an existing message after *delay* seconds (fire-and-forget)."""
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _safe_edit_callback(
    query,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    link_preview_options: "LinkPreviewOptions | None" = None,
) -> None:
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            link_preview_options=link_preview_options,
        )
    except BadRequest as e:
        if not _is_message_not_modified(e):
            raise


async def _delete_message_safely(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    label: str = "message",
) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as e:
        logger.info("Could not delete %s %s in chat %s: %s", label, message_id, chat_id, e)
    except Exception:
        logger.warning("Could not delete %s %s in chat %s", label, message_id, chat_id, exc_info=True)


async def _delete_download_panel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    keep_message_id: int | None = None,
) -> None:
    message_id = DOWNLOAD_PANEL_MESSAGES.get(chat_id)
    if not message_id or message_id == keep_message_id:
        return

    await _delete_message_safely(context, chat_id, message_id, "download panel")
    DOWNLOAD_PANEL_MESSAGES.pop(chat_id, None)
    DOWNLOAD_PANEL_PAGES.pop(chat_id, None)
    DOWNLOAD_PANEL_SCOPES.pop(chat_id, None)
    DOWNLOAD_PANEL_HAD_ACTIVE.pop(chat_id, None)


async def _send_download_panel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    tasks: list[dict],
    scope: str,
    total_count: int | None = None,
    page: int = 0,
) -> None:
    await _delete_download_panel(context, chat_id)
    DOWNLOAD_PANEL_PAGES[chat_id] = page
    DOWNLOAD_PANEL_SCOPES[chat_id] = scope
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=_format_tasks(tasks, scope=scope, total_count=total_count, page=page),
        reply_markup=_tasks_keyboard(tasks, scope=scope, is_admin=_is_admin_chat(chat_id), page=page),
    )
    DOWNLOAD_PANEL_MESSAGES[chat_id] = message.message_id
    DOWNLOAD_PANEL_HAD_ACTIVE[chat_id] = _has_active_tasks(tasks)


async def _replace_message_with_download_panel(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    tasks: list[dict],
    scope: str,
    total_count: int | None = None,
    page: int = 0,
) -> None:
    await _delete_download_panel(context, chat_id, keep_message_id=message.message_id)
    DOWNLOAD_PANEL_PAGES[chat_id] = page
    DOWNLOAD_PANEL_SCOPES[chat_id] = scope
    await _safe_edit_message(
        message,
        _format_tasks(tasks, scope=scope, total_count=total_count, page=page),
        reply_markup=_tasks_keyboard(tasks, scope=scope, is_admin=_is_admin_chat(chat_id), page=page),
    )
    DOWNLOAD_PANEL_MESSAGES[chat_id] = message.message_id
    DOWNLOAD_PANEL_HAD_ACTIVE[chat_id] = _has_active_tasks(tasks)


async def _edit_message_as_download_panel(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await _safe_edit_callback(query, text, reply_markup=reply_markup)

    if not query.message:
        return

    chat_id = query.message.chat.id
    message_id = query.message.message_id
    await _delete_download_panel(context, chat_id, keep_message_id=message_id)
    DOWNLOAD_PANEL_MESSAGES[chat_id] = message_id


async def _edit_download_panel(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    tasks: list[dict],
    scope: str,
    total_count: int | None = None,
    page: int = 0,
) -> None:
    chat_id = _chat_id_from_query(query)
    DOWNLOAD_PANEL_PAGES[chat_id] = page
    DOWNLOAD_PANEL_SCOPES[chat_id] = scope
    await _edit_message_as_download_panel(
        query,
        context,
        _format_tasks(tasks, scope=scope, total_count=total_count, page=page),
        _tasks_keyboard(tasks, scope=scope, is_admin=_is_admin_chat(chat_id), page=page),
    )
    DOWNLOAD_PANEL_HAD_ACTIVE[chat_id] = _has_active_tasks(tasks)


async def _wait_for_magnet_task_id(
    magnet_uri: str,
    known_task_ids: set[str],
    progress_message,
    attempts: int | None = None,
    delay_seconds: float | None = None,
) -> str:
    attempts = MAGNET_POLL_ATTEMPTS if attempts is None else attempts
    delay_seconds = MAGNET_POLL_INTERVAL_SECONDS if delay_seconds is None else delay_seconds
    for step in range(attempts):
        if step:
            await asyncio.sleep(delay_seconds)

        await _safe_edit_message(progress_message, _magnet_wait_text(step, attempts))

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError:
            logger.warning("Failed to poll Download Station task id for magnet", exc_info=True)
            continue

        task_id = _find_magnet_task_id(tasks, magnet_uri, known_task_ids)
        if task_id:
            return task_id

    return ""


# --- Rutracker search ---


def _build_search_query(base: str, settings: dict) -> str:
    parts = [base]
    quality = settings.get("quality", "1080p")
    if quality != "any":
        parts.append(quality)
    if settings.get("audio"):
        parts.append("Original")
    if settings.get("subs"):
        parts.append("Sub")
    return " ".join(parts)


async def _build_diagnostics_text() -> str:
    report = await asyncio.to_thread(
        run_diagnostics,
        rutracker_client=rutracker_client,
        jackett_client=jackett_client,
        ds_client=ds_client,
        tracker_service=_tracker_service(),
        display_timezone=DISPLAY_TIMEZONE,
        plex_client=plex_client,
        plex_cache_info=_plex_cache_info() if plex_client else None,
    )
    return format_diagnostics(report)


async def kp_link_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ConversationHandler entry point: user sent a Kinopoisk URL.

    Fetches film/series info, stores the search base in user_data, then shows
    the quality-options keyboard so the user can kick off the normal search flow.
    """
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    kp_id = extract_kp_id(text)
    if not kp_id:
        return ConversationHandler.END

    msg = await update.message.reply_text("🎬 Получаю информацию из Кинопоиска…")
    try:
        info: KinopoiskInfo = await asyncio.to_thread(kinopoisk_client.get_film_info, kp_id)
    except KinopoiskError as e:
        await msg.edit_text(f"Не удалось получить данные из Кинопоиска:\n{e}")
        return ConversationHandler.END

    context.user_data["srch_query"] = info.search_base
    context.user_data["srch_kp_info"] = {
        "title_ru": info.title_ru,
        "title_en": info.title_en,
        "year": info.year,
        "type_label": info.type_label,
        "director": info.director,
    }

    lines = [f"{info.type_label}: <b>{info.title_ru}</b>"]
    if info.title_en and info.title_en.lower() != info.title_ru.lower():
        lines.append(f"  {info.title_en}")
    if info.year:
        lines.append(f"📅 Год: {info.year}")
    if info.director:
        lines.append(f"🎬 Режиссёр: {info.director}")
    lines.append(f"\n🔍 Запрос для поиска: «{info.search_base}»")

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_search_options_keyboard(_tracker_label_from_context(context)),
    )
    return SEARCH_OPTIONS


def _tracker_label_from_context(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return a human-readable label for the currently selected Jackett trackers.

    Returns an empty string when Jackett is not configured (no tracker button shown).
    Falls back to 'Rutracker' when no indexer list has been fetched yet.
    """
    if jackett_client is None:
        return ""
    indexers = context.user_data.get("srch_jackett_indexers", [])
    selected = context.user_data.get("srch_jackett_selected", set())
    if not indexers or not selected:
        return "Rutracker"  # default before first fetch
    return tracker_selection_label(indexers, selected)


async def search_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query_text = (update.message.text or "").strip()
    if not query_text:
        await update.message.reply_text("Введите текст для поиска или /cancel для отмены.")
        return ConversationHandler.END

    # Normalise 'Сезон N' → 'Сезон: N' to match Rutracker title format
    query_text = _normalize_season_in_query(query_text)
    context.user_data["srch_query"] = query_text
    msg = await update.message.reply_text(
        f"Запрос: «{query_text}»",
        reply_markup=_search_options_keyboard(_tracker_label_from_context(context)),
    )
    context.user_data["srch_ui_msg_id"] = msg.message_id
    context.user_data["srch_ui_chat_id"] = update.effective_chat.id
    return SEARCH_OPTIONS


def _build_results_text(results_data: list[dict], search_query: str, page: int, *, banner: str = "") -> str:
    """Format the visible page of search results as an HTML text block.

    Each result shows: icon + number + full title as a clickable hyperlink,
    then size / seeders on the next line.  A partial-series note is appended
    when present.  A tracker badge is shown for Jackett results.

    Returns HTML-formatted text; callers must pass parse_mode='HTML'.
    """
    lines = []
    if banner:
        lines.append(banner)
    lines.append(f"Результаты по «{html_module.escape(search_query)}»:")
    start = page * SEARCH_PAGE_SIZE
    for index, r in enumerate(results_data[start : start + SEARCH_PAGE_SIZE], start=start):
        icon = "⭐" if r.get("recommended") else "🔎"
        ep_note = f"  ⚠️ {r['ep_str']}" if r.get("partial") and r.get("ep_str") else ""
        title_escaped = html_module.escape(r["title"])
        url = r.get("url", "")
        tracker_id = r.get("tracker_name", "")
        abbr = _tracker_abbr(tracker_id) if tracker_id else ""
        tracker_prefix = f"[{abbr}] " if abbr else ""
        title_linked = (
            f'<a href="{html_module.escape(url)}">{title_escaped}</a>'
            if url else title_escaped
        )
        lines.append(
            f"\n{icon} {index + 1}. {tracker_prefix}{title_linked}"
            f"\n   📦 {r['size']} | 🌱 {r['seeders']}{ep_note}"
        )
    return "\n".join(lines)


async def _show_jackett_selector(
    edit_fn,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    header: str = "",
    return_to: str = "results",
) -> int:
    """Fetch Jackett indexers and show the tracker-selection keyboard.

    return_to controls what happens when the user confirms:
      "options"  → save selection, return to SEARCH_OPTIONS screen
      "advanced" → save selection, return to SEARCH_ADVANCED screen
      "results"  → run search immediately, return to SEARCH_RESULTS

    Returns SEARCH_JACKETT_SELECT on success, ConversationHandler.END on failure.
    """
    if jackett_client is None:
        await edit_fn("Jackett не настроен.")
        return ConversationHandler.END

    try:
        indexers = await asyncio.to_thread(jackett_client.get_indexers)
    except JackettError as e:
        logger.error("Jackett get_indexers failed: %s", e)
        await edit_fn(_friendly_error("jackett", str(e)), reply_markup=_search_error_keyboard(), parse_mode="HTML")
        return ConversationHandler.END

    if not indexers:
        logger.warning("Jackett: no indexers configured")
        await edit_fn("🌐 <b>Jackett</b>: нет настроенных индексеров", reply_markup=_search_error_keyboard(), parse_mode="HTML")
        return ConversationHandler.END

    # Keep existing selection if available; otherwise default to Rutracker
    if "srch_jackett_selected" not in context.user_data:
        rutracker_ids = {i["id"] for i in indexers if "rutracker" in i["id"].lower()}
        context.user_data["srch_jackett_selected"] = rutracker_ids if rutracker_ids else {i["id"] for i in indexers}

    context.user_data["srch_jackett_indexers"] = indexers
    context.user_data["srch_picker_return_to"] = return_to

    selected = context.user_data["srch_jackett_selected"]
    confirm_label = "✅ Применить" if return_to in ("options", "advanced") else "🔍 Искать"
    show_back = return_to in ("options", "advanced")

    prompt = (header + "\n" if header else "") + "Выберите трекеры для поиска:"
    try:
        await edit_fn(
            prompt,
            reply_markup=_jackett_select_keyboard(
                indexers, selected, confirm_label=confirm_label, show_back=show_back
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Jackett selector edit failed: %s", exc, exc_info=True)
        raise
    return SEARCH_JACKETT_SELECT


async def search_pick_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open tracker picker from SEARCH_OPTIONS or SEARCH_ADVANCED."""
    query = update.callback_query
    await query.answer()
    # callback_data: srch:pick_tracker:options  or  srch:pick_tracker:advanced
    parts = (query.data or "").split(":")
    return_to = parts[2] if len(parts) > 2 else "options"
    return await _show_jackett_selector(query.edit_message_text, context, return_to=return_to)


async def search_switch_trackers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open tracker picker from SEARCH_RESULTS (will re-run search on confirm)."""
    query = update.callback_query
    await query.answer()
    return await _show_jackett_selector(query.edit_message_text, context, return_to="results")


async def search_direct_rutracker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Search directly on Rutracker, bypassing Jackett."""
    query = update.callback_query
    await query.answer()
    if rutracker_client is None:
        await query.answer("Rutracker не настроен.", show_alert=True)
        return SEARCH_RESULTS
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    if not search_query:
        await query.answer("Запрос потерян. Начните поиск заново.", show_alert=True)
        return ConversationHandler.END
    await query.edit_message_text(f"🔍 Ищу «{search_query}» напрямую в Rutracker…")
    try:
        rt_results = await asyncio.to_thread(rutracker_client.search, search_query)
    except RutrackerError as rt_err:
        await query.edit_message_text(_friendly_error("rutracker", str(rt_err)), reply_markup=_search_error_keyboard(), parse_mode="HTML")
        return ConversationHandler.END
    results_data = []
    for r in rt_results:
        ep = _parse_episode_info(r.title)
        partial = ep is not None and ep[0] < ep[1]
        results_data.append({
            "source": "rutracker",
            "topic_id": r.topic_id,
            "title": r.title,
            "url": f"https://rutracker.org/forum/viewtopic.php?t={r.topic_id}",
            "category": r.category,
            "size": r.size,
            "seeders": r.seeders,
            "partial": partial,
            "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
            "magnet_url": None,
            "torrent_url": None,
            "tracker_name": "rutracker",
        })
    if not results_data:
        await query.edit_message_text(
            f"По запросу «{search_query}» ничего не найдено в Rutracker."
        )
        return ConversationHandler.END
    results_data.sort(key=_score_result, reverse=True)
    results_data[0]["recommended"] = True
    banner = "🔗 Прямой поиск Rutracker"
    context.user_data["srch_results"] = results_data
    context.user_data["srch_results_page"] = 0
    context.user_data["srch_banner"] = banner
    context.user_data["srch_source"] = "rutracker"
    await query.edit_message_text(
        _build_results_text(results_data, search_query, 0, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=0,
            show_switch_trackers=False,
            show_retry_jackett=bool(jackett_client),  # offer back to Jackett
            show_direct_rutracker=False,              # already on direct RT
        ),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return SEARCH_RESULTS


def _extract_rutracker_topic_id(url: str) -> str:
    """Extract numeric topic_id from a Rutracker topic URL.

    Handles both:
      https://rutracker.org/forum/viewtopic.php?t=1234567
      https://rutracker.net/forum/viewtopic.php?t=1234567
    Returns the id string or "" if not found / not a Rutracker URL.
    """
    if "rutracker." not in url:
        return ""
    match = re.search(r"[?&]t=(\d+)", url)
    return match.group(1) if match else ""


async def _refresh_jackett_torrent_url(
    jackett_client,
    result: dict,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Re-run Jackett search to obtain a fresh torrent_url for the given result.

    Used when the cached torrent_url returns 404 (Jackett proxy token may have
    expired or the tracker session was refreshed).  Matches fresh results first
    by topic URL, then by title + tracker + size.

    Returns a fresh torrent_url string, or None if the result could not be found.
    """
    query = (
        context.user_data.get("srch_search_query")
        or context.user_data.get("srch_query", "")
    )
    if not query:
        logger.debug("_refresh_jackett_torrent_url: no query in user_data, skipping")
        return None

    indexers = list(context.user_data.get("srch_jackett_selected") or [])
    topic_url = result.get("url", "")
    tracker_name = result.get("tracker_name") or result.get("category", "")
    title = result.get("title", "")
    size = result.get("size", "")

    try:
        fresh = await asyncio.to_thread(jackett_client.search, query, indexers or None)
    except JackettError as e:
        logger.warning("_refresh_jackett_torrent_url: re-search failed: %s", e)
        return None

    # Primary match: same topic URL (unique per tracker post)
    if topic_url:
        for r in fresh:
            if r.topic_url == topic_url and r.torrent_url:
                logger.debug("_refresh_jackett_torrent_url: matched by topic_url")
                return r.torrent_url

    # Fallback match: title + tracker + size
    for r in fresh:
        if r.title == title and r.tracker == tracker_name and r.size == size and r.torrent_url:
            logger.debug("_refresh_jackett_torrent_url: matched by title+tracker+size")
            return r.torrent_url

    logger.debug("_refresh_jackett_torrent_url: no matching result found in %d fresh results", len(fresh))
    return None


async def _run_search(send_fn, context: ContextTypes.DEFAULT_TYPE, search_query: str) -> int:
    """Core search logic shared between callback and text-message entry points.

    *send_fn* is either ``query.edit_message_text`` (callback) or
    ``message.reply_text`` (plain text).  The first call shows a loading
    indicator; subsequent calls reuse the returned Message so only one
    bot message ends up in the chat.

    Post-processing pipeline:
      1. Fetch up to RUTRACKER_MAX_RESULTS (50) results.
      2. If the query contains a season number, filter to that season only.
      3. Sort all (remaining) results by _score_result — best first.
      4. If filtering left 0 results AND the query had a quality keyword,
         show a fallback button to retry without the quality filter.
    """
    context.user_data["srch_search_query"] = search_query
    loading_msg = await send_fn(f"🔍 Ищу «{search_query}»…")
    if loading_msg is not None:
        context.user_data["srch_ui_msg_id"] = loading_msg.message_id
        context.user_data["srch_ui_chat_id"] = loading_msg.chat_id

    # After the first send we always edit-in-place regardless of origin.
    edit_fn = loading_msg.edit_text if loading_msg is not None else send_fn

    # --- Search: Rutracker first, Jackett as fallback on error ---
    results_data = []
    banner = ""
    source = "rutracker"

    if jackett_client:
        # --- Jackett-first: use pre-selected indexers (default to Rutracker indexer) ---
        if "srch_jackett_selected" not in context.user_data:
            try:
                indexers = await asyncio.to_thread(jackett_client.get_indexers)
                rutracker_ids = {i["id"] for i in indexers if "rutracker" in i["id"].lower()}
                selected = rutracker_ids if rutracker_ids else {i["id"] for i in indexers}
                context.user_data["srch_jackett_indexers"] = indexers
                context.user_data["srch_jackett_selected"] = selected
            except JackettError as e:
                logger.warning("Jackett get_indexers failed at search start: %s", e)
                if rutracker_client:
                    banner = f"⚠️ Jackett недоступен, ищу напрямую в Rutracker"
                    # fall through to Rutracker path below
                else:
                    await edit_fn(_friendly_error("jackett", str(e)), reply_markup=_search_error_keyboard(), parse_mode="HTML")
                    return ConversationHandler.END

        selected: set[str] = context.user_data.get("srch_jackett_selected", set())

        if selected:  # Jackett search
            try:
                j_results_raw = await asyncio.wait_for(
                    asyncio.to_thread(
                        jackett_client.search,
                        search_query,
                        indexers=list(selected),
                        fetch_limit=JACKETT_FETCH_LIMIT,
                    ),
                    timeout=45.0,
                )
            except (JackettError, asyncio.TimeoutError) as e:
                raw_err = (
                    "Jackett не ответил за 45 сек — проверьте Global timeout в настройках Jackett"
                    if isinstance(e, asyncio.TimeoutError) else str(e)
                )
                logger.error("Jackett search failed in _run_search: %s", raw_err)
                if rutracker_client:
                    banner = f"⚠️ Jackett: {raw_err[:80]}. Ищу в Rutracker напрямую…"
                    # fall through to Rutracker path
                else:
                    await edit_fn(_friendly_error("jackett", raw_err), reply_markup=_search_error_keyboard(), parse_mode="HTML")
                    return ConversationHandler.END
            else:
                for r in j_results_raw:
                    ep = _parse_episode_info(r.title)
                    partial = ep is not None and ep[0] < ep[1]
                    results_data.append({
                        "source": "jackett",
                        "topic_id": "",
                        "title": r.title,
                        "url": r.topic_url or "",
                        "category": r.tracker,
                        "size": r.size,
                        "seeders": r.seeders,
                        "partial": partial,
                        "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
                        "magnet_url": r.magnet_url,
                        "torrent_url": r.torrent_url,
                        "tracker_name": r.tracker,
                    })
                source = "jackett"

    if not results_data and rutracker_client:
        # Direct Rutracker search: either Jackett not configured, or Jackett failed/returned nothing
        try:
            rt_results = await asyncio.to_thread(rutracker_client.search, search_query)
        except RutrackerError as rt_err:
            await edit_fn(_friendly_error("rutracker", str(rt_err)), reply_markup=_search_error_keyboard(), parse_mode="HTML")
            return ConversationHandler.END
        for r in rt_results:
            ep = _parse_episode_info(r.title)
            partial = ep is not None and ep[0] < ep[1]
            results_data.append({
                "source": "rutracker",
                "topic_id": r.topic_id,
                "title": r.title,
                "url": f"https://rutracker.org/forum/viewtopic.php?t={r.topic_id}",
                "category": r.category,
                "size": r.size,
                "seeders": r.seeders,
                "partial": partial,
                "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
                "magnet_url": None,
                "torrent_url": None,
                "tracker_name": "rutracker",
            })
        source = "rutracker"

    if not results_data and not rutracker_client and not jackett_client:
        await edit_fn("Поиск недоступен: не настроен ни Rutracker, ни Jackett.", reply_markup=_search_error_keyboard(), parse_mode="HTML")
        return ConversationHandler.END

    if not results_data:
        await edit_fn(
            f"По запросу «{search_query}» ничего не найдено.\n"
            "Попробуйте другой запрос."
        )
        return ConversationHandler.END

    # --- Step 1: season filter ---
    season_num = _extract_season_from_query(search_query)
    if season_num is not None:
        # Snapshot pre-filter results so we can hint "which seasons DO exist on
        # the tracker" when the requested season has zero matches.
        pre_filter_results = list(results_data)
        filtered = _filter_by_season(results_data, season_num)
        if filtered:
            results_data = filtered
        else:
            base_query = context.user_data.get("srch_query", "").strip()
            has_quality = bool(base_query) and base_query.lower() != search_query.lower()
            if has_quality:
                await edit_fn(
                    f"По запросу «{search_query}» раздач с указанным качеством не найдено.\n"
                    f"Попробовать без фильтра качества?",
                    reply_markup=_no_quality_keyboard(base_query),
                )
                return SEARCH_RESULTS

            # If we ran this search inside the season picker (srch_base_title set)
            # and the tracker has SOME seasons but not the one requested, list them
            # and offer a way back to the picker.
            available = _seasons_available_in_results(pre_filter_results)
            in_picker_flow = bool(context.user_data.get("srch_base_title"))
            if available and in_picker_flow:
                seasons_str = ", ".join(str(n) for n in available)
                await edit_fn(
                    f"По запросу «{search_query}» ничего не найдено.\n"
                    f"На трекерах найдены сезоны: {seasons_str}.",
                    reply_markup=_season_back_to_picker_keyboard(),
                )
                return SEARCH_SEASON_SELECT

            await edit_fn(
                f"По запросу «{search_query}» ничего не найдено.\n"
                "Попробуйте другой запрос."
            )
            return ConversationHandler.END

    # --- Step 2: sort by score, best first ---
    results_data.sort(key=_score_result, reverse=True)
    results_data[0]["recommended"] = True

    context.user_data["srch_results"] = results_data
    context.user_data["srch_results_page"] = 0
    context.user_data["srch_banner"] = banner
    context.user_data["srch_source"] = source

    await edit_fn(
        _build_results_text(results_data, search_query, 0, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=0,
            show_switch_trackers=bool(jackett_client and source == "jackett"),
            show_retry_jackett=bool(jackett_client and source == "rutracker"),
            show_direct_rutracker=bool(rutracker_client and source == "jackett"),
        ),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return SEARCH_RESULTS


async def _execute_search(query, context: ContextTypes.DEFAULT_TYPE, search_query: str) -> int:
    """Execute a Rutracker search triggered by a callback query (edits the message)."""
    return await _run_search(query.edit_message_text, context, search_query)


async def search_results_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Navigate to a different page of search results."""
    query = update.callback_query
    await query.answer()

    try:
        page = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return SEARCH_RESULTS

    context.user_data["srch_results_page"] = page
    results_data = context.user_data.get("srch_results", [])
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    banner = context.user_data.get("srch_banner", "")
    source = context.user_data.get("srch_source", "")

    await query.edit_message_text(
        _build_results_text(results_data, search_query, page, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=page,
            show_switch_trackers=bool(jackett_client and source == "jackett"),
            show_retry_jackett=bool(jackett_client and source == "rutracker"),
            show_direct_rutracker=bool(rutracker_client and source == "jackett"),
        ),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return SEARCH_RESULTS


async def search_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Быстрый поиск с 1080p."""
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("srch_query", "")
    return await _execute_search(query, context, f"{base} 1080p")


async def search_no_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Повторить поиск без фильтра качества (фоллбэк при 0 результатов)."""
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("srch_query", "").strip()
    if not base:
        await query.edit_message_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END
    return await _execute_search(query, context, base)


async def search_expand_jackett(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Kept for backwards-compat; now delegates to search_switch_trackers."""
    return await search_switch_trackers(update, context)


async def search_jackett_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from tracker picker to SEARCH_OPTIONS or SEARCH_ADVANCED without searching."""
    query = update.callback_query
    await query.answer()

    return_to = context.user_data.get("srch_picker_return_to", "options")
    base = context.user_data.get("srch_query", "")
    tracker_label = _tracker_label_from_context(context)

    if return_to == "advanced":
        settings = context.user_data.get("srch_settings", dict(_SRCH_DEFAULT_SETTINGS))
        await query.edit_message_text(
            f"Запрос: «{base}»\nНастройте параметры поиска:",
            reply_markup=_search_advanced_keyboard(settings, tracker_label),
        )
        return SEARCH_ADVANCED

    await query.edit_message_text(
        f"Запрос: «{base}»",
        reply_markup=_search_options_keyboard(tracker_label),
    )
    return SEARCH_OPTIONS


async def search_jackett_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle a Jackett indexer on/off in the selection panel."""
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split(":", 2)
    if len(parts) < 3:
        return SEARCH_JACKETT_SELECT

    indexer_id = parts[2]
    selected: set[str] = context.user_data.get("srch_jackett_selected", set())
    if indexer_id in selected:
        selected.discard(indexer_id)
    else:
        selected.add(indexer_id)
    context.user_data["srch_jackett_selected"] = selected

    indexers = context.user_data.get("srch_jackett_indexers", [])
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    return_to = context.user_data.get("srch_picker_return_to", "results")
    confirm_label = "✅ Применить" if return_to in ("options", "advanced") else "🔍 Искать"
    show_back = return_to in ("options", "advanced")
    await query.edit_message_text(
        f"Поиск: «{search_query}»\nВыберите трекеры для поиска:",
        reply_markup=_jackett_select_keyboard(
            indexers, selected, confirm_label=confirm_label, show_back=show_back
        ),
    )
    return SEARCH_JACKETT_SELECT


async def _safe_answer(query, text: str = "", *, show_alert: bool = False) -> None:
    """Answer a callback query, silently ignoring 'already answered' errors.

    Telegram only allows one answerCallbackQuery per callback ID.  Calling it a
    second time raises BadRequest.  This helper is used in error paths so that
    a duplicate answer never causes the surrounding recovery code to abort.
    """
    try:
        await query.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def search_jackett_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm tracker selection.

    Behaviour depends on srch_picker_return_to stored in user_data:
    - "options"  → save selection, return to SEARCH_OPTIONS (no search yet)
    - "advanced" → save selection, return to SEARCH_ADVANCED (no search yet)
    - "results"  → run search with selected indexers, return to SEARCH_RESULTS

    IMPORTANT: query.answer() is called exactly once per execution path.
    """
    query = update.callback_query

    if jackett_client is None:
        await query.answer()
        await query.edit_message_text("Jackett не настроен.")
        return ConversationHandler.END

    selected: set[str] = context.user_data.get("srch_jackett_selected", set())
    if not selected:
        await query.answer("Выберите хотя бы один трекер.", show_alert=True)
        indexers = context.user_data.get("srch_jackett_indexers", [])
        return_to_err = context.user_data.get("srch_picker_return_to", "results")
        confirm_label = "✅ Применить" if return_to_err in ("options", "advanced") else "🔍 Искать"
        show_back = return_to_err in ("options", "advanced")
        search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
        await query.edit_message_text(
            f"Поиск: «{search_query}»\nВыберите трекеры для поиска:",
            reply_markup=_jackett_select_keyboard(
                indexers, selected, confirm_label=confirm_label, show_back=show_back
            ),
        )
        return SEARCH_JACKETT_SELECT

    return_to = context.user_data.get("srch_picker_return_to", "results")

    # --- Return to options/advanced without searching ---
    if return_to in ("options", "advanced"):
        await query.answer()
        base = context.user_data.get("srch_query", "")
        tracker_label = _tracker_label_from_context(context)
        if return_to == "advanced":
            settings = context.user_data.get("srch_settings", dict(_SRCH_DEFAULT_SETTINGS))
            await query.edit_message_text(
                f"Запрос: «{base}»\nНастройте параметры поиска:",
                reply_markup=_search_advanced_keyboard(settings, tracker_label),
            )
            return SEARCH_ADVANCED
        else:
            await query.edit_message_text(
                f"Запрос: «{base}»",
                reply_markup=_search_options_keyboard(tracker_label),
            )
            return SEARCH_OPTIONS

    # --- "results" mode: run search immediately ---
    await query.answer()
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))

    try:
        await query.edit_message_text(f"🔍 Ищу «{search_query}» через Jackett…")
    except Exception as exc:
        logger.warning("search_jackett_do: loading edit failed: %s", exc)

    try:
        j_results_raw = await asyncio.wait_for(
            asyncio.to_thread(
                jackett_client.search,
                search_query,
                indexers=list(selected),
                fetch_limit=JACKETT_FETCH_LIMIT,
            ),
            timeout=45.0,
        )
    except (JackettError, asyncio.TimeoutError) as e:
        raw_err = (
            "Jackett не ответил за 45 сек — проверьте Global timeout в настройках Jackett"
            if isinstance(e, asyncio.TimeoutError) else str(e)
        )
        if "not well-formed" in raw_err or "разобрать ответ" in raw_err:
            raw_err += " — возможно, индексер требует авторизации в Jackett"
        logger.error("Jackett search failed: %s", raw_err)
        await _safe_answer(query, f"❌ {raw_err}", show_alert=True)
        existing = context.user_data.get("srch_results", [])
        banner = context.user_data.get("srch_banner", "")
        if existing:
            await query.edit_message_text(
                _build_results_text(existing, search_query, 0, banner=banner),
                reply_markup=_search_results_keyboard(
                    existing, page=0,
                    show_retry_jackett=True,   # Jackett failed — offer retry, not "direct RT" (already on RT)
                    show_direct_rutracker=False,
                ),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return SEARCH_RESULTS
        await query.edit_message_text(_friendly_error("jackett", raw_err), reply_markup=_search_error_keyboard(), parse_mode="HTML")
        return ConversationHandler.END

    if not j_results_raw:
        await _safe_answer(query, "Jackett не нашёл результатов по выбранным трекерам.", show_alert=True)
        existing = context.user_data.get("srch_results", [])
        banner = context.user_data.get("srch_banner", "")
        await query.edit_message_text(
            _build_results_text(existing, search_query, 0, banner=banner) if existing
            else f"По запросу «{search_query}» ничего не найдено.",
            reply_markup=_search_results_keyboard(
                existing, page=0,
                show_retry_jackett=True,   # offer retry with different trackers
                show_direct_rutracker=False,
            ) if existing else None,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return SEARCH_RESULTS if existing else ConversationHandler.END

    logger.info("Jackett search returned %d results", len(j_results_raw))

    j_results_data = []
    for r in j_results_raw:
        ep = _parse_episode_info(r.title)
        partial = ep is not None and ep[0] < ep[1]
        j_results_data.append({
            "source": "jackett",
            "topic_id": "",
            "title": r.title,
            "url": r.topic_url or "",
            "category": r.tracker,
            "size": r.size,
            "seeders": r.seeders,
            "partial": partial,
            "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
            "magnet_url": r.magnet_url,
            "torrent_url": r.torrent_url,
            "tracker_name": r.tracker,
        })

    merged = sorted(j_results_data, key=_score_result, reverse=True)
    banner = f"🔍 Jackett: {len(merged)} результатов"
    if merged:
        merged[0]["recommended"] = True

    context.user_data["srch_results"] = merged
    context.user_data["srch_results_page"] = 0
    context.user_data["srch_source"] = "jackett"
    context.user_data["srch_banner"] = banner

    try:
        await query.edit_message_text(
            _build_results_text(merged, search_query, 0, banner=banner),
            reply_markup=_search_results_keyboard(
                merged, page=0,
                show_switch_trackers=True,
                show_retry_jackett=False,
                show_direct_rutracker=bool(rutracker_client),
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        logger.error("Jackett results display failed: %s", exc, exc_info=True)
        await _safe_answer(query, f"Ошибка отображения: {exc}", show_alert=True)
    return SEARCH_RESULTS


async def search_show_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показать расширенные настройки поиска."""
    query = update.callback_query
    await query.answer()
    settings = dict(_SRCH_DEFAULT_SETTINGS)
    context.user_data["srch_settings"] = settings
    base = context.user_data.get("srch_query", "")
    await query.edit_message_text(
        f"Запрос: «{base}»\nНастройте параметры поиска:",
        reply_markup=_search_advanced_keyboard(settings, _tracker_label_from_context(context)),
    )
    return SEARCH_ADVANCED


async def search_toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключение качества или доп. опций в расширенном поиске."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")  # ["srch", "quality"|"toggle", value]
    if len(parts) < 3:
        return SEARCH_ADVANCED

    action, value = parts[1], parts[2]
    settings = context.user_data.setdefault("srch_settings", dict(_SRCH_DEFAULT_SETTINGS))

    if action == "quality":
        settings["quality"] = value
    elif action == "toggle" and value in ("audio", "subs"):
        settings[value] = not settings.get(value, False)

    base = context.user_data.get("srch_query", "")
    await query.edit_message_text(
        f"Запрос: «{base}»\nНастройте параметры поиска:",
        reply_markup=_search_advanced_keyboard(settings, _tracker_label_from_context(context)),
    )
    return SEARCH_ADVANCED


async def search_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запустить поиск с выбранными расширенными настройками."""
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("srch_query", "")
    settings = context.user_data.get("srch_settings", dict(_SRCH_DEFAULT_SETTINGS))
    return await _execute_search(query, context, _build_search_query(base, settings))


async def search_series_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'Другой сезон' button: offer season selection, then search."""
    query = update.callback_query
    await query.answer()

    series_query = context.user_data.pop("srch_series_query", "")
    if not series_query:
        await query.edit_message_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    context.user_data["srch_base_title"] = series_query
    context.user_data["srch_query"] = series_query

    # If KinoPoisk is available, look up the season count and offer a selector.
    if kinopoisk_client:
        await query.edit_message_text(f"🔍 Ищу информацию о «{series_query}»…")
        try:
            total_seasons: int | None = await asyncio.wait_for(
                asyncio.to_thread(kinopoisk_client.search_series_seasons, series_query),
                timeout=8,
            )
        except Exception:
            total_seasons = None

        context.user_data["srch_total_seasons"] = total_seasons

        if total_seasons == 1:
            # Single season — skip the selector and search directly.
            return await _execute_search(query, context, series_query)

        plex_seasons = await _get_plex_seasons_for_series(series_query)
        context.user_data["srch_plex_seasons"] = plex_seasons

        season_count_label = f" ({total_seasons} сез.)" if total_seasons else ""
        quality_hint = _series_quality_hint(context.user_data.get("srch_picked_quality", ""))
        plex_line = _series_plex_seasons_line(plex_seasons, total_seasons)
        await query.edit_message_text(
            f"📺 Сериал: «{series_query}»{season_count_label}\n"
            f"{plex_line}{quality_hint}Выберите сезон:",
            reply_markup=_season_select_keyboard(total_seasons, plex_seasons=plex_seasons),
        )
        return SEARCH_SEASON_SELECT

    # No KinoPoisk — go straight to search.
    return await _execute_search(query, context, series_query)


def _series_quality_hint(picked_quality: str) -> str:
    """Return a short, human-readable line for the season picker when a quality
    was inherited from the previously picked release. Empty string if unknown."""
    if not picked_quality:
        return ""
    pretty = {"4k": "2160p (4K)", "1080": "1080p", "720": "720p", "480": "480p"}.get(picked_quality, "")
    if not pretty:
        return ""
    return f"Будет искать в качестве {pretty} (по выбранному торренту).\n"


def _series_plex_seasons_line(plex_seasons: set[int] | None, total_seasons: int | None) -> str:
    """Return a 'В Plex: 1, 2, 3' line (or 'Все сезоны уже в Plex') for the picker.

    Empty string when no information to show.
    """
    if not plex_seasons:
        return ""
    sorted_seasons = sorted(plex_seasons)
    if total_seasons and len(sorted_seasons) >= total_seasons:
        return "Все сезоны уже в Plex.\n"
    return f"В Plex: {', '.join(str(n) for n in sorted_seasons)}\n"


async def _get_plex_seasons_for_series(series_query: str) -> set[int]:
    """Return the set of season numbers this show has in Plex. Empty set if disabled/unknown."""
    if not PLEX_ENABLED or not series_query:
        return set()
    show = _plex_show_find(series_query)
    if show is None:
        return set()
    seasons = await _plex_ensure_show_seasons(show)
    return set(seasons.keys())


async def search_season_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User tapped a numbered season button."""
    query = update.callback_query
    await query.answer()

    try:
        season_num = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        return SEARCH_SEASON_SELECT

    base = context.user_data.get("srch_base_title", "")
    if not base:
        await query.edit_message_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    quality_suffix = _quality_to_query_suffix(context.user_data.get("srch_picked_quality", ""))
    search_query = _normalize_season_in_query(f"{base} Сезон {season_num}{quality_suffix}")
    return await _execute_search(query, context, search_query)


async def search_season_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose to search the whole series without a season filter."""
    query = update.callback_query
    await query.answer()

    base = context.user_data.get("srch_base_title", "")
    if not base:
        await query.edit_message_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    quality_suffix = _quality_to_query_suffix(context.user_data.get("srch_picked_quality", ""))
    return await _execute_search(query, context, f"{base}{quality_suffix}".strip())


async def search_season_back_to_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from a 0-results screen back to the season picker.

    Triggered by '⬅️ К выбору сезона' shown when a specific-season search
    failed but the tracker has other seasons. Rebuilds the same picker UI
    from saved srch_base_title + srch_total_seasons.
    """
    query = update.callback_query
    await query.answer()

    base = context.user_data.get("srch_base_title", "")
    if not base:
        await query.edit_message_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    total_seasons = context.user_data.get("srch_total_seasons")
    season_count_label = f" ({total_seasons} сез.)" if total_seasons else ""
    quality_hint = _series_quality_hint(context.user_data.get("srch_picked_quality", ""))
    # Reuse the cached set if we already populated it on first picker entry —
    # avoids redundant Plex API calls when bouncing back from a 0-results screen.
    plex_seasons = context.user_data.get("srch_plex_seasons")
    if plex_seasons is None:
        plex_seasons = await _get_plex_seasons_for_series(base)
        context.user_data["srch_plex_seasons"] = plex_seasons
    plex_line = _series_plex_seasons_line(plex_seasons, total_seasons)
    await query.edit_message_text(
        f"📺 Сериал: «{base}»{season_count_label}\n"
        f"{plex_line}{quality_hint}Выберите сезон:",
        reply_markup=_season_select_keyboard(total_seasons, plex_seasons=plex_seasons),
    )
    return SEARCH_SEASON_SELECT


async def search_season_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from the season picker to the 'series added' success message.

    Restores the success_text+keyboard that was shown right after the season
    was downloaded, so the user can either tap '🔎 Другой сезон' again later
    or just keep that message as a download record. Keeps srch_series_query
    populated so the picker can be re-opened.
    """
    query = update.callback_query
    await query.answer()

    success_text = context.user_data.get("srch_series_success_text", "")
    task_id = context.user_data.get("srch_series_success_task_id", "")
    if not success_text:
        await query.edit_message_text(
            "Запрос потерян. Используйте /status для текущей задачи."
        )
        return ConversationHandler.END

    # Re-arm the offer so '🔎 Другой сезон' on the restored card works again.
    base_title = context.user_data.get("srch_base_title", "")
    if base_title:
        context.user_data["srch_series_query"] = base_title

    await query.edit_message_text(
        success_text,
        reply_markup=_search_after_add_keyboard(task_id),
    )
    return SEARCH_RESULTS


async def search_season_input_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User wants to type a custom season number — prompt them."""
    query = update.callback_query
    await query.answer()

    base = context.user_data.get("srch_base_title", "")
    await query.edit_message_text(
        f"Введите номер сезона для «{base}»:" if base else "Введите номер сезона:"
    )
    return SEARCH_SEASON_SELECT


async def search_season_got_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a season number after the manual-input prompt."""
    text = (update.message.text or "").strip()
    base = context.user_data.get("srch_base_title", "")

    if not base:
        await update.message.reply_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    if not text.isdigit():
        await update.message.reply_text(
            f"Пожалуйста, введите номер сезона цифрой для «{base}»:"
        )
        return SEARCH_SEASON_SELECT

    season_num = int(text)
    quality_suffix = _quality_to_query_suffix(context.user_data.get("srch_picked_quality", ""))
    search_query = _normalize_season_in_query(f"{base} Сезон {season_num}{quality_suffix}")
    return await _run_search(update.message.reply_text, context, search_query)


async def _download_and_add(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    index: int,
    *,
    subscribe: bool = False,
    _skip_plex_check: bool = False,
) -> int:
    """Shared implementation for direct-download and direct-subscribe from the results list.

    Downloads the torrent at *index*, adds it to Download Station, optionally
    creates a subscription, then shows a success (or error) message.
    Returns the next ConversationHandler state.
    """
    results = context.user_data.get("srch_results", [])
    if index < 0 or index >= len(results):
        await query.edit_message_text("Результат недоступен.")
        return ConversationHandler.END

    result = results[index]
    context.user_data["srch_picked"] = index
    topic_id = result.get("topic_id", "")
    source = result.get("source", "rutracker")

    # --- Plex duplicate check ---
    if not _skip_plex_check and PLEX_ENABLED:
        raw_title = result.get("movie_title") or result.get("title") or ""
        raw_year = result.get("year") or _movie_extract_year(raw_title) or 0
        req_quality = _plex_quality_from_result(result)
        display_title = result.get("title") or raw_title
        plex_check = _plex_pre_check(raw_title, int(raw_year), req_quality)
        if plex_check is not None:
            context.user_data["plex_pending"] = {
                "type": "search",
                "index": index,
                "subscribe": subscribe,
            }
            await query.edit_message_text(
                _plex_confirm_text(plex_check, display_title, req_quality),
                reply_markup=_plex_confirm_keyboard(),
                parse_mode="HTML",
            )
            return SEARCH_PLEX_CONFIRM
        # Movie check returned None — try the TV-series path before downloading.
        if _plex_is_series(raw_title):
            series_query = _extract_series_base_query(raw_title) or ""
            season_num = _extract_season_from_query(raw_title)
            series_check = await _plex_pre_check_series(series_query, season_num, req_quality)
            if series_check is not None:
                context.user_data["plex_pending"] = {
                    "type": "search",
                    "index": index,
                    "subscribe": subscribe,
                }
                await query.edit_message_text(
                    _plex_series_confirm_text(series_check, display_title, req_quality),
                    reply_markup=_plex_confirm_keyboard(),
                    parse_mode="HTML",
                )
                return SEARCH_PLEX_CONFIRM

    await query.edit_message_text("⏳ Скачиваю torrent-файл…")

    title = result["title"]
    safe_name = _safe_filename(f"{title}.torrent")
    temp_path = _temp_path(safe_name)
    task_id = ""
    download_method = "torrent-файл"
    tracker_result: TrackerApplyResult | None = None

    chat_id = query.message.chat.id if query.message else None

    # Snapshot existing task IDs before any create_magnet call so we can
    # identify the newly created task when DS doesn't return an ID immediately.
    try:
        _before_tasks = await asyncio.to_thread(ds_client.list_tasks)
        known_task_ids: set[str] = {t["id"] for t in _before_tasks if t.get("id")}
    except DownloadStationError:
        known_task_ids = set()

    try:
        if result.get("torrent_url") and jackett_client:
            # Jackett result: download via Jackett proxy (uniform for all indexers).
            # On failure, re-search to get a fresh proxy URL (Jackett re-authenticates
            # with the tracker as part of the search), then retry once before
            # falling back to magnet.
            from jackett import _sanitize_error_text as _jk_sanitize
            logger.info(
                "jackett download attempt: %s",
                _jk_sanitize(result["torrent_url"], jackett_client._api_key),
            )
            try:
                torrent_bytes = await asyncio.to_thread(
                    jackett_client.download_torrent, result["torrent_url"]
                )
                temp_path.write_bytes(torrent_bytes)
                task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
            except JackettMagnetRedirect as magnet_redir:
                # Tracker has no .torrent — its download URL redirects to magnet.
                magnet = magnet_redir.magnet_url or result.get("magnet_url", "")
                if not magnet:
                    raise JackettError("Torrent-файл недоступен и magnet-ссылка отсутствует.") from magnet_redir
                logger.info("Jackett redirected to magnet — using it directly")
                task_id = await asyncio.to_thread(ds_client.create_magnet, magnet)
                if not task_id:
                    task_id = await _wait_for_magnet_task_id(magnet, known_task_ids, query.message)
                download_method = "magnet"
            except JackettError as torrent_err:
                logger.warning("torrent_url download failed (%s), refreshing via re-search", torrent_err)
                await query.edit_message_text("⏳ Обновляю раздачи, повторяю попытку…")
                fresh_url = await _refresh_jackett_torrent_url(jackett_client, result, context)
                if fresh_url:
                    try:
                        torrent_bytes = await asyncio.to_thread(
                            jackett_client.download_torrent, fresh_url
                        )
                        temp_path.write_bytes(torrent_bytes)
                        task_id = await asyncio.to_thread(
                            ds_client.create_torrent_file, temp_path, safe_name
                        )
                    except JackettError as retry_err:
                        logger.warning("retry also failed (%s), trying magnet", retry_err)
                        if result.get("magnet_url"):
                            task_id = await asyncio.to_thread(
                                ds_client.create_magnet, result["magnet_url"]
                            )
                            if not task_id:
                                task_id = await _wait_for_magnet_task_id(
                                    result["magnet_url"], known_task_ids, query.message
                                )
                            download_method = "magnet"
                        else:
                            raise retry_err
                elif result.get("magnet_url"):
                    logger.warning("re-search found no fresh URL, falling back to magnet")
                    task_id = await asyncio.to_thread(ds_client.create_magnet, result["magnet_url"])
                    if not task_id:
                        task_id = await _wait_for_magnet_task_id(
                            result["magnet_url"], known_task_ids, query.message
                        )
                    download_method = "magnet"
                else:
                    raise torrent_err
        elif source == "rutracker" and topic_id and rutracker_client:
            # Direct Rutracker search result (not via Jackett) — use rutracker_client
            torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
            temp_path.write_bytes(torrent_bytes)
            task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
        elif result.get("magnet_url"):
            # Fallback: magnet link (no .torrent available)
            task_id = await asyncio.to_thread(ds_client.create_magnet, result["magnet_url"])
            if not task_id:
                task_id = await _wait_for_magnet_task_id(
                    result["magnet_url"], known_task_ids, query.message
                )
            download_method = "magnet"
        else:
            await query.edit_message_text("Не удалось скачать торрент: нет доступного источника.")
            return ConversationHandler.END

        _remember_task_owner(task_id, chat_id)
        _remember_task_meta(task_id, _build_task_meta_from_result(result, source="search"))

        if temp_path.exists():
            if _torrent_file_is_private(temp_path):
                tracker_result = TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")
                _mark_tracker_processed_if_final(task_id, tracker_result)
            else:
                await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
                tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
                _mark_tracker_processed_if_final(task_id, tracker_result)
        else:
            # magnet path — no torrent file to check
            await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
            tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
            _mark_tracker_processed_if_final(task_id, tracker_result)

        if subscribe:
            if source == "jackett":
                sub_key = f"jackett:{uuid.uuid4().hex[:8]}"
                subs = state_store.load_topic_subscriptions()
                now_text = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", title))
                subs[sub_key] = build_jackett_subscription(
                    chat_id=chat_id,
                    query=search_query,
                    result=result,
                    seen_results=context.user_data.get("srch_results", []),
                    added_at=now_text,
                )
                state_store.save_topic_subscriptions(subs)
                logger.info("Jackett subscription added: key=%s query=%s", sub_key, subs[sub_key]["query"])
            else:
                # Rutracker topic subscription (existing logic)
                episode_info = _parse_episode_info(title)
                if episode_info and chat_id:
                    subs = state_store.load_topic_subscriptions()
                    subs[topic_id] = {
                        "chat_id": chat_id,
                        "title": title,
                        "last_episode_end": episode_info[0],
                        "total_episodes": episode_info[1],
                        "added_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                    }
                    state_store.save_topic_subscriptions(subs)
                    logger.info(
                        "Subscription added: topic=%s chat=%s episodes=%s/%s",
                        topic_id, chat_id, episode_info[0], episode_info[1],
                    )

        added_msg = _task_added_message(
            download_method, title=title, task_id=task_id, tracker_result=tracker_result
        )
        suffix = "\n\n🔔 Буду следить за новыми сериями." if subscribe else ""
        success_text = f"{added_msg}{suffix}"

        series_query = _extract_series_base_query(title)
        _card_chat_id = _chat_id_from_query(query)
        _card_msg_id = _message_id_from_message(query.message) if query.message else None
        if series_query:
            context.user_data["srch_series_query"] = series_query
            # Remember the quality of the release the user actually picked so the
            # next-season search can suggest the same filter.
            context.user_data["srch_picked_quality"] = _plex_quality_from_result(result)
            # Remember the success message + task_id so the season picker can offer
            # a "⬅️ Назад" button that restores this view instead of force-cancelling.
            context.user_data["srch_series_success_text"] = success_text
            context.user_data["srch_series_success_task_id"] = task_id
            await query.edit_message_text(
                success_text, reply_markup=_search_after_add_keyboard(task_id)
            )
            _register_task_card_from_query(query, task_id)
            if task_id and _card_chat_id and _card_msg_id:
                _start_task_card_refresh(context.application, _card_chat_id, _card_msg_id, task_id)
            return SEARCH_RESULTS

        await query.edit_message_text(success_text, reply_markup=_task_reply_markup(task_id))
        _register_task_card_from_query(query, task_id)
        if task_id and _card_chat_id and _card_msg_id:
            _start_task_card_refresh(context.application, _card_chat_id, _card_msg_id, task_id)
    except (RutrackerError, JackettError, DownloadStationError) as e:
        await query.edit_message_text(f"Ошибка: {e}")
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

    return ConversationHandler.END


async def search_direct_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажатие «⬇️ Скачать» прямо в списке результатов."""
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка при разборе запроса.")
        return ConversationHandler.END
    return await _download_and_add(query, context, index, subscribe=False)


async def search_direct_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажатие «🔔 Следить» прямо в списке результатов."""
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка при разборе запроса.")
        return ConversationHandler.END
    return await _download_and_add(query, context, index, subscribe=True)


async def plex_confirm_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirmed downloading despite Plex duplicate warning."""
    query = update.callback_query
    await query.answer()

    pending = context.user_data.pop("plex_pending", None)
    if not pending:
        await query.edit_message_text("Данные потеряны — начните загрузку заново.")
        return ConversationHandler.END

    if pending["type"] == "search":
        return await _download_and_add(
            query, context, pending["index"],
            subscribe=pending.get("subscribe", False),
            _skip_plex_check=True,
        )

    # magnet / torrent — handled via global plex_confirm_standalone below
    await query.edit_message_text("Неизвестный тип ожидания.")
    return ConversationHandler.END


async def plex_cancel_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancelled download from Plex duplicate warning dialog (inside search flow)."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("plex_pending", None)
    chat_id = query.message.chat.id if query.message else None
    try:
        await query.message.delete()
    except Exception:
        pass
    if chat_id:
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Отменено"))
    return ConversationHandler.END


async def plex_confirm_standalone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plex:confirm for magnet/torrent uploads (outside ConversationHandler)."""
    query = update.callback_query
    await query.answer()

    pending = context.user_data.pop("plex_pending", None)
    if not pending:
        await query.edit_message_text("Данные потеряны — пришлите файл или ссылку заново.")
        return

    chat_id = query.message.chat.id if query.message else None

    if pending["type"] == "magnet":
        magnet_uri = pending.get("magnet_uri", "")
        if not magnet_uri:
            await query.edit_message_text("Магнет-ссылка потеряна — пришлите её заново.")
            return
        await _do_process_magnet(query.message, context, magnet_uri, chat_id=chat_id)

    elif pending["type"] == "torrent":
        temp_path_str = pending.get("temp_path", "")
        safe_name = pending.get("safe_name", "download.torrent")
        temp_path = Path(temp_path_str)
        if not temp_path_str or not temp_path.exists():
            await query.edit_message_text("Torrent-файл не найден — пришлите его заново.")
            return
        await _do_process_torrent(query.message, context, temp_path, safe_name, chat_id=chat_id)

    else:
        await query.edit_message_text("Неизвестный тип ожидания.")


async def plex_cancel_standalone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plex:cancel for magnet/torrent uploads (outside ConversationHandler)."""
    query = update.callback_query
    await query.answer()
    pending = context.user_data.pop("plex_pending", None)

    # Clean up temp file if it was a torrent
    if pending and pending.get("type") == "torrent":
        temp_path_str = pending.get("temp_path", "")
        if temp_path_str:
            try:
                Path(temp_path_str).unlink(missing_ok=True)
            except OSError:
                pass

    chat_id = query.message.chat.id if query.message else None
    try:
        await query.message.delete()
    except Exception:
        pass
    if chat_id:
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Отменено"))


async def search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    has_photo = context.user_data.pop("srch_confirm_has_photo", False)
    photo_msg_id = context.user_data.pop("srch_confirm_message_id", None)
    photo_chat_id = context.user_data.pop("srch_confirm_chat_id", None)

    if update.callback_query:
        await update.callback_query.answer()
        chat_id = update.callback_query.message.chat.id if update.callback_query.message else None
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        if chat_id:
            asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Отменено"))
    elif update.message:
        chat_id = update.message.chat.id
        # If a photo confirm card is still open in the chat, delete it
        if has_photo and photo_msg_id and photo_chat_id:
            try:
                await context.bot.delete_message(chat_id=photo_chat_id, message_id=photo_msg_id)
            except Exception:
                pass
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Отменено"))

    for key in (
        "srch_query", "srch_search_query", "srch_settings", "srch_results",
        "srch_picked", "srch_kp_info", "srch_results_page",
        "srch_base_title", "srch_total_seasons", "srch_series_query",
        "srch_picked_quality", "srch_series_success_text", "srch_series_success_task_id",
        "srch_plex_seasons",
        "srch_ui_msg_id", "srch_ui_chat_id", "srch_banner",
        "srch_jackett_indexers", "srch_jackett_selected", "srch_source",
        "srch_picker_return_to", "srch_jackett_mode",
    ):
        context.user_data.pop(key, None)

    return ConversationHandler.END


async def search_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Re-run the most recent search. Triggered from error-message buttons (srch:retry).

    Works both as a ConversationHandler entry_point (conversation already ended)
    and within active states, so the retry button functions in all situations.
    """
    query = update.callback_query
    await query.answer()
    search_query = (
        context.user_data.get("srch_search_query")
        or context.user_data.get("srch_query", "")
    ).strip()
    if not search_query:
        await query.edit_message_text(
            "Запрос потерян — начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    return await _run_search(search_query, query.edit_message_text, context)


async def search_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Called when the search ConversationHandler times out due to inactivity."""
    has_photo = context.user_data.pop("srch_confirm_has_photo", False)
    photo_msg_id = context.user_data.pop("srch_confirm_message_id", None)
    photo_chat_id = context.user_data.pop("srch_confirm_chat_id", None)

    if has_photo and photo_msg_id and photo_chat_id:
        try:
            await context.bot.delete_message(chat_id=photo_chat_id, message_id=photo_msg_id)
        except Exception:
            pass

    # If a Plex confirm dialog was pending when the conversation expired, also
    # discard its temp .torrent file so it doesn't sit in TMP_DIR until the next
    # bot restart. (No-op for magnet/search variants — they have no temp_path.)
    _cleanup_plex_pending(context.user_data.pop("plex_pending", None))

    for key in (
        "srch_query", "srch_search_query", "srch_settings", "srch_results",
        "srch_picked", "srch_kp_info", "srch_results_page",
        "srch_base_title", "srch_total_seasons", "srch_series_query",
        "srch_picked_quality", "srch_series_success_text", "srch_series_success_task_id",
        "srch_plex_seasons",
        "srch_ui_msg_id", "srch_ui_chat_id", "srch_banner",
        "srch_jackett_indexers", "srch_jackett_selected", "srch_source",
        "srch_picker_return_to", "srch_jackett_mode",
    ):
        context.user_data.pop(key, None)

    return ConversationHandler.END


def _cleanup_plex_pending(pending: object) -> None:
    """Delete the temp .torrent file associated with an abandoned plex_pending entry.

    plex_pending dicts for type='torrent' carry a temp_path. For other types
    (magnet, search) there's nothing to clean up.
    """
    if not isinstance(pending, dict):
        return
    temp_path_str = pending.get("temp_path")
    if not temp_path_str:
        return
    try:
        Path(temp_path_str).unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove abandoned plex_pending temp file %s", temp_path_str, exc_info=True)


async def search_jackett_check_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point from the Jackett subscription 'view results' button."""
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return ConversationHandler.END

    parts = (query.data or "").split(":", 2)
    if len(parts) < 3:
        return ConversationHandler.END
    sub_key = parts[2]

    subs = state_store.load_topic_subscriptions()
    sub = subs.get(sub_key)
    if not sub or sub.get("type") != "jackett":
        await query.edit_message_text("Подписка не найдена.")
        return ConversationHandler.END

    chat_id = query.message.chat.id if query.message else None
    if not _can_manage_subscription(chat_id, sub):
        await query.edit_message_text("Эта подписка не относится к вашему чату.")
        return ConversationHandler.END

    search_query = sub.get("query", "")
    if not search_query or jackett_client is None:
        await query.edit_message_text("Подписка или Jackett недоступны.")
        return ConversationHandler.END

    context.user_data["srch_query"] = search_query
    return await _run_search(query.edit_message_text, context, search_query)


# --- Subscription management ---


async def subs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import html as _html
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return

    subs = state_store.load_topic_subscriptions()
    chat_id = update.effective_chat.id if update.effective_chat else None
    is_admin = _is_admin_chat(chat_id)
    my_subs = {
        tid: sub for tid, sub in subs.items()
        if (is_admin or sub.get("chat_id") == chat_id) and sub.get("type") != "jackett"
    }
    jackett_subs_all = {
        k: v for k, v in subs.items()
        if v.get("type") == "jackett" and (is_admin or v.get("chat_id") == chat_id)
    }

    is_movie_sub = _is_movie_subscribed(chat_id) if chat_id else False
    if not my_subs and not jackett_subs_all and not is_movie_sub:
        await update.message.reply_text("📭 Активных подписок нет.")
        return

    total_count = len(my_subs) + len(jackett_subs_all) + (1 if is_movie_sub else 0)
    lines = [f"🔔 Активные подписки ({total_count}):"]
    rows = []

    for i, (topic_id, sub) in enumerate(my_subs.items(), 1):
        short = _format_sub_title(sub.get("title", ""))
        ep_end = sub.get("last_episode_end", "?")
        total = sub.get("total_episodes", "?")
        lines.append(f"\n{i}. {_html.escape(short)}\n   📺 {ep_end} из {total} эп.")
        if sub.get("unavailable_at"):
            lines.append("   ⚠️ Тема недоступна, проверка приостановлена.")
        rows.append([
            InlineKeyboardButton(
                f"🔕 {i}. Отписаться",
                callback_data=f"{SUB_CALLBACK_PREFIX}:unsub:{topic_id}",
            )
        ])

    offset = len(my_subs)
    for i, (key, sub) in enumerate(jackett_subs_all.items(), offset + 1):
        query_text = sub.get("query", "?")
        short_q = query_text[:40] + "…" if len(query_text) > 40 else query_text
        last_check = sub.get("last_check", "—")
        lines.append(f"\n📡 <b>{_html.escape(short_q)}</b>")
        lines.append(f"   Проверено: {last_check}")
        rows.append([
            InlineKeyboardButton(
                f"🔄 {short_q[:20]}",
                callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_view:{key}",
            ),
            InlineKeyboardButton("🗑️", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}"),
        ])

    if is_movie_sub:
        lines.append("\n🎬 <b>Подписка на новинки:</b> включена")
        rows.append([
            InlineKeyboardButton(
                "🔕 Отписаться от /new",
                callback_data=f"{SUB_CALLBACK_PREFIX}:new_unsub",
            )
        ])

    if _next_subscription_check_at is not None:
        next_dt = datetime.fromtimestamp(_next_subscription_check_at, DISPLAY_TIMEZONE)
        lines.append(f"\n🕐 Следующая проверка: {next_dt.strftime('%H:%M')}")

    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")


async def sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return

    parts = query.data.split(":", 2)
    if len(parts) < 2:
        return

    action = parts[1]
    topic_id = parts[2] if len(parts) > 2 else ""
    chat_id = query.message.chat.id if query.message else None

    if action == "new_unsub":
        if chat_id:
            _set_movie_subscription(chat_id, False)
        await query.edit_message_reply_markup(reply_markup=None)
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "🔕 Уведомления о новинках отключены"))
        return

    if len(parts) < 3:
        return

    if action == "unsub":
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(topic_id)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text("Эта подписка не относится к вашему чату.")
            return
        sub = subs.pop(topic_id, None)
        state_store.save_topic_subscriptions(subs)
        if sub:
            short = _format_sub_title(sub.get("title", ""))
            await query.edit_message_text(f"🔕 Подписка отменена:\n{short}")
        else:
            await query.edit_message_text("Подписка не найдена.")

    elif action == "jackett_unsub":
        key = topic_id
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(key)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text("Эта подписка не относится к вашему чату.")
            return
        sub = subs.pop(key, None)
        state_store.save_topic_subscriptions(subs)
        if sub:
            await query.edit_message_text(f"🔕 Подписка отменена:\n{sub.get('query', key)}")
        else:
            await query.edit_message_text("Подписка не найдена.")

    elif action in {"admin_unsub", "admin_jackett_unsub"}:
        if not _is_admin_chat(chat_id):
            await query.edit_message_text("Только администратор может управлять всеми подписками.")
            return

        subs = state_store.load_topic_subscriptions()
        sub = subs.pop(topic_id, None)
        state_store.save_topic_subscriptions(subs)
        if not sub:
            await query.edit_message_text("Подписка не найдена.")
            return

        text, keyboard = _build_admin_subscriptions_view()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def _check_jackett_sub_via_rutracker_direct(
    app: Application,
    subs: dict,
    key: str,
    sub: dict,
) -> bool:
    """Check a Jackett subscription directly via Rutracker when possible.

    When the stored ``topic_url`` is a Rutracker topic URL and ``rutracker_client``
    is available, uses the lightweight ``get_topic_title`` + ``download_torrent``
    path instead of a full Jackett search.  This is faster, more precise, and
    avoids Jackett availability issues for known Rutracker topics.

    Returns True  → subscription handled; caller should skip the Jackett-search path.
    Returns False → Rutracker unavailable/not configured; caller falls back to Jackett.
    """
    topic_url = str(sub.get("topic_url") or "")
    topic_id = _extract_rutracker_topic_id(topic_url)
    if not topic_id or not rutracker_client:
        return False

    chat_id = sub.get("chat_id")
    now_text = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")

    try:
        new_title = await asyncio.to_thread(rutracker_client.get_topic_title, topic_id)
    except RutrackerTopicUnavailable as e:
        sub["unavailable_at"] = now_text
        sub["unavailable_reason"] = str(e)
        short = _format_sub_title(sub.get("title", "")) or topic_url
        if chat_id:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ Подписка больше недоступна на Rutracker.\n\n"
                        f"{short}\n\n"
                        "Проверка приостановлена."
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🗑️ Удалить подписку",
                            callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}",
                        )
                    ]]),
                )
            except Exception:
                logger.warning("Failed to notify chat %s about unavailable Jackett/RT sub", chat_id, exc_info=True)
        logger.info("Jackett/RT sub topic unavailable: key=%s topic=%s", key, topic_id)
        return True  # handled — stop checking this subscription
    except RutrackerError as e:
        logger.warning("Rutracker direct check failed for sub %s (%s) — falling back to Jackett", key, e)
        return False  # fall through to Jackett search

    # No new-episode info in title → nothing actionable
    new_info = _parse_episode_info(new_title)
    sub["last_check"] = now_text
    if new_info is None:
        return True

    new_end, new_total = new_info
    last_end = int(sub.get("last_episode_end") or 0)
    if new_end <= last_end:
        return True  # no progress

    # New episodes detected — download torrent directly from Rutracker.
    safe_name = _safe_filename(f"rutracker_{topic_id}.torrent")
    temp_path = _temp_path(safe_name)
    task_id = ""
    try:
        torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
        temp_path.write_bytes(torrent_bytes)
        task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
        if chat_id and task_id:
            _remember_task_owner(task_id, chat_id)
            _remember_task_meta(
                task_id,
                _build_task_meta_from_title(new_title or "", source="jackett_sub"),
            )
    except (RutrackerError, DownloadStationError) as e:
        logger.warning("Failed to download Rutracker update for Jackett sub %s: %s", key, e)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

    is_complete = new_end >= new_total
    short_q = str(sub.get("query") or sub.get("title") or key)
    short_q = short_q[:40] + "…" if len(short_q) > 40 else short_q
    progress = f"\nСерии: {last_end} → {new_end} из {new_total}"

    # Update stored state
    sub["last_episode_end"] = new_end
    sub["total_episodes"] = new_total
    sub["title"] = new_title
    # Remove subscription only when the season is done AND the torrent was
    # successfully handed off to Download Station.  If the download failed,
    # keep the subscription so the next check can retry.
    if is_complete and task_id:
        subs.pop(key, None)

    # Build notification
    if task_id and is_complete:
        text = (
            f"🔔 Подписка «{short_q}» — сезон завершён! ✅\n"
            f"{progress}\n"
            "Торрент обновлён в Download Station. Подписка снята."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 К загрузкам", callback_data=_task_callback("list", task_id)),
        ]])
    elif task_id:
        text = (
            f"🔔 Подписка «{short_q}» обновилась — задача добавлена!\n"
            f"\n🔎 {new_title}{progress}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Статус задачи", callback_data=_task_callback("info", task_id)),
            InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}"),
        ]])
    else:
        text = (
            f"🔔 Подписка «{short_q}» обновилась, но скачать не удалось.\n"
            f"\n🔎 {new_title}{progress}\n\n"
            "⚠️ Скачайте вручную."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Посмотреть и скачать", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_view:{key}"),
            InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}"),
        ]])

    if chat_id:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        except Exception:
            logger.warning("Failed to notify chat %s for Jackett/RT sub update", chat_id, exc_info=True)

    logger.info(
        "Jackett/RT sub updated via Rutracker direct: key=%s ep=%s→%s/%s task=%s",
        key, last_end, new_end, new_total, task_id,
    )
    return True


async def _jackett_subscription_auto_download(candidate: JackettResult, chat_id: int | None) -> str:
    """Try to download the subscription update and add it to Download Station.

    Returns the task_id string (may be empty if DS didn't return one immediately).
    Raises on unrecoverable errors so the caller can fall back to notify-only mode.
    """
    title = candidate.title
    safe_name = _safe_filename(f"{title}.torrent")
    temp_path = _temp_path(safe_name)
    task_id = ""

    try:
        if candidate.torrent_url:
            try:
                torrent_bytes = await asyncio.to_thread(
                    jackett_client.download_torrent, candidate.torrent_url
                )
                temp_path.write_bytes(torrent_bytes)
                task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
                if chat_id and task_id:
                    _remember_task_owner(task_id, chat_id)
                    _remember_task_meta(task_id, _build_task_meta_from_title(title, source="jackett_sub"))
                # Add public trackers unless private torrent
                if not _torrent_file_is_private(temp_path):
                    await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
                return task_id
            except JackettMagnetRedirect as redir:
                magnet = redir.magnet_url or candidate.magnet_url or ""
                if not magnet:
                    raise
                logger.info("Subscription torrent redirected to magnet, using it directly")
                task_id = await asyncio.to_thread(ds_client.create_magnet, magnet)
                if chat_id and task_id:
                    _remember_task_owner(task_id, chat_id)
                    _remember_task_meta(task_id, _build_task_meta_from_title(title, source="jackett_sub"))
                return task_id
            except (JackettError, DownloadStationError) as e:
                logger.warning("Subscription torrent_url download failed (%s), trying magnet", e)

        if candidate.magnet_url:
            task_id = await asyncio.to_thread(ds_client.create_magnet, candidate.magnet_url)
            if chat_id and task_id:
                _remember_task_owner(task_id, chat_id)
                _remember_task_meta(task_id, _build_task_meta_from_title(title, source="jackett_sub"))
            return task_id

        raise JackettError("Нет torrent_url и magnet_url у кандидата")
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


async def _check_jackett_subscriptions(app: Application) -> None:
    """Check all Jackett query-based subscriptions for new results."""
    if jackett_client is None:
        return

    subs = state_store.load_topic_subscriptions()
    jackett_subs = {k: v for k, v in subs.items() if v.get("type") == "jackett"}
    if not jackett_subs:
        return

    logger.debug("Checking %d Jackett subscription(s)", len(jackett_subs))
    changed = False

    for key, sub in list(jackett_subs.items()):
        try:
            # Skip subscriptions that have been marked permanently unavailable.
            if sub.get("unavailable_at"):
                continue

            # Fast path: if the topic is on Rutracker, use the direct API instead of
            # a full Jackett text search (cheaper, more reliable).
            if await _check_jackett_sub_via_rutracker_direct(app, subs, key, sub):
                changed = True
                continue

            search_query = sub.get("query", "")
            if not search_query:
                continue

            # Narrow search to the subscription's tracker if known (faster, less noise).
            tracker_id = str(sub.get("tracker") or "").strip().lower() or None
            indexers_filter: list[str] | None = [tracker_id] if tracker_id else None

            new_results = await asyncio.to_thread(
                jackett_client.search,
                search_query,
                indexers=indexers_filter,
                fetch_limit=JACKETT_FETCH_LIMIT,
            )
            candidate = select_jackett_subscription_candidate(sub, new_results)
            if candidate is None:
                sub["last_check"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                changed = True
                continue

            chat_id = sub.get("chat_id")
            short_q = search_query[:40] + "…" if len(search_query) > 40 else search_query
            episode_info = _parse_episode_info(candidate.title)
            progress = f"\nСерии: {episode_info[0]} из {episode_info[1]}" if episode_info else ""

            # Try to auto-download the update (same as Rutracker subscription behaviour).
            task_id: str | None = None
            try:
                task_id = await _jackett_subscription_auto_download(candidate, chat_id)
                logger.info(
                    "Subscription auto-download: key=%s task_id=%s title=%s",
                    key, task_id, candidate.title,
                )
            except Exception as dl_err:
                logger.warning(
                    "Subscription auto-download failed for %s: %s — sending notify-only",
                    key, dl_err,
                )

            # Build notification text depending on whether auto-download succeeded.
            if task_id is not None:
                text = (
                    f"🔔 Подписка «{short_q}» обновилась — задача добавлена в DS!\n"
                    f"\n🔎 {candidate.title}"
                    f"\n📦 {candidate.size} | 🌱 {candidate.seeders} | 📡 {candidate.tracker}"
                    f"{progress}"
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🔄 Статус задачи",
                        callback_data=_task_callback("info", task_id),
                    ),
                    InlineKeyboardButton(
                        "🔕 Отписаться",
                        callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}",
                    ),
                ]])
            else:
                text = (
                    f"🔔 Найдено обновление подписки «{short_q}»:\n"
                    f"\n🔎 {candidate.title}"
                    f"\n📦 {candidate.size} | 🌱 {candidate.seeders} | 📡 {candidate.tracker}"
                    f"{progress}"
                    "\n\n⚠️ Авто-загрузка не удалась — скачайте вручную."
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🔍 Посмотреть и скачать",
                        callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_view:{key}",
                    ),
                    InlineKeyboardButton(
                        "🔕 Отписаться",
                        callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}",
                    ),
                ]])

            sent = not chat_id
            if chat_id:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
                    sent = True
                except Exception:
                    logger.warning("Failed to notify chat %s for Jackett subscription", chat_id, exc_info=True)

            if sent:
                checked_at = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                if sub.get("version") == JACKETT_SUBSCRIPTION_SCHEMA:
                    apply_jackett_subscription_match(sub, candidate, checked_at)
                else:
                    seen_titles = list(dict.fromkeys([
                        *sub.get("seen_titles", []),
                        *(r.title for r in new_results),
                    ]))
                    sub["seen_titles"] = seen_titles[-100:]
                    sub["last_check"] = checked_at
                changed = True
                logger.info("Jackett subscription update: key=%s title=%s", key, candidate.title)

        except Exception:
            logger.warning("Error checking Jackett subscription %s", key, exc_info=True)

    if changed:
        state_store.save_topic_subscriptions(subs)


async def _mark_rutracker_subscription_unavailable(
    app: Application,
    topic_id: str,
    sub: dict,
    error: RutrackerTopicUnavailable,
) -> bool:
    if sub.get("unavailable_at"):
        return False

    chat_id = sub.get("chat_id")
    short = _format_sub_title(sub.get("title", "")) or topic_id
    text = (
        "⚠️ Подписка Rutracker больше недоступна.\n\n"
        f"{short}\n"
        f"ID темы: {topic_id}\n\n"
        "Я приостановил проверку этой подписки. Если тема удалена окончательно, её можно убрать."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑️ Удалить подписку", callback_data=f"{SUB_CALLBACK_PREFIX}:unsub:{topic_id}")
    ]])

    if chat_id:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        except Exception:
            logger.warning("Failed to notify chat %s about unavailable topic %s", chat_id, topic_id, exc_info=True)
            return False

    sub["unavailable_at"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    sub["unavailable_reason"] = str(error)
    logger.info("Rutracker subscription paused: topic=%s reason=%s", topic_id, error)
    return True


def _rutracker_subscription_pending_payload(
    *,
    title: str,
    last_episode_end: int,
    new_episode_end: int,
    total_episodes: int,
    task_id: str,
    complete: bool,
) -> dict:
    return {
        "title": title,
        "last_episode_end": last_episode_end,
        "new_episode_end": new_episode_end,
        "total_episodes": total_episodes,
        "task_id": task_id,
        "complete": complete,
        "created_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
    }


def _rutracker_subscription_notification(pending: dict, topic_id: str) -> tuple[str, InlineKeyboardMarkup]:
    title = str(pending.get("title") or topic_id)
    short = _format_sub_title(title)
    last_end = pending.get("last_episode_end", "?")
    new_end = pending.get("new_episode_end", "?")
    new_total = pending.get("total_episodes", "?")
    task_id = str(pending.get("task_id") or "")
    is_complete = bool(pending.get("complete"))

    if is_complete:
        text = (
            f"🔔 {short}: сезон завершён!\n"
            f"Эпизодов: {last_end} → {new_end} из {new_total} ✅\n"
            "Торрент обновлён в Download Station.\n"
            "Подписка снята автоматически."
        )
        return text, _download_list_keyboard()

    text = (
        f"🔔 {short}: новая серия!\n"
        f"Эпизодов: {last_end} → {new_end} из {new_total}\n"
        "Торрент обновлён в Download Station."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 К загрузкам", callback_data=_task_callback("list", task_id)),
        InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:unsub:{topic_id}"),
    ]])
    return text, keyboard


async def _deliver_rutracker_subscription_notification(
    app: Application,
    subs: dict[str, dict],
    topic_id: str,
    sub: dict,
    pending: dict,
) -> bool:
    text, keyboard = _rutracker_subscription_notification(pending, topic_id)
    chat_id = sub.get("chat_id")
    if chat_id:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        except Exception:
            logger.warning("Failed to notify chat %s for subscription update", chat_id, exc_info=True)
            return False

    if pending.get("complete"):
        subs.pop(topic_id, None)
    else:
        sub["last_episode_end"] = pending.get("new_episode_end", sub.get("last_episode_end", 0))
        sub["total_episodes"] = pending.get("total_episodes", sub.get("total_episodes", 0))
        sub["title"] = pending.get("title", sub.get("title", ""))
        sub.pop("pending_notification", None)

    return True


async def _check_subscriptions(app: Application) -> None:
    if not rutracker_client:
        await _check_jackett_subscriptions(app)
        return

    subs = state_store.load_topic_subscriptions()
    if not subs:
        await _check_jackett_subscriptions(app)
        return

    logger.debug("Checking %d topic subscription(s)", len(subs))
    changed = False

    for topic_id, sub in list(subs.items()):
        if sub.get("type") == "jackett":
            continue
        if sub.get("unavailable_at"):
            continue
        try:
            pending = sub.get("pending_notification")
            if isinstance(pending, dict):
                if await _deliver_rutracker_subscription_notification(app, subs, topic_id, sub, pending):
                    changed = True
                    logger.info("Subscription notification delivered: topic=%s", topic_id)
                continue

            try:
                new_title = await asyncio.to_thread(rutracker_client.get_topic_title, topic_id)
            except RutrackerTopicUnavailable as e:
                if await _mark_rutracker_subscription_unavailable(app, topic_id, sub, e):
                    changed = True
                continue

            new_info = _parse_episode_info(new_title)
            if new_info is None:
                continue

            new_end, new_total = new_info
            last_end = sub.get("last_episode_end", 0)

            if new_end <= last_end:
                continue

            chat_id = sub.get("chat_id")
            is_complete = new_end >= new_total

            safe_name = _safe_filename(f"rutracker_{topic_id}.torrent")
            temp_path = _temp_path(safe_name)
            task_id = ""
            try:
                torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
                temp_path.write_bytes(torrent_bytes)
                task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
                if chat_id:
                    _remember_task_owner(task_id, chat_id)
                    _remember_task_meta(
                        task_id,
                        _build_task_meta_from_title(new_title or "", source="rutracker_sub"),
                    )
            except (RutrackerError, DownloadStationError) as e:
                logger.warning("Failed to update subscription %s: %s", topic_id, e)
                continue
            finally:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

            pending = _rutracker_subscription_pending_payload(
                title=new_title,
                last_episode_end=last_end,
                new_episode_end=new_end,
                total_episodes=new_total,
                task_id=task_id,
                complete=is_complete,
            )
            if await _deliver_rutracker_subscription_notification(app, subs, topic_id, sub, pending):
                logger.info("Subscription update: topic=%s episodes=%s→%s/%s", topic_id, last_end, new_end, new_total)
            else:
                sub["pending_notification"] = pending
                logger.info("Subscription update pending notification: topic=%s episodes=%s→%s/%s", topic_id, last_end, new_end, new_total)
            changed = True

        except Exception:
            logger.warning("Error checking subscription for topic %s", topic_id, exc_info=True)

    if changed:
        state_store.save_topic_subscriptions(subs)

    await _check_jackett_subscriptions(app)


async def _subscription_check_loop(app: Application) -> None:
    global _next_subscription_check_at
    interval = SUBSCRIPTION_CHECK_INTERVAL_HOURS * 3600
    # Run immediately on startup so users don't wait N hours for the first check
    try:
        await _run_background_step("initial subscription check", lambda: _check_subscriptions(app))
        while True:
            _next_subscription_check_at = time.time() + interval
            await asyncio.sleep(interval)
            _next_subscription_check_at = None
            await _run_background_step("subscription check", lambda: _check_subscriptions(app))
    except asyncio.CancelledError:
        logger.info("Subscription check loop stopped")
        raise


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /start from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    if RUTRACKER_ENABLED or JACKETT_ENABLED:
        kp_hint = " или ссылку с Кинопоиска" if KINOPOISK_ENABLED else ""
        search_hint = f"Напишите название фильма{kp_hint} — бот найдёт торрент сам.\n"
    else:
        search_hint = ""
    await update.message.reply_text(
        "Пришлите .torrent файлом или magnet-ссылку сообщением.\n"
        f"{search_hint}"
        "Откройте /status, чтобы смотреть загрузки и управлять ими.\n"
        "Команды доступны через меню Telegram."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /help from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    if RUTRACKER_ENABLED or JACKETT_ENABLED:
        kp_hint = " или вставьте ссылку с Кинопоиска" if KINOPOISK_ENABLED else ""
        source_hint = "по Rutracker" if RUTRACKER_ENABLED else "через Jackett"
        search_lines = (
            f"- напишите название фильма/сериала{kp_hint} — сразу откроется поиск {source_hint};\n"
        )
    else:
        search_lines = ""
    movie_lines = ""
    if MOVIE_DISCOVERY_ENABLED and (RUTRACKER_ENABLED or JACKETT_ENABLED):
        movie_lines = (
            "- /new показывает кэш новинок фильмов и мультфильмов: свежие годы, 1080p/2160p, "
            "без сериалов, CAM/TS и adult-раздач;\n"
        )
    admin_lines = ""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if _is_admin_chat(chat_id):
        admin_lines = (
            "- /admin открывает админ-панель с диагностикой и главной сводкой;\n"
            "- /users управляет доступом пользователей;\n"
            "- /status показывает все загрузки с переключателем «мои / все»;\n"
        )
    else:
        admin_lines = "- /status показывает ваши загрузки;\n"
    await update.message.reply_text(
        "Что умею:\n"
        "- принимаю .torrent файлы и magnet-ссылки, добавляю задачи в Download Station;\n"
        "- добавляю public-трекеры к новым задачам, если это включено;\n"
        "- присылаю уведомление с кнопками, когда загрузка завершилась или остановилась с ошибкой;\n"
        "- даю кнопки управления задачами: обновить статус, пауза, запуск, удаление;\n"
        "- могу удалить завершённые задачи из списка вручную или автоматически;\n"
        f"{search_lines}"
        f"{movie_lines}"
        f"{admin_lines}"
        "- /ping проверяет связь с ботом;\n"
        "- /id показывает ваш chat_id",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✖️ Закрыть", callback_data="help:close")]]
        ),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_admin_chat(chat_id):
        return

    progress_message = await update.message.reply_text("🛠️ Обновляю админ-панель…")
    await _safe_edit_message(
        progress_message,
        await _build_admin_panel_text(),
        parse_mode="HTML",
        reply_markup=_admin_panel_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    admin_chat_id = query.message.chat.id if query.message else None
    if not _is_admin_chat(admin_chat_id):
        await query.answer("Только для администратора", show_alert=True)
        logger.warning("Rejected admin callback from chat_id=%s", admin_chat_id)
        return

    await query.answer()

    parts = (query.data or "").split(":", 1)
    action = parts[1] if len(parts) > 1 else "home"

    if action == "close":
        chat_id = query.message.chat.id if query.message else None
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            logger.debug("Failed to delete admin panel message", exc_info=True)
        if chat_id:
            asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Закрыто"))
        return

    if action == "diagnostics":
        await _safe_edit_callback(query, "🧭 Проверяю сервисы…")
        await _safe_edit_callback(
            query,
            await _build_diagnostics_text(),
            parse_mode="HTML",
            reply_markup=_admin_diagnostics_keyboard(),
        )
        return

    if action == "subscriptions":
        text, keyboard = _build_admin_subscriptions_view()
        await _safe_edit_callback(query, text, parse_mode="HTML", reply_markup=keyboard)
        return

    if action == "force_kp_refresh":
        cache = _load_movie_discovery_cache()
        kp_cache_dict = cache.get("kp_cache") if isinstance(cache.get("kp_cache"), dict) else {}
        total_entries = len(kp_cache_dict)
        found_entries = sum(1 for e in kp_cache_dict.values() if isinstance(e, dict) and e.get("kp_id"))
        miss_entries = total_entries - found_entries

        # Budget estimate: each search_movie() call ≈ 1.5 HTTP requests on average
        today_str = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d")
        stats = cache.get("kp_api_stats")
        if isinstance(stats, dict) and stats.get("date") == today_str:
            searches_today = int(stats.get("searches") or 0)
        else:
            searches_today = 0
        http_used_est = round(searches_today * 1.5)
        remaining = max(0, _KP_DAILY_LIMIT - http_used_est)
        needed_http_est = round(total_entries * 1.5)
        can_full = total_entries > 0 and remaining >= needed_http_est

        runs_needed = (total_entries + _KP_MAX_STALE_REFRESH - 1) // _KP_MAX_STALE_REFRESH if total_entries > 0 else 1

        text_lines = [
            "🔄 <b>Принудительное обновление KP кэша</b>",
            "",
            f"Записей в кэше: <b>{total_entries}</b> "
            f"({found_entries} найдено · {miss_entries} не найдено)",
            "",
            "📊 <b>Бюджет API сегодня</b>",
            f"• Использовано: ~{http_used_est} из {_KP_DAILY_LIMIT} HTTP-вызовов",
            f"• Осталось: ~{remaining}",
            f"• Потребуется для полного обновления: ~{needed_http_est}",
            "",
        ]
        if can_full:
            text_lines.append("✅ Бюджета достаточно для обновления за один прогон.")
        else:
            text_lines.append(
                f"⚠️ Бюджета не хватит для одного прогона. "
                f"Постепенное обновление: по {_KP_MAX_STALE_REFRESH} записей в прогоне, "
                f"~{runs_needed} {_plural(runs_needed, 'прогон', 'прогона', 'прогонов')}."
            )

        await _safe_edit_callback(
            query,
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=_admin_kp_force_refresh_keyboard(can_full),
        )
        return

    if action == "confirm_force_kp_refresh_full":
        cache = _load_movie_discovery_cache()
        kp_cache_dict = cache.get("kp_cache") if isinstance(cache.get("kp_cache"), dict) else {}
        stale_ts = "2000-01-01T00:00:00+00:00"
        for entry in kp_cache_dict.values():
            if isinstance(entry, dict):
                entry["cached_at"] = stale_ts
        cache["kp_cache"] = kp_cache_dict
        _save_movie_discovery_cache(cache)
        asyncio.create_task(_refresh_movie_discovery_cache(max_stale_kp_refresh=None))
        await _safe_edit_callback(
            query,
            "🔄 <b>Запускаю полное обновление KP кэша</b>\n\n"
            f"Все <b>{len(kp_cache_dict)}</b> {_plural(len(kp_cache_dict), 'запись', 'записи', 'записей')} "
            f"помечены устаревшими.\n"
            "Обновление идёт в фоне — займёт несколько минут.",
            parse_mode="HTML",
            reply_markup=_admin_kp_cache_cleared_keyboard(),
        )
        return

    if action == "confirm_force_kp_refresh_gradual":
        cache = _load_movie_discovery_cache()
        kp_cache_dict = cache.get("kp_cache") if isinstance(cache.get("kp_cache"), dict) else {}
        stale_ts = "2000-01-01T00:00:00+00:00"
        for entry in kp_cache_dict.values():
            if isinstance(entry, dict):
                entry["cached_at"] = stale_ts
        cache["kp_cache"] = kp_cache_dict
        _save_movie_discovery_cache(cache)
        asyncio.create_task(_refresh_movie_discovery_cache())
        runs_needed = (len(kp_cache_dict) + _KP_MAX_STALE_REFRESH - 1) // _KP_MAX_STALE_REFRESH if kp_cache_dict else 1
        await _safe_edit_callback(
            query,
            "🔄 <b>Запускаю постепенное обновление KP кэша</b>\n\n"
            f"Все <b>{len(kp_cache_dict)}</b> {_plural(len(kp_cache_dict), 'запись', 'записи', 'записей')} "
            f"помечены устаревшими.\n"
            f"Обновляется по {_KP_MAX_STALE_REFRESH} за прогон — "
            f"~{runs_needed} {_plural(runs_needed, 'прогон', 'прогона', 'прогонов')} автообновления.",
            parse_mode="HTML",
            reply_markup=_admin_kp_cache_cleared_keyboard(),
        )
        return

    if action == "clear_kp_cache":
        cache = _load_movie_discovery_cache()
        entry_count = len(cache.get("kp_cache", {})) if isinstance(cache.get("kp_cache"), dict) else 0
        await _safe_edit_callback(
            query,
            f"🗑 <b>Очистить кэш результатов Кинопоиска?</b>\n\n"
            f"В кэше сейчас <b>{entry_count}</b> {_plural(entry_count, 'запись', 'записи', 'записей')}.\n"
            f"После очистки все фильмы будут запрошены заново при следующем обновлении новинок.",
            parse_mode="HTML",
            reply_markup=_admin_kp_cache_confirm_keyboard(),
        )
        return

    if action == "confirm_clear_kp_cache":
        cache = _load_movie_discovery_cache()
        old_size = len(cache.get("kp_cache", {})) if isinstance(cache.get("kp_cache"), dict) else 0
        cache["kp_cache"] = {}
        _save_movie_discovery_cache(cache)
        await _safe_edit_callback(
            query,
            f"✅ KP кеш очищен: удалено <b>{old_size}</b> {_plural(old_size, 'запись', 'записи', 'записей')}.\n\n"
            f"Кеш будет заполнен заново при следующем обновлении новинок.",
            parse_mode="HTML",
            reply_markup=_admin_kp_cache_cleared_keyboard(),
        )
        return

    if action == "movie_trackers":
        text, keyboard = await _movie_trackers_panel()
        await _safe_edit_callback(query, text, reply_markup=keyboard)
        return

    if action.startswith("tracker_toggle:"):
        tracker_id = action.split(":", 1)[1]
        settings = _load_movie_discovery_settings()
        known_ids: list[str] = settings.get("jackett_trackers_known") or []
        enabled_raw = settings.get("jackett_trackers_enabled")
        enabled_set: set[str] = set(enabled_raw) if enabled_raw is not None else set(known_ids)
        if tracker_id in enabled_set:
            enabled_set.discard(tracker_id)
        else:
            enabled_set.add(tracker_id)
        settings["jackett_trackers_enabled"] = sorted(enabled_set) if enabled_set else None
        _save_movie_discovery_settings(settings)
        asyncio.create_task(_recompute_movie_discovery_from_cache())
        text, keyboard = await _movie_trackers_panel()
        await _safe_edit_callback(query, text, reply_markup=keyboard)
        return

    if action == "tracker_enable_all":
        settings = _load_movie_discovery_settings()
        settings["jackett_trackers_enabled"] = None
        _save_movie_discovery_settings(settings)
        asyncio.create_task(_recompute_movie_discovery_from_cache())
        text, keyboard = await _movie_trackers_panel()
        await _safe_edit_callback(query, text, reply_markup=keyboard)
        return

    await _safe_edit_callback(query, "🛠️ Обновляю админ-панель…")
    await _safe_edit_callback(
        query,
        await _build_admin_panel_text(),
        parse_mode="HTML",
        reply_markup=_admin_panel_keyboard(),
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /ping from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    await update.message.reply_text("pong")


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)

    if not _is_allowed(update):
        logger.warning("Rejected /id from chat_id=%s", chat_id)
        await _reply_access_pending(update, context)
        return

    await update.message.reply_text(f"Ваш chat_id: {chat_id}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /status from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    await _delete_message_safely(context, chat.id, message.message_id, "status command")
    progress_message = await context.bot.send_message(chat_id=chat.id, text="📋 Получаю список загрузок…")

    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError as e:
        logger.exception("Failed to list Download Station tasks")
        await _safe_edit_message(progress_message, f"Не удалось получить задачи: {e}")
        return

    scope = _default_list_scope(chat.id)
    visible_tasks = _filter_tasks_for_scope(tasks, chat.id, scope)
    total_count = len(tasks) if _is_admin_chat(chat.id) else None
    await _replace_message_with_download_panel(
        progress_message,
        context,
        chat.id,
        visible_tasks,
        scope,
        total_count=total_count,
    )


async def movie_new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /new from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    if not _movie_discovery_enabled():
        await update.message.reply_text("Новинки недоступны: не настроен Rutracker или Jackett.")
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    cache = _load_movie_discovery_cache()
    if not cache.get("cards"):
        progress = await update.message.reply_text("🎬 Собираю новинки…")
        cache = await _refresh_movie_discovery_cache()
        await _safe_edit_message(
            progress,
            _format_movie_discovery_cache(cache),
            reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    # Re-apply Plex badges from current in-memory library (cheap, no network).
    # Needed after restart: JSON cache may have stale in_plex values.
    _enrich_cards_with_plex(cache.get("cards") or [])

    await update.message.reply_text(
        _format_movie_discovery_cache(cache),
        reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def movie_new_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return

    chat_id = query.message.chat.id if query.message else None
    await query.answer()
    await _safe_edit_callback(query, "🎬 Обновляю новинки…")
    cache = await _refresh_movie_discovery_cache()
    await _safe_edit_callback(
        query,
        _format_movie_discovery_cache(cache),
        reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def movie_new_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return

    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    try:
        if query.message:
            await query.message.delete()
    except Exception:
        logger.debug("Failed to delete movie discovery message", exc_info=True)
    if chat_id:
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Закрыто"))


async def movie_new_subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return
    chat_id = query.message.chat.id if query.message else None
    if chat_id:
        _set_movie_subscription(chat_id, True)
    await query.answer("Подписан на обновления 🔔")
    # Redraw keyboard so the button reflects the new state
    cache = _load_movie_discovery_cache()
    try:
        await query.edit_message_reply_markup(
            reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        )
    except Exception:
        pass


async def movie_new_unsubscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return
    chat_id = query.message.chat.id if query.message else None
    if chat_id:
        _set_movie_subscription(chat_id, False)
    await query.answer("Отписан от обновлений")
    cache = _load_movie_discovery_cache()
    try:
        await query.edit_message_reply_markup(
            reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        )
    except Exception:
        pass


async def movie_new_open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the '🎬 Открыть /new' button in movie discovery notifications."""
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    cache = _load_movie_discovery_cache()
    _enrich_cards_with_plex(cache.get("cards") or [])
    await _safe_edit_callback(
        query,
        _format_movie_discovery_cache(cache),
        reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def help_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return

    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    try:
        if query.message:
            await query.message.delete()
    except Exception:
        logger.debug("Failed to delete help message", exc_info=True)
    if chat_id:
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Закрыто"))


async def movie_new_show_releases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    try:
        index = int((query.data or "").split(":")[-1])
    except (TypeError, ValueError):
        await query.edit_message_text("Не удалось открыть новинку.")
        return ConversationHandler.END

    cache = _load_movie_discovery_cache()
    cards = cache.get("cards") if isinstance(cache.get("cards"), list) else []
    if index < 0 or index >= len(cards):
        await query.edit_message_text("Новинка не найдена. Обновите список.")
        return ConversationHandler.END

    card = cards[index]
    releases = [_movie_release_to_search_result(release) for release in card.get("releases", [])]
    releases = sorted(releases, key=_score_result, reverse=True)
    if not releases:
        await query.edit_message_text("По этой новинке пока нет подходящих раздач.")
        return ConversationHandler.END

    search_query = f"{card.get('title', '')} {card.get('year', '')}".strip()
    context.user_data["srch_results"] = releases
    context.user_data["srch_search_query"] = search_query
    context.user_data["srch_query"] = search_query
    context.user_data["srch_source"] = "movie_discovery"
    await query.edit_message_text(
        _build_results_text(releases, search_query, 0, banner="🎬 Раздачи по выбранной новинке"),
        reply_markup=_search_results_keyboard(releases, page=0, show_jackett_expand=False, show_jackett_direct=False, show_back_to_discovery=True),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return SEARCH_RESULTS


async def movie_new_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    chat_id = query.message.chat.id if query.message else None
    await query.answer()
    cache = _load_movie_discovery_cache()
    cards = cache.get("cards") if isinstance(cache.get("cards"), list) else []
    try:
        await query.edit_message_text(
            _format_movie_discovery_cache(cache),
            reply_markup=_movie_discovery_keyboard(cards, chat_id=chat_id),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except BadRequest as e:
        if not _is_message_not_modified(e):
            raise
    return ConversationHandler.END


def _format_users_panel(*, back_to_admin: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    admins = sorted(ADMIN_CHAT_IDS)
    permanent = sorted(ALLOWED_CHAT_IDS - ADMIN_CHAT_IDS)
    approved_users = {
        uid: info
        for uid, info in sorted(state_store.load_approved_users().items())
        if uid not in ALLOWED_CHAT_IDS and uid not in ADMIN_CHAT_IDS
    }

    lines = ["👥 Пользователи с доступом\n"]

    lines.append("👑 Администраторы:")
    if admins:
        lines.extend(f"  • {uid}" for uid in admins)
    else:
        lines.append("  (нет)")

    if permanent:
        lines.append("\n📌 Постоянные (из конфига):")
        lines.extend(f"  • {uid}" for uid in permanent)

    lines.append("\n✅ Одобренные:")
    if approved_users:
        for uid, info in approved_users.items():
            name = info.get("name", "")
            added_at = info.get("added_at", "")
            label = f"  • {uid}"
            if name:
                label += f" — {name}"
            if added_at:
                label += f"\n    📅 {added_at}"
            lines.append(label)
    else:
        lines.append("  (нет)")

    return "\n".join(lines), users_keyboard(approved_users, back_to_admin=back_to_admin)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_admin_chat(chat_id):
        return

    text, keyboard = _format_users_panel(back_to_admin=False)
    await update.message.reply_text(text, reply_markup=keyboard)


async def access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    admin_chat_id = query.message.chat.id if query.message else None
    if not _is_admin_chat(admin_chat_id):
        await query.answer("Только администратор может выдавать доступ", show_alert=True)
        logger.warning("Rejected access callback from chat_id=%s", admin_chat_id)
        return

    await query.answer()

    parts = (query.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "users_refresh":
        text, keyboard = _format_users_panel()
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    try:
        target_chat_id = int(parts[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Не удалось разобрать запрос доступа.")
        return

    if action == "approve":
        already_allowed = target_chat_id in _all_allowed_chat_ids()
        name = ACCESS_PENDING_USERS.pop(target_chat_id, "")
        state_store.add_approved_user(target_chat_id, name)

        note = "уже был разрешен" if already_allowed else "разрешен"
        label = f" ({name})" if name else ""
        await query.edit_message_text(f"Доступ {note}.\nchat_id: {target_chat_id}{label}")
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=(
                    "Доступ разрешен.\n"
                    "Пришлите .torrent файлом или magnet-ссылку сообщением."
                ),
            )
        except Exception:
            logger.warning("Failed to notify approved chat_id=%s", target_chat_id, exc_info=True)
        return

    if action == "deny":
        ACCESS_PENDING_USERS.pop(target_chat_id, None)
        await query.edit_message_text(f"Запрос доступа отклонен.\nchat_id: {target_chat_id}")
        return

    if action == "remove":
        state_store.remove_approved_user(target_chat_id)
        _revoke_chat_runtime_state(target_chat_id)
        ACCESS_PENDING_USERS.pop(target_chat_id, None)
        logger.info("Admin removed access for chat_id=%s", target_chat_id)
        text, keyboard = _format_users_panel()
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    await query.edit_message_text("Неизвестное действие с доступом.")


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        logger.warning("Rejected callback from chat_id=%s", _chat_id(update))
        return

    await query.answer()

    # Fast path: close (delete) the message — no task_id needed.
    if (query.data or "").startswith(f"{TASK_CALLBACK_PREFIX}:close"):
        chat_id = _chat_id_from_query(query)
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            logger.debug("Failed to delete task message on close", exc_info=True)
        if chat_id:
            asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Закрыто"))
        return

    try:
        _, action, task_id = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_text("Не удалось разобрать действие.")
        return

    chat_id = _chat_id_from_query(query)
    message_id = query.message.message_id if query.message else None
    # Cancel any running auto-refresh for this card before handling the action
    if chat_id and message_id:
        _cancel_task_card_refresh(chat_id, message_id)

    if action == "list":
        scope = _normalize_list_scope(task_id, chat_id)
        DOWNLOAD_PANEL_PAGES.pop(chat_id, None)
        _forget_task_card_message(chat_id, message_id)
        await _safe_edit_callback(query, "📋 Обновляю список загрузок…")
        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось получить задачи: {e}")
            return

        visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
        total_count = len(tasks) if _is_admin_chat(chat_id) else None
        await _edit_download_panel(query, context, visible_tasks, scope, total_count=total_count, page=0)
        return

    if action in ("page_prev", "page_next"):
        scope = _normalize_list_scope(task_id, chat_id)
        _forget_task_card_message(chat_id, message_id)
        await _safe_edit_callback(query, "📋 Обновляю список загрузок…")
        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось получить задачи: {e}")
            return

        visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
        total_pages = max(1, (len(visible_tasks) + TASK_LIST_PAGE_SIZE - 1) // TASK_LIST_PAGE_SIZE)
        current_page = DOWNLOAD_PANEL_PAGES.get(chat_id, 0)
        page = current_page - 1 if action == "page_prev" else current_page + 1
        page = max(0, min(page, total_pages - 1))
        total_count = len(tasks) if _is_admin_chat(chat_id) else None
        await _edit_download_panel(query, context, visible_tasks, scope, total_count=total_count, page=page)
        return

    if action == "delete_ask":
        if not _can_access_task_id(chat_id, task_id):
            await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
            return

        _forget_task_card_message(chat_id, message_id, task_id)
        # Show confirmation immediately — no network fetch needed.
        await query.edit_message_text(
            f"Удалить задачу из Download Station?\nID: {task_id}",
            reply_markup=_delete_confirm_keyboard(task_id),
        )
        return

    if action == "delete_finished_ask":
        scope = _normalize_list_scope(task_id, chat_id)
        await _safe_edit_callback(query, "🔎 Проверяю завершенные задачи…")
        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось получить задачи: {e}")
            return

        visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
        finished_ids = _finished_task_ids(visible_tasks)
        if not finished_ids:
            await query.edit_message_text(
                "Завершенных задач сейчас нет.",
                reply_markup=_tasks_keyboard(visible_tasks, scope=scope, is_admin=_is_admin_chat(chat_id)),
            )
            return

        await query.edit_message_text(
            f"Удалить завершенные задачи из Download Station?\n\nНайдено: {len(finished_ids)}",
            reply_markup=_delete_finished_confirm_keyboard(scope),
        )
        return

    if action == "delete_finished":
        scope = _normalize_list_scope(task_id, chat_id)
        await _safe_edit_callback(query, "🧹 Удаляю завершенные задачи…")
        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
            visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
            finished_ids = _finished_task_ids(visible_tasks)
            if not finished_ids:
                await query.edit_message_text(
                    "Завершенных задач сейчас нет.",
                    reply_markup=_tasks_keyboard(visible_tasks, scope=scope, is_admin=_is_admin_chat(chat_id)),
                )
                return

            await asyncio.to_thread(ds_client.delete_tasks, finished_ids)
            _forget_task_state(finished_ids)
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось удалить завершенные задачи: {e}")
            return

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError:
            await query.edit_message_text(f"Удалено завершенных задач: {len(finished_ids)}.")
            return

        visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
        total_count = len(tasks) if _is_admin_chat(chat_id) else None
        await _edit_message_as_download_panel(
            query,
            context,
            (
                f"Удалено завершенных задач: {len(finished_ids)}.\n\n"
                f"{_format_tasks(visible_tasks, scope=scope, total_count=total_count)}"
            ),
            reply_markup=_tasks_keyboard(visible_tasks, scope=scope, is_admin=_is_admin_chat(chat_id)),
        )
        DOWNLOAD_PANEL_HAD_ACTIVE[chat_id] = _has_active_tasks(visible_tasks)
        return

    if action == "trackers":
        if not _can_access_task_id(chat_id, task_id):
            await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
            return

        await _safe_edit_callback(query, "➕ Добавляю public-трекеры…")
        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось получить задачу: {e}")
            return

        task = _find_task(tasks, task_id)
        if not task:
            await query.edit_message_text(f"Задача не найдена.\nID: {task_id}")
            return
        if (task.get("type") or "").lower() != "bt":
            await query.edit_message_text(
                f"Public-трекеры доступны только для BT-задач.\n\n{_format_task_card(task)}",
                reply_markup=_make_task_keyboard(task_id, task.get("status", ""), task.get("type", "")),
            )
            return

        tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
        _mark_tracker_processed_if_final(task_id, tracker_result)

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
            task = _find_task(tasks, task_id) or task
        except DownloadStationError:
            pass

        tracker_lines = _tracker_result_lines(tracker_result) or ["Public-трекеры: выключены"]
        await query.edit_message_text(
            "\n".join(["Трекеры обновлены.", *tracker_lines, "", _format_task_card(task)]),
            reply_markup=_make_task_keyboard(task_id, task.get("status", ""), task.get("type", "")),
        )
        _register_task_card_from_query(query, task_id)
        return

    if action == "sub_notify":
        # Subscribe the current user to a "done" notification for this task.
        notified = _load_notified_tasks()
        raw = notified.get(task_id)
        if isinstance(raw, dict):
            subscribers: list[str] = raw.get("subscribers", [])
            subscriber_set = set(subscribers)
            subscriber_set.add(str(chat_id))
            raw["subscribers"] = sorted(subscriber_set)
            notified[task_id] = raw
        else:
            notified[task_id] = {
                "status": "",
                "sent": [],
                "failures": {},
                "subscribers": [str(chat_id)],
            }
        _save_notified_tasks(notified)

        if chat_id:
            asyncio.create_task(_send_auto_delete(context.bot, chat_id, "🔔 Уведомлю когда скачается!"))

        # Remove the subscribe button from the message so it can't be pressed again.
        try:
            if query.message:
                new_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Открыть задачу", callback_data=_task_callback("info", task_id))],
                    [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))],
                ])
                await query.edit_message_reply_markup(reply_markup=new_kb)
        except Exception:
            logger.debug("Failed to update keyboard after sub_notify", exc_info=True)
        return

    if action in {"resume", "pause", "delete"}:
        if not _can_access_task_id(chat_id, task_id):
            await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
            return

        action_progress = {
            "resume": "▶️ Отправляю команду запуска…",
            "pause": "⏸️ Отправляю команду паузы…",
            "delete": "🗑️ Удаляю задачу…",
        }[action]
        await _safe_edit_callback(query, action_progress)
        try:
            if action == "resume":
                await asyncio.to_thread(ds_client.resume_task, task_id)
                notice = "Команда запуска отправлена."
            elif action == "pause":
                await asyncio.to_thread(ds_client.pause_task, task_id)
                notice = "Команда паузы отправлена."
            else:
                await asyncio.to_thread(ds_client.delete_task, task_id)
                _forget_task_state([task_id])
                scope = _normalize_list_scope(task_id, chat_id)
                del_chat_id = chat_id
                del_msg_id = query.message.message_id if query.message else None
                await query.edit_message_text(
                    f"🗑 Задача удалена.\nID: {task_id}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "📋 К списку загрузок",
                            callback_data=_task_callback("list", scope),
                        )
                    ]]),
                )
                if del_chat_id and del_msg_id:
                    asyncio.create_task(
                        _delayed_delete_message(context.bot, del_chat_id, del_msg_id, delay=5.0)
                    )
                return
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось выполнить действие: {e}")
            return

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError:
            await query.edit_message_text(f"{notice}\nID: {task_id}")
            return

        task = _find_task(tasks, task_id)
        if task:
            await query.edit_message_text(
                f"{notice}\n\n{_format_task_card(task)}",
                reply_markup=_make_task_keyboard(task_id, task.get("status", ""), task.get("type", "")),
            )
            _register_task_card_from_query(query, task_id)
        else:
            await query.edit_message_text(f"{notice}\nID: {task_id}")
        return

    if not _can_access_task_id(chat_id, task_id):
        await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
        return

    await _safe_edit_callback(query, "🔎 Получаю задачу…")
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError as e:
        await query.edit_message_text(f"Не удалось получить задачу: {e}")
        return

    task = _find_task(tasks, task_id)
    if not task:
        await query.edit_message_text(f"Задача не найдена.\nID: {task_id}")
        return

    status = (task.get("status") or "").lower()
    await query.edit_message_text(
        _format_task_card(task),
        reply_markup=_make_task_keyboard(task_id, status, task.get("type", "")),
    )
    _register_task_card_from_query(query, task_id)

    # Start auto-refresh while the task is actively transferring
    if status in _ACTIVE_STATUSES and chat_id and message_id:
        _start_task_card_refresh(context.application, chat_id, message_id, task_id)


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /resume from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    if not context.args:
        await update.message.reply_text("Укажите id задачи, например: /resume dbid_421")
        return

    task_id = context.args[0].strip()
    if not _can_access_task_id(update.effective_chat.id if update.effective_chat else None, task_id):
        await update.message.reply_text("Эта задача не относится к вашим загрузкам.")
        return

    progress_message = await update.message.reply_text("▶️ Отправляю команду запуска…")
    try:
        await asyncio.to_thread(ds_client.resume_task, task_id)
    except DownloadStationError as e:
        logger.exception("Failed to resume Download Station task")
        await _safe_edit_message(progress_message, f"Не удалось запустить задачу {task_id}: {e}")
        return

    await _safe_edit_message(progress_message, f"Команда запуска отправлена для {task_id}.")


async def _do_process_magnet(
    progress_message,
    context: ContextTypes.DEFAULT_TYPE,
    magnet_uri: str,
    chat_id: int | None = None,
) -> None:
    """Core Download Station logic for adding a magnet link.

    *progress_message* is the message to edit with status updates.
    Used by both _process_magnet_uri and plex_confirm_standalone.
    """
    try:
        try:
            before_tasks = await asyncio.to_thread(ds_client.list_tasks)
            known_task_ids = {task["id"] for task in before_tasks if task.get("id")}
        except DownloadStationError:
            logger.warning("Failed to fetch task list before magnet create", exc_info=True)
            known_task_ids = set()

        task_id = await asyncio.to_thread(ds_client.create_magnet, magnet_uri)
        if not task_id:
            task_id = await _wait_for_magnet_task_id(magnet_uri, known_task_ids, progress_message)
        _remember_task_owner(task_id, chat_id)
        dn = _extract_magnet_dn(magnet_uri)
        if dn:
            _remember_task_meta(task_id, _build_task_meta_from_title(dn, source="magnet"))
        await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
        tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
        _mark_tracker_processed_if_final(task_id, tracker_result)
    except DownloadStationError as e:
        logger.exception("Failed to create Download Station task")
        await _safe_edit_message(progress_message, f"Не удалось добавить magnet-ссылку: {e}")
        return

    msg_text = _task_added_message("magnet-ссылка", task_id=task_id, tracker_result=tracker_result)
    if not task_id:
        msg_text += "\n\nID пока не появился. Откройте список загрузок через кнопку ниже."

    await _safe_edit_message(
        progress_message,
        msg_text,
        reply_markup=_task_reply_markup(task_id) or _download_list_keyboard(),
    )
    _register_task_card_from_message(
        progress_message,
        task_id,
        fallback_chat_id=chat_id,
    )
    if task_id:
        _pm_chat_id = _chat_id_from_message(progress_message)
        _pm_msg_id = _message_id_from_message(progress_message)
        if _pm_chat_id and _pm_msg_id:
            _start_task_card_refresh(context.application, _pm_chat_id, _pm_msg_id, task_id)


async def _do_process_torrent(
    progress_message,
    context: ContextTypes.DEFAULT_TYPE,
    temp_path: Path,
    safe_name: str,
    chat_id: int | None = None,
) -> None:
    """Core Download Station logic for adding a torrent file.

    *progress_message* is the message to edit with status updates.
    Used by both handle_doc and plex_confirm_standalone.
    Cleans up *temp_path* on exit.
    """
    try:
        logger.info("Creating Download Station task from torrent file %s", safe_name)
        await _safe_edit_message(progress_message, "⏳ Добавляю torrent-файл в Download Station…")
        task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
        _remember_task_owner(task_id, chat_id)
        meta_title = _normalize_torrent_filename_for_match(safe_name)
        if meta_title:
            _remember_task_meta(task_id, _build_task_meta_from_title(meta_title, source="torrent_file"))
        if _torrent_file_is_private(temp_path):
            tracker_result = TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")
            _mark_tracker_processed_if_final(task_id, tracker_result)
        else:
            await _safe_edit_message(progress_message, "➕ Добавляю public-трекеры…")
            await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
            tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
            _mark_tracker_processed_if_final(task_id, tracker_result)

        await _safe_edit_message(
            progress_message,
            _task_added_message(
                "torrent-файл",
                title=safe_name,
                task_id=task_id,
                tracker_result=tracker_result,
            ),
            reply_markup=_task_reply_markup(task_id),
        )
        _register_task_card_from_message(progress_message, task_id, fallback_chat_id=chat_id)
        if task_id and progress_message:
            _hd_chat_id = _chat_id_from_message(progress_message)
            _hd_msg_id = _message_id_from_message(progress_message)
            if _hd_chat_id and _hd_msg_id:
                _start_task_card_refresh(context.application, _hd_chat_id, _hd_msg_id, task_id)
    except Exception as e:
        logger.exception("Failed to process torrent")
        error_text = f"Ошибка при обработке .torrent: {type(e).__name__}: {e}"
        await _safe_edit_message(progress_message, error_text)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


async def _process_magnet_uri(update: Update, context: ContextTypes.DEFAULT_TYPE, magnet_uri: str) -> None:
    """Add a magnet-link task to Download Station. Shared by handle_text and text_message_entry."""
    chat_id = update.effective_chat.id if update.effective_chat else None

    # --- Plex duplicate check (best-effort: extract title + year from magnet dn= param) ---
    if PLEX_ENABLED:
        dn_raw = _extract_magnet_dn(magnet_uri)
        if dn_raw:
            raw_year = _movie_extract_year(dn_raw) or 0
            req_quality = _plex_quality_from_title(dn_raw)
            plex_check = _plex_pre_check(dn_raw, int(raw_year), req_quality)
            if plex_check is not None:
                progress_message = await update.message.reply_text(_magnet_wait_text(0, 8))
                context.user_data["plex_pending"] = {
                    "type": "magnet",
                    "magnet_uri": magnet_uri,
                }
                await _safe_edit_message(
                    progress_message,
                    _plex_confirm_text(plex_check, dn_raw, req_quality),
                    reply_markup=_plex_confirm_keyboard(),
                    parse_mode="HTML",
                )
                return
            # Movie check missed — try the TV-series path.
            if _plex_is_series(dn_raw):
                series_query = _extract_series_base_query(dn_raw) or ""
                season_num = _extract_season_from_query(dn_raw)
                series_check = await _plex_pre_check_series(series_query, season_num, req_quality)
                if series_check is not None:
                    progress_message = await update.message.reply_text(_magnet_wait_text(0, 8))
                    context.user_data["plex_pending"] = {
                        "type": "magnet",
                        "magnet_uri": magnet_uri,
                    }
                    await _safe_edit_message(
                        progress_message,
                        _plex_series_confirm_text(series_check, dn_raw, req_quality),
                        reply_markup=_plex_confirm_keyboard(),
                        parse_mode="HTML",
                    )
                    return

    progress_message = await update.message.reply_text(_magnet_wait_text(0, 8))
    logger.info("Creating Download Station task from magnet chat_id=%s", _chat_id(update))
    await _do_process_magnet(progress_message, context, magnet_uri, chat_id=chat_id)


async def text_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ConversationHandler entry point for all plain text messages.

    Routes plain text automatically:
    • magnet link     → add to Download Station, end conversation
    • Kinopoisk URL   → Kinopoisk lookup + quality-options keyboard
    • anything else   → treat as a Rutracker search query
    """
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    # If a Plex confirm dialog is still pending from a previous interaction and
    # the user moved on (sent a new search/magnet/link), clean up the abandoned
    # temp .torrent file before processing the new request.
    _cleanup_plex_pending(context.user_data.pop("plex_pending", None))

    # If a 'series added' offer was sitting in user_data and the user switched
    # to a new search/torrent/link instead of tapping "🔎 Другой сезон", drop
    # the stale series state so it doesn't leak across unrelated flows.
    for stale_key in (
        "srch_series_query", "srch_series_success_text", "srch_series_success_task_id",
        "srch_picked_quality", "srch_plex_seasons",
    ):
        context.user_data.pop(stale_key, None)

    # 1. Magnet link — handle immediately, don't start a search conversation.
    magnet_uri = _find_magnet(text)
    if magnet_uri:
        await _process_magnet_uri(update, context, magnet_uri)
        return ConversationHandler.END

    # 2. Kinopoisk URL — delegate to the existing KP flow if the client is ready.
    if kinopoisk_client and extract_kp_id(text):
        return await kp_link_entry(update, context)

    # 3. Anything else — treat as a search query if any search client is ready.
    if rutracker_client is None and jackett_client is None:
        await update.message.reply_text("Пришлите .torrent файл или magnet-ссылку.")
        return ConversationHandler.END

    # If we're re-entering from an active search state, delete the previous
    # search UI message so it doesn't clutter the chat.
    # Exception: if the message is a task card (download was triggered from the
    # search results and the message was edited in-place into a task card), do NOT
    # delete it — the background monitor still needs it to show progress updates.
    old_msg_id = context.user_data.pop("srch_ui_msg_id", None)
    old_chat_id = context.user_data.pop("srch_ui_chat_id", None)
    if old_msg_id and old_chat_id:
        is_task_card = any(
            (old_chat_id, old_msg_id) in msgs
            for msgs in TASK_CARD_MESSAGES.values()
        )
        if not is_task_card:
            try:
                await context.bot.delete_message(chat_id=old_chat_id, message_id=old_msg_id)
            except Exception:
                pass  # message may already be deleted or too old — ignore

    return await search_got_query(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safety-net text handler — should normally never fire.

    The ConversationHandler (entry point text_message_entry) is always
    registered and consumes every text message.  This handler is kept as a
    last-resort fallback in case something unexpected falls through.
    """
    if not _is_allowed(update):
        logger.warning("Rejected text from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    magnet_uri = _find_magnet(update.message.text or "")
    if not magnet_uri:
        await update.message.reply_text("Пришлите .torrent файл или magnet-ссылку.")
        return

    await _process_magnet_uri(update, context, magnet_uri)


async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected document from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    temp_path = None
    progress_message = None
    chat_id = update.effective_chat.id if update.effective_chat else None

    try:
        doc = update.message.document
        if not doc:
            return

        original_name = doc.file_name or "download.torrent"

        if not original_name.lower().endswith(".torrent"):
            await update.message.reply_text("Нужен именно файл .torrent.")
            return

        if doc.file_size and doc.file_size > MAX_TORRENT_FILE_BYTES:
            await update.message.reply_text(
                f"Файл слишком большой. Максимальный размер: {_format_size(MAX_TORRENT_FILE_BYTES)}."
            )
            return

        safe_name = _safe_filename(original_name)
        temp_path = _temp_path(safe_name)
        progress_message = await update.message.reply_text("⏳ Обрабатываю torrent-файл…")

        logger.info("Downloading %s from chat_id=%s", original_name, _chat_id(update))
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(temp_path))

        if not _looks_like_torrent(temp_path):
            await _safe_edit_message(progress_message, "Файл не похож на настоящий .torrent.")
            return

        # --- Plex duplicate check (best-effort: use torrent filename) ---
        if PLEX_ENABLED:
            # safe_name has underscores instead of spaces (safe_filename strips non-ASCII
            # and replaces separators). Convert back so normalize_movie_title can parse it:
            # "____They_Will_Kill_You___2026_____" → "They Will Kill You 2026"
            plex_title = _normalize_torrent_filename_for_match(safe_name)
            raw_year = _movie_extract_year(plex_title) or 0
            req_quality = _plex_quality_from_title(plex_title)
            plex_check = _plex_pre_check(plex_title, int(raw_year), req_quality)
            if plex_check is not None:
                context.user_data["plex_pending"] = {
                    "type": "torrent",
                    "temp_path": str(temp_path),
                    "safe_name": safe_name,
                }
                await _safe_edit_message(
                    progress_message,
                    _plex_confirm_text(plex_check, original_name, req_quality),
                    reply_markup=_plex_confirm_keyboard(),
                    parse_mode="HTML",
                )
                return  # temp_path kept — will be cleaned up by confirm/cancel/timeout
            # Movie check missed — try the TV-series path.
            if _plex_is_series(plex_title):
                series_query = _extract_series_base_query(plex_title) or ""
                season_num = _extract_season_from_query(plex_title)
                series_check = await _plex_pre_check_series(series_query, season_num, req_quality)
                if series_check is not None:
                    context.user_data["plex_pending"] = {
                        "type": "torrent",
                        "temp_path": str(temp_path),
                        "safe_name": safe_name,
                    }
                    await _safe_edit_message(
                        progress_message,
                        _plex_series_confirm_text(series_check, original_name, req_quality),
                        reply_markup=_plex_confirm_keyboard(),
                        parse_mode="HTML",
                    )
                    return

        await _do_process_torrent(progress_message, context, temp_path, safe_name, chat_id=chat_id)
        temp_path = None  # _do_process_torrent owns cleanup now

    except Exception as e:
        logger.exception("Failed to process torrent")

        try:
            error_text = f"Ошибка при обработке .torrent: {type(e).__name__}: {e}"
            if progress_message is not None:
                await _safe_edit_message(progress_message, error_text)
            else:
                await update.message.reply_text(error_text)
        except Exception:
            pass

    finally:
        try:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        except Exception:
            logger.warning("Failed to remove temporary torrent file", exc_info=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error:
        logger.error(
            "Unhandled telegram error",
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )


def _cleanup_tmp_dir() -> None:
    try:
        if TMP_DIR.exists():
            count = 0
            for f in TMP_DIR.iterdir():
                try:
                    f.unlink()
                    count += 1
                except OSError:
                    pass
            if count:
                logger.info("Cleaned up %d stale temp files from %s", count, TMP_DIR)
    except OSError:
        logger.warning("Failed to clean up temp dir %s", TMP_DIR, exc_info=True)


def _run_polling(app: Application) -> None:
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=TELEGRAM_ALLOWED_UPDATES,
    )


async def setup_bot_commands(app: Application) -> None:
    global BACKGROUND_MONITOR_TASK, TRACKER_BACKGROUND_TASK, PROGRESS_UPDATE_TASK, MOVIE_DISCOVERY_TASK

    _cleanup_tmp_dir()
    commands = list(BOT_COMMANDS)
    admin_commands = commands + [
        BotCommand("admin", "Админ-панель"),
        BotCommand("users", "Управление доступом пользователей"),
    ]
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await app.bot.set_my_commands(admin_commands, scope={"type": "chat", "chat_id": admin_id})
        except Exception:
            pass
    await app.bot.set_my_commands(commands)
    logger.info("Telegram command menu updated")

    if _tracker_background_enabled():
        TRACKER_BACKGROUND_TASK = app.create_task(_tracker_background_loop())

    if _task_maintenance_enabled():
        BACKGROUND_MONITOR_TASK = app.create_task(_task_maintenance_loop(app))

    PROGRESS_UPDATE_TASK = app.create_task(_progress_update_loop(app))
    logger.info("Progress update loop started, interval=%ss", PROGRESS_UPDATE_INTERVAL_SECONDS)

    if _subscription_monitor_enabled():
        global SUBSCRIPTION_MONITOR_TASK
        SUBSCRIPTION_MONITOR_TASK = app.create_task(_subscription_check_loop(app))
        logger.info("Subscription check loop started, interval=%sh", SUBSCRIPTION_CHECK_INTERVAL_HOURS)

    if _movie_discovery_enabled():
        global MOVIE_NOTIFY_PENDING_TASK
        MOVIE_DISCOVERY_TASK = app.create_task(_movie_discovery_loop(app))
        logger.info("Movie discovery loop started, interval=%sh", MOVIE_DISCOVERY_INTERVAL_HOURS)
        MOVIE_NOTIFY_PENDING_TASK = app.create_task(_movie_notification_pending_loop(app))
        logger.info("Movie notification pending loop started")

    if PLEX_ENABLED:
        app.create_task(_plex_cache_loop())
        logger.info("Plex library cache loop started, interval=%ss", _PLEX_CACHE_INTERVAL)


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_bot_commands)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("new", movie_new_command))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=f"^{ADMIN_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(access_callback, pattern=f"^{ACCESS_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(task_callback, pattern=f"^{TASK_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(sub_callback, pattern=f"^{SUB_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(movie_new_refresh_callback, pattern=r"^new:refresh$"))
    app.add_handler(CallbackQueryHandler(movie_new_close_callback, pattern=r"^new:close$"))
    app.add_handler(CallbackQueryHandler(movie_new_subscribe_callback, pattern=r"^new:subscribe$"))
    app.add_handler(CallbackQueryHandler(movie_new_unsubscribe_callback, pattern=r"^new:unsubscribe$"))
    app.add_handler(CallbackQueryHandler(movie_new_open_callback, pattern=r"^new:open$"))
    app.add_handler(CallbackQueryHandler(help_close_callback, pattern=r"^help:close$"))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("subs", subs_command))
    # Always register the ConversationHandler so text_message_entry intercepts
    # all plain-text messages (magnets, KP links, search queries).
    # When Rutracker is disabled text_message_entry falls back gracefully.
    app.add_handler(ConversationHandler(
        entry_points=[
            # Every plain text message (KP links, search queries, magnets)
            # is routed by text_message_entry.
            MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
            # Jackett subscription "view results" entry point.
            CallbackQueryHandler(
                search_jackett_check_entry,
                pattern=rf"^{SUB_CALLBACK_PREFIX}:jackett_view:",
            ),
            CallbackQueryHandler(movie_new_show_releases, pattern=r"^new:show:\d+$"),
            # Re-run the last search from an error message (conversation already ended).
            CallbackQueryHandler(search_retry, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:retry$"),
        ],
            states={
                SEARCH_OPTIONS: [
                    CallbackQueryHandler(search_quick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:quick$"),
                    CallbackQueryHandler(search_show_advanced, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:adv$"),
                    CallbackQueryHandler(search_pick_tracker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:pick_tracker:"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                ],
                SEARCH_ADVANCED: [
                    CallbackQueryHandler(search_toggle_setting, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:quality:"),
                    CallbackQueryHandler(search_toggle_setting, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:toggle:"),
                    CallbackQueryHandler(search_do, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:do_search$"),
                    CallbackQueryHandler(search_pick_tracker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:pick_tracker:"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                ],
                SEARCH_RESULTS: [
                    CallbackQueryHandler(search_direct_download, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:dl:\d+$"),
                    CallbackQueryHandler(search_direct_subscribe, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub:\d+$"),
                    CallbackQueryHandler(search_results_page, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:res_page:"),
                    CallbackQueryHandler(search_series_entry, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:series_base$"),
                    CallbackQueryHandler(search_no_quality, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:no_quality$"),
                    CallbackQueryHandler(search_switch_trackers, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:switch_trackers$"),
                    CallbackQueryHandler(search_direct_rutracker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:direct_rt$"),
                    # Legacy patterns — delegate to new handlers
                    CallbackQueryHandler(search_expand_jackett, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:expand_jackett$"),
                    CallbackQueryHandler(search_switch_trackers, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:jackett_direct$"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    CallbackQueryHandler(movie_new_back, pattern=r"^new:back$"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                ],
                SEARCH_SEASON_SELECT: [
                    CallbackQueryHandler(search_season_pick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season:\d+$"),
                    CallbackQueryHandler(search_season_skip, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_skip$"),
                    CallbackQueryHandler(search_season_input_ask, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_input$"),
                    CallbackQueryHandler(search_season_back, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_back$"),
                    CallbackQueryHandler(search_season_back_to_picker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_back_to_picker$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, search_season_got_input),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                ],
                SEARCH_JACKETT_SELECT: [
                    CallbackQueryHandler(
                        search_jackett_toggle,
                        pattern=rf"^{SEARCH_CALLBACK_PREFIX}:{JACKETT_SELECT_PREFIX}_toggle:",
                    ),
                    CallbackQueryHandler(
                        search_jackett_do,
                        pattern=rf"^{SEARCH_CALLBACK_PREFIX}:{JACKETT_SELECT_PREFIX}_search$",
                    ),
                    CallbackQueryHandler(search_jackett_back, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:jk_back$"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                ],
                SEARCH_PLEX_CONFIRM: [
                    CallbackQueryHandler(plex_confirm_download, pattern=r"^plex:confirm$"),
                    CallbackQueryHandler(plex_cancel_download, pattern=r"^plex:cancel$"),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, search_timeout),
                    CallbackQueryHandler(search_timeout),
                ],
            },
            fallbacks=[CommandHandler("cancel", search_cancel)],
            per_user=True,
            per_chat=True,
            conversation_timeout=600,
        )
    )
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Global Plex confirmation handlers (for magnet/torrent, outside ConversationHandler)
    app.add_handler(CallbackQueryHandler(plex_confirm_standalone, pattern=r"^plex:confirm$"))
    app.add_handler(CallbackQueryHandler(plex_cancel_standalone, pattern=r"^plex:cancel$"))
    app.add_handler(MessageReactionHandler(reaction_easter_egg))
    app.add_error_handler(error_handler)

    logger.info(
        "Bot started. tmp_dir=%s allowed_chats=%s ds_url=%s destination=%s "
        "rutracker=%s kinopoisk=%s plex=%s",
        TMP_DIR,
        sorted(_all_allowed_chat_ids()),
        DS_URL,
        DS_DESTINATION,
        f"enabled (user={RUTRACKER_USERNAME})" if RUTRACKER_ENABLED else "disabled (no credentials)",
        "enabled" if KINOPOISK_ENABLED else "disabled",
        "enabled" if PLEX_ENABLED else "disabled",
    )
    _run_polling(app)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Bot crashed")
        raise
