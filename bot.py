import asyncio
import html as html_module
import json
import logging
import os
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
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
    _score_result,
    _short_title,
    _tracker_abbr,
)
from keyboards import (
    ACCESS_CALLBACK_PREFIX,
    JACKETT_SELECT_PREFIX,
    SEARCH_CALLBACK_PREFIX,
    TASK_CALLBACK_PREFIX,
    TASK_LIST_PAGE_SIZE,
    TASK_LIST_SCOPE_ALL,
    TASK_LIST_SCOPE_DEFAULT,
    TASK_LIST_SCOPE_MY,
    _SRCH_DEFAULT_SETTINGS,
    _SRCH_QUALITY_OPTIONS,
    _access_approval_keyboard,
    _access_callback,
    _delete_confirm_keyboard,
    _delete_finished_confirm_keyboard,
    _download_list_keyboard,
    _final_notification_keyboard,
    _finished_task_ids,
    _jackett_select_keyboard,
    _new_task_keyboard,
    _search_advanced_keyboard,
    _search_after_add_keyboard,
    _no_quality_keyboard,
    _search_options_keyboard,
    _search_results_keyboard,
    _season_select_keyboard,
    SEARCH_PAGE_SIZE,
    SUB_CALLBACK_PREFIX,
    _task_callback,
    _task_keyboard,
    _task_reply_markup,
    _tasks_keyboard,
)
from jackett import JackettError, JackettResult
from kinopoisk import KinopoiskError, KinopoiskInfo, KP_URL_RE, extract_kp_id
from rutracker import RutrackerError, RutrackerResult
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
TOPIC_SUBSCRIPTIONS_FILE = settings.topic_subscriptions_file
SUBSCRIPTION_CHECK_INTERVAL_HOURS = settings.subscription_check_interval_hours
JACKETT_URL = settings.jackett_url
JACKETT_API_KEY = settings.jackett_api_key
JACKETT_ENABLED = settings.jackett_enabled
JACKETT_INDEXERS = settings.jackett_indexers
JACKETT_MAX_RESULTS = settings.jackett_max_results
JACKETT_FETCH_LIMIT = settings.jackett_fetch_limit

KP_URL_FILTER = filters.Regex(KP_URL_RE)
SEARCH_QUERY, SEARCH_OPTIONS, SEARCH_ADVANCED, SEARCH_RESULTS, SEARCH_SEASON_SELECT, SEARCH_JACKETT_SELECT = range(6)
BOT_COMMANDS = [
    BotCommand("status", "Список загрузок"),
    BotCommand("help", "Справка по боту"),
    BotCommand("id", "Показать мой chat_id"),
    BotCommand("ping", "Проверка связи"),
]
DOWNLOAD_PANEL_MESSAGES: dict[int, int] = {}
DOWNLOAD_PANEL_PAGES: dict[int, int] = {}
DOWNLOAD_PANEL_SCOPES: dict[int, str] = {}
# chat_id → имя пользователя (заполняется при запросе доступа)
ACCESS_PENDING_USERS: dict[int, str] = {}
BACKGROUND_MONITOR_TASK: asyncio.Task | None = None
PROGRESS_UPDATE_TASK: asyncio.Task | None = None
SUBSCRIPTION_MONITOR_TASK: asyncio.Task | None = None
PROGRESS_UPDATE_INTERVAL_SECONDS = 30
# (chat_id, message_id) → running refresh task for that task card
TASK_CARD_REFRESH_TASKS: dict[tuple[int, int], asyncio.Task] = {}
# Unix timestamp of next scheduled subscription check (set by the loop)
_next_subscription_check_at: float | None = None


@dataclass
class TrackerApplyResult:
    added_count: int = 0
    available_count: int = 0
    cache_time: float | None = None
    skipped_reason: str = ""


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
    return TRACKERS_MODE not in {"0", "false", "no", "off", "disabled"} and TRACKERS_MAX > 0


def _tracker_background_enabled() -> bool:
    return _public_trackers_enabled() and TRACKERS_BACKGROUND_ENABLED


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


def _tracker_key(tracker: str) -> str:
    return tracker.strip().rstrip("/").lower()


def _parse_trackers_text(text: str) -> list[str]:
    trackers = []
    seen = set()

    for raw_line in text.splitlines():
        tracker = raw_line.strip().lstrip("\ufeff")
        if not tracker or tracker.startswith("#"):
            continue
        if not tracker.lower().startswith(("udp://", "http://", "https://")):
            continue

        key = _tracker_key(tracker)
        if key in seen:
            continue

        seen.add(key)
        trackers.append(tracker)

    return trackers


def _read_trackers_cache(require_fresh: bool = True) -> tuple[list[str], float | None]:
    try:
        cache_time = TRACKERS_CACHE_FILE.stat().st_mtime
    except OSError:
        return [], None

    max_age_seconds = TRACKERS_CACHE_TTL_HOURS * 3600
    if require_fresh and time.time() - cache_time > max_age_seconds:
        return [], cache_time

    try:
        text = TRACKERS_CACHE_FILE.read_text(encoding="utf-8")
    except OSError:
        return [], cache_time

    return _parse_trackers_text(text)[:TRACKERS_MAX], cache_time


def _write_trackers_cache(text: str) -> None:
    TRACKERS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACKERS_CACHE_FILE.write_text(text, encoding="utf-8")


def _load_public_trackers() -> tuple[list[str], float | None]:
    if not _public_trackers_enabled():
        return [], None

    cached_trackers, cache_time = _read_trackers_cache(require_fresh=True)
    if cached_trackers:
        return cached_trackers, cache_time

    try:
        request = urllib.request.Request(TRACKERS_URL, headers={"User-Agent": "tg-torrent-drop/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode("utf-8")
        trackers = _parse_trackers_text(text)[:TRACKERS_MAX]
        if trackers:
            _write_trackers_cache("\n".join(trackers) + "\n")
            return trackers, TRACKERS_CACHE_FILE.stat().st_mtime
    except Exception:
        logger.warning("Failed to update public trackers list", exc_info=True)

    stale_trackers, stale_time = _read_trackers_cache(require_fresh=False)
    return stale_trackers, stale_time


def _format_tracker_cache_time(cache_time: float | None) -> str:
    if cache_time is None:
        return ""

    return datetime.fromtimestamp(cache_time, DISPLAY_TIMEZONE).strftime("%d.%m.%Y %H:%M")


def _tracker_result_lines(result: TrackerApplyResult | None) -> list[str]:
    if not result or not _public_trackers_enabled():
        return []

    if result.added_count:
        lines = [f"Public-трекеры: добавлено {result.added_count}"]
    elif result.skipped_reason:
        lines = [f"Public-трекеры: {result.skipped_reason}"]
    elif result.available_count:
        lines = ["Public-трекеры: новых нет"]
    else:
        lines = ["Public-трекеры: список недоступен"]

    cache_time = _format_tracker_cache_time(result.cache_time)
    if cache_time:
        lines.append(f"Список трекеров: {cache_time}")

    return lines


def _add_public_trackers_to_download_task(task_id: str) -> TrackerApplyResult:
    result = TrackerApplyResult()
    trackers, cache_time = _load_public_trackers()
    result.available_count = len(trackers)
    result.cache_time = cache_time

    if not _public_trackers_enabled():
        return result
    if not task_id:
        result.skipped_reason = "ID задачи пока не найден"
        return result
    if not trackers:
        result.skipped_reason = "список недоступен"
        return result

    try:
        existing = {_tracker_key(tracker) for tracker in ds_client.list_task_trackers(task_id)}
        additions = [tracker for tracker in trackers if _tracker_key(tracker) not in existing]
        if not additions:
            return result

        ds_client.add_task_trackers(task_id, additions)

        confirmed = set()
        for attempt in range(3):
            if attempt:
                time.sleep(1)

            confirmed = {_tracker_key(tracker) for tracker in ds_client.list_task_trackers(task_id)}
            if any(_tracker_key(tracker) in confirmed for tracker in additions):
                break

        result.added_count = sum(1 for tracker in additions if _tracker_key(tracker) in confirmed)
        if not result.added_count:
            result.skipped_reason = "добавление не подтвердилось"
    except DownloadStationError:
        logger.warning("Failed to add public trackers to task %s", task_id, exc_info=True)
        result.skipped_reason = "Download Station API не принял список"

    return result


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
    task_id = task.get("id")
    if not task_id or task_id in processed_ids:
        return False

    if (task.get("type") or "").lower() != "bt":
        return False

    status = (task.get("status") or "").lower()
    return status != "finished"


def _tracker_attempt_is_final(result: TrackerApplyResult) -> bool:
    if result.added_count:
        return True
    if result.available_count and not result.skipped_reason:
        return True
    if result.skipped_reason.startswith("приватный torrent"):
        return True
    return False


def _mark_tracker_processed_if_final(task_id: str, result: TrackerApplyResult) -> None:
    if not task_id or not _tracker_attempt_is_final(result):
        return

    _add_tracker_processed_ids({task_id})


def _tracker_button_visible(task_id: str, status: str, task_type: str) -> bool:
    if not task_id or not _tracker_background_enabled():
        return False
    if (task_type or "").lower() != "bt":
        return False
    if (status or "").lower() == "finished":
        return False

    return task_id not in _load_tracker_processed_ids()


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
        logger.info(
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


async def _background_monitor_loop(app: Application) -> None:
    if not _background_monitor_enabled():
        logger.info("Background monitor disabled")
        return

    logger.info(
        "Background monitor enabled, interval=%ss",
        TRACKERS_BACKGROUND_INTERVAL_SECONDS,
    )

    try:
        await asyncio.sleep(10)
        while True:
            await _run_tracker_background_once()
            await _run_task_notifications_once(app)
            await _run_auto_delete_finished_once()
            await _run_prune_stale_state_once()
            await asyncio.sleep(TRACKERS_BACKGROUND_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Background monitor stopped")
        raise


def _format_updated_at() -> str:
    return datetime.now(DISPLAY_TIMEZONE).strftime("%H:%M:%S")


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


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    return _view_find_task(tasks, task_id)


def _make_task_keyboard(task_id: str, status: str = "", task_type: str = "") -> InlineKeyboardMarkup:
    """Bot-level wrapper: injects tracker-button visibility state into the stateless _task_keyboard."""
    return _task_keyboard(
        task_id, status, task_type,
        show_trackers=_tracker_button_visible(task_id, status, task_type),
    )


def _notification_keyboard(task_id: str, status: str = "", task_type: str = "") -> InlineKeyboardMarkup:
    if (status or "").lower() in {"finished", "seeding"}:
        return _final_notification_keyboard(task_id, show_plex=PLEX_ENABLED)

    return _make_task_keyboard(task_id, status, task_type)


def _format_task_card(task: dict) -> str:
    return _view_format_task_card(task)


def _load_task_owners() -> dict[str, int]:
    return state_store.load_task_owners()


def _save_task_owners(owners: dict[str, int]) -> None:
    state_store.save_task_owners(owners)


def _remember_task_owner(task_id: str, chat_id: int | None) -> None:
    state_store.remember_task_owner(task_id, chat_id)


def _load_notified_tasks() -> dict[str, str]:
    return state_store.load_notified_tasks()


def _save_notified_tasks(tasks: dict[str, str]) -> None:
    state_store.save_notified_tasks(tasks)


def _load_auto_delete_tasks() -> dict[str, float]:
    return state_store.load_auto_delete_tasks()


def _save_auto_delete_tasks(tasks: dict[str, float]) -> None:
    state_store.save_auto_delete_tasks(tasks)


def _forget_task_state(task_ids: list[str]) -> None:
    state_store.forget_task_state(task_ids)


def _explicit_notification_chat_ids() -> set[int]:
    return parse_chat_ids(NOTIFY_CHAT_IDS_RAW)


def _notification_recipients(task_id: str) -> set[int]:
    return _policy_notification_recipients(
        task_id,
        explicit_chat_ids=_explicit_notification_chat_ids(),
        task_owners=_load_task_owners(),
        notify_external_tasks=TASK_NOTIFY_EXTERNAL_TASKS,
        fallback_chat_ids=_all_allowed_chat_ids(),
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

        notification_key = _notification_status_key(status)
        if notified.get(task_id) == notification_key:
            continue

        recipients = _notification_recipients(task_id)
        if not recipients:
            continue

        sent = False
        for chat_id in recipients:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=_format_task_notification(task),
                    reply_markup=_notification_keyboard(task_id, status, task.get("type", "")),
                )
                sent = True
            except Exception:
                logger.warning(
                    "Failed to send task status notification chat_id=%s task_id=%s",
                    chat_id,
                    task_id,
                    exc_info=True,
                )

        if sent:
            notified[task_id] = notification_key
            changed = True

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


def _cancel_task_card_refresh(chat_id: int, message_id: int) -> None:
    """Cancel the auto-refresh loop for a specific task-card message."""
    key = (chat_id, message_id)
    task = TASK_CARD_REFRESH_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()


def _start_task_card_refresh(app, chat_id: int, message_id: int, task_id: str) -> None:
    """Start (or restart) the 30-second auto-refresh loop for a task card."""
    _cancel_task_card_refresh(chat_id, message_id)
    key = (chat_id, message_id)
    TASK_CARD_REFRESH_TASKS[key] = app.create_task(
        _task_card_refresh_loop(app, chat_id, message_id, task_id)
    )


async def _task_card_refresh_loop(app, chat_id: int, message_id: int, task_id: str) -> None:
    """Refresh the task card every 30 s while the task is actively downloading."""
    try:
        while True:
            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL_SECONDS)
            try:
                tasks = await asyncio.to_thread(ds_client.list_tasks)
            except DownloadStationError:
                continue

            task = _find_task(tasks, task_id)
            if not task:
                return  # task deleted from DS

            status = (task.get("status") or "").lower()
            if status not in _ACTIVE_STATUSES:
                return  # no longer actively transferring — stop refreshing

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
                    return  # user navigated away
                # "message is not modified" is fine — just continue
            except Exception:
                logger.debug("Task card auto-refresh edit error", exc_info=True)
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

    if not _has_active_tasks(tasks):
        return

    for chat_id, message_id in list(DOWNLOAD_PANEL_MESSAGES.items()):
        scope = _normalize_list_scope(DOWNLOAD_PANEL_SCOPES.get(chat_id), chat_id)
        page = DOWNLOAD_PANEL_PAGES.get(chat_id, 0)
        visible_tasks = _filter_tasks_for_scope(tasks, chat_id, scope)
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
            else:
                logger.warning("Failed to update progress panel chat_id=%s: %s", chat_id, e)


async def _progress_update_loop(app) -> None:
    try:
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL_SECONDS)
        while True:
            await _run_progress_panel_update_once(app)
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


async def _safe_edit_message(message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if not _is_message_not_modified(e):
            raise


async def _safe_edit_callback(query, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
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


def _friendly_error(service: str, raw: str) -> str:
    """Return an HTML-formatted user-friendly error string.

    Shows a brief human-readable summary as the visible line and hides raw
    technical details inside a Telegram ``<blockquote expandable>`` block
    (supported since Bot API 7.4 / Telegram 10.6).
    ``service`` is ``"rutracker"`` or ``"jackett"``.
    """
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
    return f"{head}\n<blockquote expandable>{html_module.escape(raw)}</blockquote>"


async def search_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: диагностика соединения с Rutracker и Jackett."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_admin_chat(chat_id):
        return

    msg = await update.message.reply_text("🔍 Проверяю соединение…")
    lines: list[str] = []

    if rutracker_client:
        try:
            status = await asyncio.to_thread(rutracker_client.diagnose)
        except Exception as e:
            lines.append(_friendly_error("rutracker", str(e)))
            status = None

        if status is not None:
            if status["login_ok"]:
                lines.append("✅ <b>Rutracker</b>: подключен")
            else:
                lines.append(_friendly_error("rutracker", status["error"]))
    else:
        lines.append("⛔ <b>Rutracker</b>: не настроен — задайте RUTRACKER_USERNAME в .env")

    if jackett_client:
        diag = await asyncio.to_thread(jackett_client.test_connection)
        if diag["api_ok"]:
            indexer_names = [i["name"] if isinstance(i, dict) else i for i in diag["indexers"]]
            indexer_list = ", ".join(indexer_names[:10]) or "нет"
            if len(indexer_names) > 10:
                indexer_list += f" (+{len(indexer_names) - 10})"
            lines.append(f"\n🌐 <b>Jackett</b>: ✅ подключен")
            lines.append(f"   Индексеры: {html_module.escape(indexer_list)}")
        else:
            error = diag.get("error", "Неизвестная ошибка")
            lines.append("\n" + _friendly_error("jackett", error))
    else:
        lines.append("\n🌐 <b>Jackett</b>: не настроен")

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


async def kp_link_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ConversationHandler entry point: user sent a Kinopoisk URL.

    Fetches film/series info, stores the search base in user_data, then shows
    the quality-options keyboard so the user can kick off a Rutracker search.
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
        reply_markup=_search_options_keyboard(),
    )
    return SEARCH_OPTIONS


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return ConversationHandler.END

    if rutracker_client is None and jackett_client is None:
        await update.message.reply_text(
            "⛔ Поиск недоступен: не настроен ни Rutracker, ни Jackett.\n"
            "Добавьте учётные данные в .env и перезапустите бот."
        )
        return ConversationHandler.END

    await _delete_message_safely(
        context, update.effective_chat.id, update.message.message_id, "search command"
    )
    await update.message.reply_text(
        "🔍 Введите название для поиска:\n(или /cancel для отмены)"
    )
    return SEARCH_QUERY


async def search_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query_text = (update.message.text or "").strip()
    if not query_text:
        await update.message.reply_text("Введите текст для поиска или /cancel для отмены.")
        return SEARCH_QUERY

    # Normalise 'Сезон N' → 'Сезон: N' to match Rutracker title format
    query_text = _normalize_season_in_query(query_text)
    context.user_data["srch_query"] = query_text
    msg = await update.message.reply_text(
        f"Запрос: «{query_text}»",
        reply_markup=_search_options_keyboard(),
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
) -> int:
    """Fetch Jackett indexers and show the tracker-selection keyboard.

    Returns SEARCH_JACKETT_SELECT on success, ConversationHandler.END on failure.
    header is optional text shown above the keyboard prompt.
    """
    if jackett_client is None:
        await edit_fn("Jackett не настроен.")
        return ConversationHandler.END

    try:
        indexers = await asyncio.to_thread(jackett_client.get_indexers)
    except JackettError as e:
        logger.error("Jackett get_indexers failed: %s", e)
        await edit_fn(_friendly_error("jackett", str(e)), parse_mode="HTML")
        return ConversationHandler.END

    if not indexers:
        logger.warning("Jackett: no indexers configured")
        await edit_fn("🌐 <b>Jackett</b>: нет настроенных индексеров", parse_mode="HTML")
        return ConversationHandler.END

    # Default: select only Rutracker; fallback to all if not found
    rutracker_ids = {i["id"] for i in indexers if "rutracker" in i["id"].lower()}
    selected = rutracker_ids if rutracker_ids else {i["id"] for i in indexers}

    context.user_data["srch_jackett_indexers"] = indexers
    context.user_data["srch_jackett_selected"] = selected

    prompt = (header + "\n" if header else "") + "Выберите трекеры для поиска:"
    try:
        await edit_fn(prompt, reply_markup=_jackett_select_keyboard(indexers, selected), parse_mode="HTML")
    except Exception as exc:
        logger.error("Jackett selector edit failed: %s", exc, exc_info=True)
        raise
    return SEARCH_JACKETT_SELECT


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
    results = []
    banner = ""
    source = "rutracker"

    if rutracker_client:
        try:
            results = await asyncio.to_thread(rutracker_client.search, search_query)
            source = "rutracker"
        except RutrackerError as rt_err:
            if jackett_client:
                header = _friendly_error("rutracker", str(rt_err))
                return await _show_jackett_selector(edit_fn, context, header=header)
            else:
                await edit_fn(_friendly_error("rutracker", str(rt_err)), parse_mode="HTML")
                return ConversationHandler.END
    elif jackett_client:
        return await _show_jackett_selector(edit_fn, context)
    else:
        await edit_fn("Поиск недоступен: не настроен ни Rutracker, ни Jackett.")
        return ConversationHandler.END

    if not results:
        await edit_fn(
            f"По запросу «{search_query}» ничего не найдено.\n"
            "Попробуйте другой запрос."
        )
        return ConversationHandler.END

    results_data = []
    if source == "rutracker":
        for r in results:
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
    else:  # jackett
        for r in results:
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

    # --- Step 1: season filter (only for Rutracker results where titles match) ---
    season_num = _extract_season_from_query(search_query)
    if season_num is not None:
        filtered = _filter_by_season(results_data, season_num)
        if filtered:
            results_data = filtered
        else:
            # Season filter wiped everything — check if quality caused it.
            base_query = context.user_data.get("srch_query", "").strip()
            has_quality = bool(base_query) and base_query.lower() != search_query.lower()
            if has_quality:
                await edit_fn(
                    f"По запросу «{search_query}» раздач с указанным качеством не найдено.\n"
                    f"Попробовать без фильтра качества?",
                    reply_markup=_no_quality_keyboard(base_query),
                )
                return SEARCH_RESULTS
            # No quality to drop — just report nothing found.
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

    show_jackett_expand = bool(jackett_client and source == "rutracker")
    show_jackett_direct = bool(jackett_client)

    await edit_fn(
        _build_results_text(results_data, search_query, 0, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=0,
            show_jackett_expand=show_jackett_expand,
            show_jackett_direct=show_jackett_direct,
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
    show_jackett_expand = bool(jackett_client and context.user_data.get("srch_source") == "rutracker")
    show_jackett_direct = bool(jackett_client)

    await query.edit_message_text(
        _build_results_text(results_data, search_query, page, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=page,
            show_jackett_expand=show_jackett_expand,
            show_jackett_direct=show_jackett_direct,
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
    """Open Jackett tracker selection for expand-mode (merge with existing results)."""
    query = update.callback_query
    await query.answer()

    if jackett_client is None:
        await query.answer("Jackett не настроен.", show_alert=True)
        return SEARCH_RESULTS

    context.user_data["srch_jackett_mode"] = "expand"
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    return await _show_jackett_selector(
        query.edit_message_text,
        context,
        header=f"Поиск: «{search_query}»",
    )


async def search_jackett_start_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open Jackett tracker selection for direct-mode (Jackett-only results, no merge)."""
    query = update.callback_query
    await query.answer()

    if jackett_client is None:
        await query.answer("Jackett не настроен.", show_alert=True)
        return SEARCH_RESULTS

    context.user_data["srch_jackett_mode"] = "direct"
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    return await _show_jackett_selector(
        query.edit_message_text,
        context,
        header=f"Поиск: «{search_query}»",
    )


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
    await query.edit_message_text(
        f"Поиск: «{search_query}»\nВыберите трекеры для поиска:",
        reply_markup=_jackett_select_keyboard(indexers, selected),
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
    """Run Jackett search with selected indexers.

    Behaviour depends on srch_jackett_mode stored in user_data:
    - "expand"  (default) — merge Jackett results with existing Rutracker results.
    - "direct"            — show only Jackett results, replacing the previous list.

    IMPORTANT: query.answer() is called exactly once per execution path.  Never
    call it at the top and then again in an error branch — Telegram rejects duplicate
    answerCallbackQuery calls, which would abort the recovery edit_message_text.
    """
    query = update.callback_query

    if jackett_client is None:
        await query.answer()
        await query.edit_message_text("Jackett не настроен.")
        return ConversationHandler.END

    selected: set[str] = context.user_data.get("srch_jackett_selected", set())
    if not selected:
        # Alert shown without prior answer — this is the only answer for this path.
        await query.answer("Выберите хотя бы один трекер.", show_alert=True)
        indexers = context.user_data.get("srch_jackett_indexers", [])
        search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
        await query.edit_message_text(
            f"Поиск: «{search_query}»\nВыберите трекеры для поиска:",
            reply_markup=_jackett_select_keyboard(indexers, selected),
        )
        return SEARCH_JACKETT_SELECT

    # Normal search path: answer now (dismisses the button spinner) then show loading.
    await query.answer()

    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    mode = context.user_data.get("srch_jackett_mode", "expand")

    try:
        await query.edit_message_text(f"🔍 Ищу «{search_query}» через Jackett…")
    except Exception as exc:
        logger.warning("search_jackett_do: loading edit failed (msg=%s): %s",
                       query.message.message_id, exc)
        # Continue anyway — we'll overwrite whatever is there with results or error.

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
        # Restore previous results (or show error) in the message.
        # NOTE: query is already answered above — use _safe_answer for the alert.
        if isinstance(e, asyncio.TimeoutError):
            raw_err = "Jackett не ответил за 45 сек — проверьте Global timeout в настройках Jackett"
        else:
            raw_err = str(e)
            # Hint about common XML parse failure (bad credentials / indexer not logged in)
            if "not well-formed" in raw_err or "разобрать ответ" in raw_err:
                raw_err += " — возможно, индексер требует авторизации в Jackett"
        logger.error("Jackett search failed: %s", raw_err)
        await _safe_answer(query, f"❌ {raw_err}", show_alert=True)
        existing = context.user_data.get("srch_results", [])
        banner = context.user_data.get("srch_banner", "")
        show_expand = bool(context.user_data.get("srch_source") == "rutracker")
        if existing:
            await query.edit_message_text(
                _build_results_text(existing, search_query, 0, banner=banner),
                reply_markup=_search_results_keyboard(
                    existing, page=0,
                    show_jackett_expand=show_expand,
                    show_jackett_direct=bool(jackett_client),
                ),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return SEARCH_RESULTS
        await query.edit_message_text(
            _friendly_error("jackett", raw_err),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if not j_results_raw:
        await _safe_answer(query, "Jackett не нашёл результатов по выбранным трекерам.", show_alert=True)
        existing = context.user_data.get("srch_results", [])
        banner = context.user_data.get("srch_banner", "")
        show_expand = bool(context.user_data.get("srch_source") == "rutracker")
        await query.edit_message_text(
            _build_results_text(existing, search_query, 0, banner=banner) if existing else f"По запросу «{search_query}» ничего не найдено.",
            reply_markup=_search_results_keyboard(
                existing, page=0,
                show_jackett_expand=show_expand,
                show_jackett_direct=bool(jackett_client),
            ) if existing else None,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return SEARCH_RESULTS if existing else ConversationHandler.END

    logger.info("Jackett search returned %d results", len(j_results_raw))

    # Build Jackett result dicts
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

    mode = context.user_data.get("srch_jackett_mode", "expand")

    if mode == "direct":
        # Jackett-only: show all fetched results sorted by score, no merge.
        merged = sorted(j_results_data, key=_score_result, reverse=True)
        banner = f"🔍 Jackett: {len(merged)} результатов"
        source = "jackett"
    else:
        # Expand: merge Jackett results with existing Rutracker results.
        existing = context.user_data.get("srch_results", [])
        existing_titles = {r["title"].lower() for r in existing}
        new_jackett = [r for r in j_results_data if r["title"].lower() not in existing_titles]
        merged_raw = existing + new_jackett

        # Cap Jackett share before merging to avoid flooding Rutracker results
        jk_capped = sorted(
            [r for r in merged_raw if r.get("source") == "jackett"],
            key=_score_result, reverse=True,
        )[:JACKETT_MAX_RESULTS]
        ru_only = [r for r in merged_raw if r.get("source") != "jackett"]

        # Sort ALL results together so best Jackett razdachas surface on page 0
        merged = sorted(ru_only + jk_capped, key=_score_result, reverse=True)

        new_count = len(new_jackett)
        banner = (
            f"➕ Jackett добавил {new_count} результатов"
            if new_count > 0 else
            "ℹ️ Jackett не нашёл новых результатов (уже в списке)"
        )
        source = "mixed"

    if merged:
        merged[0]["recommended"] = True

    context.user_data["srch_results"] = merged
    context.user_data["srch_results_page"] = 0
    context.user_data["srch_source"] = source
    context.user_data["srch_banner"] = banner

    try:
        await query.edit_message_text(
            _build_results_text(merged, search_query, 0, banner=banner),
            reply_markup=_search_results_keyboard(
                merged, page=0,
                show_jackett_expand=False,   # already used Jackett — no need to offer expand again
                show_jackett_direct=bool(jackett_client),
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        logger.error(
            "Jackett results display failed: %s",
            exc,
            exc_info=True,
        )
        # query is already answered — use _safe_answer to avoid duplicate-answer error.
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
        reply_markup=_search_advanced_keyboard(settings),
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
        reply_markup=_search_advanced_keyboard(settings),
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

        season_count_label = f" ({total_seasons} сез.)" if total_seasons else ""
        await query.edit_message_text(
            f"📺 Сериал: «{series_query}»{season_count_label}\nВыберите сезон:",
            reply_markup=_season_select_keyboard(total_seasons),
        )
        return SEARCH_SEASON_SELECT

    # No KinoPoisk — go straight to search.
    return await _execute_search(query, context, series_query)


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

    search_query = _normalize_season_in_query(f"{base} Сезон {season_num}")
    return await _execute_search(query, context, search_query)


async def search_season_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose to search the whole series without a season filter."""
    query = update.callback_query
    await query.answer()

    base = context.user_data.get("srch_base_title", "")
    if not base:
        await query.edit_message_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    return await _execute_search(query, context, base)


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
    search_query = _normalize_season_in_query(f"{base} Сезон {season_num}")
    return await _run_search(update.message.reply_text, context, search_query)


async def _download_and_add(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    index: int,
    *,
    subscribe: bool = False,
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

    await query.edit_message_text("⏳ Скачиваю torrent-файл…")

    title = result["title"]
    safe_name = _safe_filename(f"{title}.torrent")
    temp_path = _temp_path(safe_name)
    task_id = ""
    tracker_result: TrackerApplyResult | None = None

    chat_id = query.message.chat.id if query.message else None

    try:
        if result.get("magnet_url"):
            # Jackett result with magnet — submit directly to DS
            task_id = await asyncio.to_thread(ds_client.create_magnet, result["magnet_url"])
        elif source == "rutracker" and topic_id and rutracker_client:
            torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
            temp_path.write_bytes(torrent_bytes)
            task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
        elif result.get("torrent_url") and jackett_client:
            torrent_bytes = await asyncio.to_thread(jackett_client.download_torrent, result["torrent_url"])
            temp_path.write_bytes(torrent_bytes)
            task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
        else:
            await query.edit_message_text("Не удалось скачать торрент: нет доступного источника.")
            return ConversationHandler.END

        _remember_task_owner(task_id, chat_id)

        if temp_path.exists():
            if _torrent_file_is_private(temp_path):
                tracker_result = TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")
                _mark_tracker_processed_if_final(task_id, tracker_result)
            else:
                tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
                _mark_tracker_processed_if_final(task_id, tracker_result)
        else:
            # magnet path — no torrent file to check
            tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
            _mark_tracker_processed_if_final(task_id, tracker_result)

        if subscribe:
            if source == "jackett":
                # Query-based subscription for Jackett results
                sub_key = f"jackett:{uuid.uuid4().hex[:8]}"
                subs = state_store.load_topic_subscriptions()
                subs[sub_key] = {
                    "type": "jackett",
                    "chat_id": chat_id,
                    "query": context.user_data.get("srch_search_query", context.user_data.get("srch_query", title)),
                    "seen_titles": [r["title"] for r in context.user_data.get("srch_results", [])],
                    "added_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                    "last_check": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                }
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
            "torrent-файл", title=title, task_id=task_id, tracker_result=tracker_result
        )
        suffix = "\n\n🔔 Буду следить за новыми сериями." if subscribe else ""
        success_text = f"{added_msg}{suffix}"

        series_query = _extract_series_base_query(title)
        if series_query:
            context.user_data["srch_series_query"] = series_query
            await query.edit_message_text(
                success_text, reply_markup=_search_after_add_keyboard(task_id)
            )
            return SEARCH_RESULTS

        await query.edit_message_text(success_text, reply_markup=_task_reply_markup(task_id))
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


async def search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    has_photo = context.user_data.pop("srch_confirm_has_photo", False)
    photo_msg_id = context.user_data.pop("srch_confirm_message_id", None)
    photo_chat_id = context.user_data.pop("srch_confirm_chat_id", None)

    if update.callback_query:
        await update.callback_query.answer()
        if has_photo:
            # The current message IS the photo — delete it and send plain text reply
            try:
                await update.callback_query.message.delete()
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    chat_id=update.callback_query.message.chat_id,
                    text="Поиск отменен.",
                )
            except Exception:
                pass
        else:
            await update.callback_query.edit_message_text("Поиск отменен.")
    elif update.message:
        await update.message.reply_text("Поиск отменен.")
        # If a photo confirm card is still open in the chat, delete it
        if has_photo and photo_msg_id and photo_chat_id:
            try:
                await context.bot.delete_message(chat_id=photo_chat_id, message_id=photo_msg_id)
            except Exception:
                pass

    for key in (
        "srch_query", "srch_search_query", "srch_settings", "srch_results",
        "srch_picked", "srch_kp_info", "srch_results_page",
        "srch_base_title", "srch_total_seasons", "srch_series_query",
        "srch_ui_msg_id", "srch_ui_chat_id", "srch_banner",
        "srch_jackett_indexers", "srch_jackett_selected", "srch_source",
    ):
        context.user_data.pop(key, None)

    return ConversationHandler.END


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

    for key in (
        "srch_query", "srch_search_query", "srch_settings", "srch_results",
        "srch_picked", "srch_kp_info", "srch_results_page",
        "srch_base_title", "srch_total_seasons", "srch_series_query",
        "srch_ui_msg_id", "srch_ui_chat_id", "srch_banner",
        "srch_jackett_indexers", "srch_jackett_selected", "srch_source",
    ):
        context.user_data.pop(key, None)

    return ConversationHandler.END


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

    if not my_subs and not jackett_subs_all:
        await update.message.reply_text("📭 Активных подписок нет.")
        return

    total_count = len(my_subs) + len(jackett_subs_all)
    lines = [f"🔔 Активные подписки ({total_count}):"]
    rows = []

    for i, (topic_id, sub) in enumerate(my_subs.items(), 1):
        short = _format_sub_title(sub.get("title", ""))
        ep_end = sub.get("last_episode_end", "?")
        total = sub.get("total_episodes", "?")
        lines.append(f"\n{i}. {_html.escape(short)}\n   📺 {ep_end} из {total} эп.")
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
    if len(parts) < 3:
        return

    action = parts[1]
    topic_id = parts[2] if len(parts) > 2 else ""

    if action == "unsub":
        subs = state_store.load_topic_subscriptions()
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
        sub = subs.pop(key, None)
        state_store.save_topic_subscriptions(subs)
        if sub:
            await query.edit_message_text(f"🔕 Подписка отменена:\n{sub.get('query', key)}")
        else:
            await query.edit_message_text("Подписка не найдена.")


async def _check_jackett_subscriptions(app: Application) -> None:
    """Check all Jackett query-based subscriptions for new results."""
    if jackett_client is None:
        return

    subs = state_store.load_topic_subscriptions()
    jackett_subs = {k: v for k, v in subs.items() if v.get("type") == "jackett"}
    if not jackett_subs:
        return

    logger.info("Checking %d Jackett subscription(s)", len(jackett_subs))
    changed = False

    for key, sub in list(jackett_subs.items()):
        try:
            search_query = sub.get("query", "")
            if not search_query:
                continue

            new_results = await asyncio.to_thread(jackett_client.search, search_query)
            new_titles = {r.title for r in new_results}
            seen_titles = set(sub.get("seen_titles", []))

            fresh = [r for r in new_results if r.title not in seen_titles]
            if not fresh:
                sub["last_check"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                changed = True
                continue

            chat_id = sub.get("chat_id")
            short_q = search_query[:40] + "…" if len(search_query) > 40 else search_query

            text = f"🔔 Новые результаты по запросу «{short_q}»:\n"
            for r in fresh[:5]:
                text += f"\n🔎 {r.title}\n   📦 {r.size} | 🌱 {r.seeders} | 📡 {r.tracker}"

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

            sub["seen_titles"] = list(seen_titles | new_titles)
            sub["last_check"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
            changed = True
            logger.info("Jackett subscription update: key=%s fresh=%d", key, len(fresh))

            if chat_id:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
                except Exception:
                    logger.warning("Failed to notify chat %s for Jackett subscription", chat_id, exc_info=True)

        except Exception:
            logger.warning("Error checking Jackett subscription %s", key, exc_info=True)

    if changed:
        state_store.save_topic_subscriptions(subs)


async def _check_subscriptions(app: Application) -> None:
    if not rutracker_client:
        await _check_jackett_subscriptions(app)
        return

    subs = state_store.load_topic_subscriptions()
    if not subs:
        await _check_jackett_subscriptions(app)
        return

    logger.info("Checking %d topic subscription(s)", len(subs))
    changed = False

    for topic_id, sub in list(subs.items()):
        if sub.get("type") == "jackett":
            continue
        try:
            new_title = await asyncio.to_thread(rutracker_client.get_topic_title, topic_id)
            new_info = _parse_episode_info(new_title)
            if new_info is None:
                continue

            new_end, new_total = new_info
            last_end = sub.get("last_episode_end", 0)

            if new_end <= last_end:
                continue

            chat_id = sub.get("chat_id")
            is_complete = new_end >= new_total
            short = _format_sub_title(new_title)

            safe_name = _safe_filename(f"rutracker_{topic_id}.torrent")
            temp_path = _temp_path(safe_name)
            task_id = ""
            try:
                torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
                temp_path.write_bytes(torrent_bytes)
                task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
                if chat_id:
                    _remember_task_owner(task_id, chat_id)
            except (RutrackerError, DownloadStationError) as e:
                logger.warning("Failed to update subscription %s: %s", topic_id, e)
                continue
            finally:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

            if is_complete:
                text = (
                    f"🔔 {short}: сезон завершён!\n"
                    f"Эпизодов: {last_end} → {new_end} из {new_total} ✅\n"
                    "Торрент обновлён в Download Station.\n"
                    "Подписка снята автоматически."
                )
                del subs[topic_id]
                kb = _download_list_keyboard()
            else:
                text = (
                    f"🔔 {short}: новая серия!\n"
                    f"Эпизодов: {last_end} → {new_end} из {new_total}\n"
                    "Торрент обновлён в Download Station."
                )
                sub["last_episode_end"] = new_end
                sub["title"] = new_title
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 К загрузкам", callback_data=_task_callback("list", task_id)),
                    InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:unsub:{topic_id}"),
                ]])

            changed = True
            logger.info("Subscription update: topic=%s episodes=%s→%s/%s", topic_id, last_end, new_end, new_total)

            if chat_id:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
                except Exception:
                    logger.warning("Failed to notify chat %s for subscription update", chat_id, exc_info=True)

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
        await _check_subscriptions(app)
    except Exception:
        logger.error("Initial subscription check error", exc_info=True)
    while True:
        _next_subscription_check_at = time.time() + interval
        await asyncio.sleep(interval)
        _next_subscription_check_at = None
        try:
            await _check_subscriptions(app)
        except Exception:
            logger.error("Subscription check loop error", exc_info=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /start from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    if RUTRACKER_ENABLED:
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

    if RUTRACKER_ENABLED:
        kp_hint = " или вставьте ссылку с Кинопоиска" if KINOPOISK_ENABLED else ""
        search_lines = (
            f"- напишите название фильма/сериала{kp_hint} — сразу откроется поиск по Rutracker;\n"
            "- /search тоже работает как альтернативная точка входа;\n"
        )
    else:
        search_lines = ""
    await update.message.reply_text(
        "Что умею:\n"
        "- принимаю .torrent файлы;\n"
        "- принимаю magnet-ссылки;\n"
        "- добавляю задачи в Download Station;\n"
        "- добавляю public-трекеры к новым задачам, если это включено;\n"
        "- присылаю уведомление с кнопками, когда загрузка завершилась или остановилась с ошибкой;\n"
        "- показываю ваши загрузки через /status;\n"
        "- администратору показываю переключатель между своими и всеми загрузками;\n"
        "- даю кнопки управления задачами: обновить статус, пауза, запуск, удаление;\n"
        "- могу удалить завершенные задачи из списка вручную или автоматически;\n"
        f"{search_lines}"
        "\n/id показывает chat_id. Новые пользователи могут написать /start, а админ разрешит доступ кнопкой."
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

    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError as e:
        logger.exception("Failed to list Download Station tasks")
        await context.bot.send_message(chat_id=chat.id, text=f"Не удалось получить задачи: {e}")
        return

    scope = _default_list_scope(chat.id)
    visible_tasks = _filter_tasks_for_scope(tasks, chat.id, scope)
    total_count = len(tasks) if _is_admin_chat(chat.id) else None
    await _send_download_panel(context, chat.id, visible_tasks, scope, total_count=total_count)


def _format_users_panel() -> tuple[str, InlineKeyboardMarkup]:
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

    rows = [
        [InlineKeyboardButton(
            f"🚫 {info.get('name', '') or uid}",
            callback_data=f"{ACCESS_CALLBACK_PREFIX}:remove:{uid}",
        )]
        for uid, info in approved_users.items()
    ]
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"{ACCESS_CALLBACK_PREFIX}:users_refresh")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_admin_chat(chat_id):
        return

    text, keyboard = _format_users_panel()
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

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError as e:
            await query.edit_message_text(f"Не удалось получить задачу: {e}")
            return

        task = _find_task(tasks, task_id)
        title = task.get("title") if task else task_id
        await query.edit_message_text(
            f"Удалить задачу из Download Station?\n\n{title}\nID: {task_id}",
            reply_markup=_delete_confirm_keyboard(task_id),
        )
        return

    if action == "delete_finished_ask":
        scope = _normalize_list_scope(task_id, chat_id)
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
        return

    if action == "trackers":
        if not _can_access_task_id(chat_id, task_id):
            await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
            return

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
        return

    if action in {"resume", "pause", "delete"}:
        if not _can_access_task_id(chat_id, task_id):
            await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
            return

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
                await query.edit_message_text(f"Задача удалена из Download Station.\nID: {task_id}")
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
        else:
            await query.edit_message_text(f"{notice}\nID: {task_id}")
        return

    if not _can_access_task_id(chat_id, task_id):
        await query.edit_message_text("Эта задача не относится к вашим загрузкам.")
        return

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

    try:
        await asyncio.to_thread(ds_client.resume_task, task_id)
    except DownloadStationError as e:
        logger.exception("Failed to resume Download Station task")
        await update.message.reply_text(f"Не удалось запустить задачу {task_id}: {e}")
        return

    await update.message.reply_text(f"Команда запуска отправлена для {task_id}.")


async def _process_magnet_uri(update: Update, context: ContextTypes.DEFAULT_TYPE, magnet_uri: str) -> None:
    """Add a magnet-link task to Download Station. Shared by handle_text and text_message_entry."""
    progress_message = await update.message.reply_text(_magnet_wait_text(0, 8))

    try:
        logger.info("Creating Download Station task from magnet chat_id=%s", _chat_id(update))
        try:
            before_tasks = await asyncio.to_thread(ds_client.list_tasks)
            known_task_ids = {task["id"] for task in before_tasks if task.get("id")}
        except DownloadStationError:
            logger.warning("Failed to fetch task list before magnet create", exc_info=True)
            known_task_ids = set()

        task_id = await asyncio.to_thread(ds_client.create_magnet, magnet_uri)
        if not task_id:
            task_id = await _wait_for_magnet_task_id(magnet_uri, known_task_ids, progress_message)
        _remember_task_owner(task_id, update.effective_chat.id if update.effective_chat else None)
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


async def text_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ConversationHandler entry point for all plain text messages.

    Routes automatically — the user never needs to type /search:
    • magnet link     → add to Download Station, end conversation
    • Kinopoisk URL   → Kinopoisk lookup + quality-options keyboard
    • anything else   → treat as a Rutracker search query
    """
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return ConversationHandler.END

    text = (update.message.text or "").strip()

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
    old_msg_id = context.user_data.pop("srch_ui_msg_id", None)
    old_chat_id = context.user_data.pop("srch_ui_chat_id", None)
    if old_msg_id and old_chat_id:
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

        logger.info("Downloading %s from chat_id=%s", original_name, _chat_id(update))
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(temp_path))

        if not _looks_like_torrent(temp_path):
            await update.message.reply_text("Файл не похож на настоящий .torrent.")
            return

        logger.info("Creating Download Station task from torrent file %s", safe_name)
        task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
        _remember_task_owner(task_id, update.effective_chat.id if update.effective_chat else None)
        if _torrent_file_is_private(temp_path):
            tracker_result = TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")
            _mark_tracker_processed_if_final(task_id, tracker_result)
        else:
            tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
            _mark_tracker_processed_if_final(task_id, tracker_result)

        await update.message.reply_text(
            _task_added_message(
                "torrent-файл",
                title=safe_name,
                task_id=task_id,
                tracker_result=tracker_result,
            ),
            reply_markup=_task_reply_markup(task_id),
        )

    except Exception as e:
        logger.exception("Failed to process torrent")

        try:
            await update.message.reply_text(
                f"Ошибка при обработке .torrent: {type(e).__name__}: {e}"
            )
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


async def setup_bot_commands(app: Application) -> None:
    global BACKGROUND_MONITOR_TASK, PROGRESS_UPDATE_TASK

    _cleanup_tmp_dir()
    commands = list(BOT_COMMANDS)
    if RUTRACKER_ENABLED:
        search_desc = (
            "Поиск по Rutracker (или вставьте ссылку Кинопоиска)"
            if KINOPOISK_ENABLED
            else "Поиск торрентов на Rutracker"
        )
        commands.append(BotCommand("search", search_desc))
        commands.append(BotCommand("subs", "Подписки на обновления серий"))
    admin_commands = commands + [
        BotCommand("users", "Управление доступом пользователей"),
        BotCommand("searchstatus", "Проверка соединения с Rutracker"),
    ]
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await app.bot.set_my_commands(admin_commands, scope={"type": "chat", "chat_id": admin_id})
        except Exception:
            pass
    await app.bot.set_my_commands(commands)
    logger.info("Telegram command menu updated")

    if _background_monitor_enabled():
        BACKGROUND_MONITOR_TASK = app.create_task(_background_monitor_loop(app))

    PROGRESS_UPDATE_TASK = app.create_task(_progress_update_loop(app))
    logger.info("Progress update loop started, interval=%ss", PROGRESS_UPDATE_INTERVAL_SECONDS)

    if rutracker_client:
        global SUBSCRIPTION_MONITOR_TASK
        SUBSCRIPTION_MONITOR_TASK = app.create_task(_subscription_check_loop(app))
        logger.info("Subscription check loop started, interval=%sh", SUBSCRIPTION_CHECK_INTERVAL_HOURS)


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
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CallbackQueryHandler(access_callback, pattern=f"^{ACCESS_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(task_callback, pattern=f"^{TASK_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(sub_callback, pattern=f"^{SUB_CALLBACK_PREFIX}:"))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("searchstatus", search_status))
    app.add_handler(CommandHandler("subs", subs_command))
    # Always register the ConversationHandler so text_message_entry intercepts
    # all plain-text messages (magnets, KP links, search queries).
    # When Rutracker is disabled text_message_entry falls back gracefully.
    app.add_handler(ConversationHandler(
        entry_points=[
            # /search is kept for backward-compat / menu discoverability.
            CommandHandler("search", search_start),
            # Every plain text message (KP links, search queries, magnets)
            # is routed by text_message_entry — no /search needed.
            MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
            # Jackett subscription "view results" entry point.
            CallbackQueryHandler(
                search_jackett_check_entry,
                pattern=rf"^{SUB_CALLBACK_PREFIX}:jackett_view:",
            ),
        ],
            states={
                SEARCH_QUERY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_query),
                ],
                SEARCH_OPTIONS: [
                    CallbackQueryHandler(search_quick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:quick$"),
                    CallbackQueryHandler(search_show_advanced, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:adv$"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                ],
                SEARCH_ADVANCED: [
                    CallbackQueryHandler(search_toggle_setting, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:quality:"),
                    CallbackQueryHandler(search_toggle_setting, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:toggle:"),
                    CallbackQueryHandler(search_do, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:do_search$"),
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
                    CallbackQueryHandler(search_expand_jackett, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:expand_jackett$"),
                    CallbackQueryHandler(search_jackett_start_direct, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:jackett_direct$"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                ],
                SEARCH_SEASON_SELECT: [
                    CallbackQueryHandler(search_season_pick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season:\d+$"),
                    CallbackQueryHandler(search_season_skip, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_skip$"),
                    CallbackQueryHandler(search_season_input_ask, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_input$"),
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
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
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
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Bot crashed")
        raise
