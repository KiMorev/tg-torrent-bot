import asyncio
import contextlib
import hashlib
import html as html_module
import json
import logging
import os
import random
import re
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
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
from diagnostics import friendly_error as _friendly_error, format_diagnostics, format_diagnostics_section, run_diagnostics
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
    BUTTON_BACK,
    BUTTON_CLOSE,
    BUTTON_DOWNLOAD_LIST,
    BUTTON_REFRESH,
    BUTTON_RETRY,
    BUTTON_SHOW_TASK,
    JACKETT_SELECT_PREFIX,
    SEARCH_CALLBACK_PREFIX,
    SEARCH_INTENT_SERIES_MASTER,
    TASK_CALLBACK_PREFIX,
    TASK_LIST_PAGE_SIZE,
    TASK_LIST_SCOPE_ALL,
    TASK_LIST_SCOPE_DEFAULT,
    TASK_LIST_SCOPE_MY,
    _SRCH_DEFAULT_SETTINGS,
    _SRCH_QUALITY_OPTIONS,
    _admin_diagnostics_detail_keyboard,
    _admin_diagnostics_keyboard,
    _admin_kp_cache_cleared_keyboard,
    _admin_kp_cache_confirm_keyboard,
    _admin_kp_force_refresh_keyboard,
    _admin_movie_status_keyboard,
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
    _download_error_keyboard,
    _cluster_picker_keyboard,
    _no_results_keyboard,
    _search_error_keyboard,
    _search_options_keyboard,
    _search_results_keyboard,
    tracker_selection_label,
    _season_input_keyboard,
    _season_select_keyboard,
    _season_back_to_picker_keyboard,
    SEARCH_PAGE_SIZE,
    SUB_CALLBACK_PREFIX,
    _task_callback,
    _task_error_keyboard,
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
from series_bulk_planner import (
    CandidateEvaluation,
    ReleaseProfile,
    STATUS_ALREADY_DOWNLOADING,
    STATUS_ALREADY_IN_PLEX,
    STATUS_EXACT,
    STATUS_GOOD,
    STATUS_MISSING,
    STATUS_NEEDS_DECISION,
    STATUS_PARTIAL,
    VOICE_ANY_RUSSIAN,
    VOICE_ANY_FROM_REFERENCE,
    VOICE_ORIGINAL_ONLY,
    VOICE_REQUIRE_SELECTED,
    VOICE_SINGLE_FROM_REFERENCE,
    KNOWN_VOICE_LABELS,
    SeriesBulkProfile,
    SeriesBulkPlan,
    SeasonPlan,
    build_series_bulk_plan,
    release_profile_from_title,
    season_pack_range_from_title,
)
from search_intent import (
    INTENT_ONE_RELEASE,
    INTENT_SERIES_MASTER,
    SearchIntentDraft,
    parse_search_intent,
    parse_search_intent_with_gpt,
)
from search_facts import (
    format_search_fact_line,
    load_search_fact_catalog,
    select_search_fact,
)
from series_continue import (
    SeriesCatchUpCandidate,
    build_series_catch_up_candidates,
    resolve_series_completeness,
    resolve_same_topic_update,
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
    is_unmatched as _plex_is_unmatched,
    _normalise_resolution as _plex_normalise_resolution,
)
from storage import (
    STORAGE_MOUNT_PATH, StorageInfo, format_bytes, get_storage_info,
    get_unified_disk_info,
)
from progressive_status import (
    ProgressiveStatus,
    SEARCH_ANIMATION_PATH, VOICE_ANIMATION_PATH,
    search_stages, voice_stages,
)
from voice_transcription import (
    check_api_key as voice_check_api_key,
    estimate_cost_usd as voice_estimate_cost_usd,
    transcribe_audio,
    transcribe_audio_detailed,
)
from gpt_client import estimate_chat_cost_usd
from gpt_features import diagnose_search_failure as gpt_diagnose_search_failure
from gpt_features import choose_movie_notification_release as gpt_choose_movie_notification_release
from gpt_features import did_you_mean as gpt_did_you_mean
from gpt_features import explain_movie_card as gpt_features_explain_movie_card
from gpt_features import explain_series_bulk_candidates as gpt_explain_series_bulk_candidates
from gpt_features import generate_search_fact_catalog as gpt_generate_search_fact_catalog
from gpt_features import kp_confidence_check as gpt_kp_confidence_check
from gpt_features import parse_torrent_title as gpt_features_parse_torrent_title
from movie_discovery import (
    _compute_card_score as _movie_compute_card_score,
    build_cards as _movie_build_cards,
    detect_quality as _movie_detect_quality,
    discovery_queries as _movie_discovery_queries,
    discovery_years as _movie_discovery_years,
    evaluate_result as _movie_evaluate_result,
    extract_year as _movie_extract_year,
    is_recent_published_at as _movie_is_recent_published_at,
    movie_key as _movie_card_key,
    normalize_movie_title as _normalize_movie_title,
    parse_size_gb as _movie_parse_size_gb,
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
    is_complete_despite_error as _policy_is_complete_despite_error,
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
PLEX_DEEPLINK_BASE_URL = settings.plex_deeplink_base_url
TOPIC_SUBSCRIPTIONS_FILE = settings.topic_subscriptions_file
SUBSCRIPTION_CHECK_INTERVAL_HOURS = settings.subscription_check_interval_hours
JACKETT_URL = settings.jackett_url
JACKETT_API_KEY = settings.jackett_api_key
JACKETT_ENABLED = settings.jackett_enabled
JACKETT_INDEXERS = settings.jackett_indexers
JACKETT_MAX_RESULTS = settings.jackett_max_results
JACKETT_FETCH_LIMIT = settings.jackett_fetch_limit
JACKETT_SEARCH_TIMEOUT_SECONDS = settings.jackett_search_timeout_seconds
JACKETT_WARMUP_ENABLED = settings.jackett_warmup_enabled
JACKETT_WARMUP_INTERVAL_SECONDS = settings.jackett_warmup_interval_seconds
JACKETT_WARMUP_QUERY = settings.jackett_warmup_query
JACKETT_WARMUP_INDEXERS = settings.jackett_warmup_indexers
JACKETT_WARMUP_BATCH_SIZE = settings.jackett_warmup_batch_size
MOVIE_DISCOVERY_ENABLED = settings.movie_discovery_enabled
MOVIE_DISCOVERY_INTERVAL_HOURS = settings.movie_discovery_interval_hours
MOVIE_DISCOVERY_DEBUG_FILE = settings.movie_discovery_debug_file
MOVIE_DISCOVERY_RUTRACKER_TM = settings.movie_discovery_rutracker_tm
MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE = settings.movie_discovery_jackett_require_date
MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS = settings.movie_discovery_jackett_max_age_days
MOVIE_DISCOVERY_LIMIT = settings.movie_discovery_limit
MOVIE_DISCOVERY_MIN_KP_RATING = settings.movie_discovery_min_kp_rating
MOVIE_DISCOVERY_QUALITIES = settings.movie_discovery_qualities
PENDING_DOWNLOADS_ENABLED = settings.pending_downloads_enabled
PENDING_DOWNLOADS_INTERVAL_SECONDS = settings.pending_downloads_interval_seconds
PENDING_DOWNLOADS_TTL_HOURS = settings.pending_downloads_ttl_hours
STORAGE_ALERT_PERCENT = settings.storage_alert_percent
OPENAI_API_KEY = settings.openai_api_key
VOICE_SEARCH_ENABLED = settings.voice_search_enabled and bool(OPENAI_API_KEY)
VOICE_MAX_SECONDS = settings.voice_max_seconds
GPT_ENABLED = settings.gpt_enabled and bool(OPENAI_API_KEY)
GPT_MODEL = settings.gpt_model
SEARCH_FACTS_CATALOG_FILE = STATE_DIR / "search_facts_catalog.json"

# kinopoiskapiunofficial.tech free tier: 500 requests/day
_KP_DAILY_LIMIT = 500
# Max stale KP entries refreshed per discovery run (mirrors movie_discovery._KP_MAX_STALE_REFRESH_PER_RUN)
_KP_MAX_STALE_REFRESH = 15
_MOVIE_DISCOVERY_MIN_STABLE_CARDS = 10
_MOVIE_DISCOVERY_REFRESH_COALESCE_SECONDS = 30.0
_movie_discovery_refresh_lock: "asyncio.Lock | None" = None
_movie_discovery_refresh_current_mode: str = ""
_movie_discovery_refresh_last_mode: str = ""
_movie_discovery_refresh_last_finished_at: float = 0.0

KP_URL_FILTER = filters.Regex(KP_URL_RE)
SEARCH_OPTIONS, SEARCH_ADVANCED, SEARCH_RESULTS, SEARCH_SEASON_SELECT, SEARCH_JACKETT_SELECT = range(5)
SEARCH_PLEX_CONFIRM = 5  # Waiting for user to confirm/cancel Plex duplicate warning
SETTINGS_CALLBACK_PREFIX = "settings"
BOT_COMMANDS = [
    BotCommand("new", "Новинки фильмов"),
    BotCommand("subs", "Подписки на обновления"),
    BotCommand("settings", "Предпочтения поиска"),
    BotCommand("continue", "Докачать сезон"),
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
CONTINUE_CALLBACK_PREFIX = "cont"
CONTINUE_PAGE_SIZE = 10
CONTINUE_STATE_KEY = "continue_state"
# chat_id → имя пользователя (заполняется при запросе доступа)
ACCESS_PENDING_USERS: dict[int, str] = {}
BACKGROUND_MONITOR_TASK: asyncio.Task | None = None
TRACKER_BACKGROUND_TASK: asyncio.Task | None = None
PROGRESS_UPDATE_TASK: asyncio.Task | None = None
SUBSCRIPTION_MONITOR_TASK: asyncio.Task | None = None
MOVIE_DISCOVERY_TASK: asyncio.Task | None = None
JACKETT_WARMUP_TASK: asyncio.Task | None = None
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
# Unix timestamp of next scheduled pending-downloads check (set by the loop)
_next_pending_check_at: float | None = None
_next_search_facts_catalog_check_at: float | None = None
_SEARCH_FACTS_REFRESH_RETRY_ERRORS = {
    "timeout",
    "network",
    "rate_limit",
    "server_error",
    "parse",
    "invalid_catalog",
    "empty",
}
_next_jackett_warmup_at: float | None = None
_jackett_warmup_cursor: int = 0
_JACKETT_WARMUP_STATUS: dict[str, object] = {}


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


def _pick_search_fact_for_chat(chat_id: int | None, query: str = "") -> str:
    if chat_id is None:
        return ""
    try:
        facts, aliases, _catalog_markers = load_search_fact_catalog(SEARCH_FACTS_CATALOG_FILE)
        state = state_store.load_search_facts_state()
        fact_text, updated_state = select_search_fact(facts, state, int(chat_id), query=query, aliases=aliases)
        if fact_text:
            state_store.save_search_facts_state(updated_state)
        return format_search_fact_line(fact_text)
    except Exception:
        logger.debug("Search fact selection failed", exc_info=True)
        return ""

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


def _build_value_props(*, joined: bool = True) -> str | list[str]:
    """Compose the bullet list of features available to an approved user.

    Each bullet is conditioned on the corresponding configuration flag so a
    minimal install (no Plex, no Movie discovery, no Kinopoisk) doesn't
    promise capabilities it doesn't have. Reused by access-pending and
    post-approval access messages.
    """
    search_enabled = RUTRACKER_ENABLED or JACKETT_ENABLED
    bullets: list[str] = []
    if search_enabled:
        bullets.append(
            "• 🔍 Поиск и скачивание торрентов — просто пришлите название фильма"
        )
    if GPT_ENABLED and search_enabled:
        bullets.append(
            "• 🧠 Умный поиск с AI: автоисправление опечаток, подсказки, "
            "пояснения «почему этот фильм» в карточках /new"
        )
    if VOICE_SEARCH_ENABLED and search_enabled:
        bullets.append(
            "• 🎙 Можно искать голосом — запишите голосовое сообщение, бот распознает"
        )
    if MOVIE_DISCOVERY_ENABLED and search_enabled:
        bullets.append(
            "• 🎬 Подборка свежих фильмов и сериалов с рейтингом Кинопоиска — /new"
        )
    if PLEX_ENABLED:
        bullets.append(
            "• ▶️ Открытие готового контента в Plex одной кнопкой"
        )
    bullets.append(
        "• Для неполных сериалов: ⬇️ — скачать сейчас, новые серии по мере выхода или ждать завершения сезона; 🔔 — уведомления"
    )
    return "\n".join(bullets) if joined else bullets


def _build_access_approved_text() -> str:
    """Message sent to a user immediately after admin approval."""
    return (
        "✅ Доступ разрешён.\n"
        "\n"
        "Теперь можно пользоваться ботом:\n"
        f"{_build_value_props()}\n"
        "\n"
        "Напишите название фильма или сериала, отправьте ссылку Кинопоиска, "
        ".torrent-файл или magnet-ссылку.\n"
        "Подробно по возможностям — /help."
    )


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
            "👋 Это <b>PlexLoader</b> — 🧠 умный помощник для домашнего киносервера на базе Plex.\n"
            "\n"
            "После одобрения вам будут доступны:\n"
            f"{_build_value_props()}\n"
            "\n"
            f"Ваш chat_id: <code>{chat_id}</code>\n"
            f"{tail}",
            parse_mode="HTML",
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


def _jackett_warmup_enabled() -> bool:
    return JACKETT_WARMUP_ENABLED and jackett_client is not None


def _split_indexer_ids(raw: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in (raw or "").split(","):
        indexer_id = item.strip().lower()
        if not indexer_id or indexer_id == "all" or indexer_id in seen:
            continue
        seen.add(indexer_id)
        result.append(indexer_id)
    return result


def _jackett_warmup_configured_indexers() -> list[str] | None:
    warmup_raw = (JACKETT_WARMUP_INDEXERS or "auto").strip().lower()
    if warmup_raw and warmup_raw != "auto":
        return _split_indexer_ids(warmup_raw)
    if (JACKETT_INDEXERS or "all").strip().lower() != "all":
        return _split_indexer_ids(JACKETT_INDEXERS)
    return None


def _jackett_warmup_next_batch(indexers: list[str], batch_size: int | None = None) -> list[str]:
    global _jackett_warmup_cursor
    pool = list(dict.fromkeys(i.strip().lower() for i in indexers if i and i.strip()))
    if not pool:
        return []
    size = max(1, min(batch_size or JACKETT_WARMUP_BATCH_SIZE, len(pool)))
    start = _jackett_warmup_cursor % len(pool)
    batch = [pool[(start + offset) % len(pool)] for offset in range(size)]
    _jackett_warmup_cursor = (start + size) % len(pool)
    return batch


def _format_warmup_dt(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _jackett_warmup_status_snapshot() -> dict:
    status = dict(_JACKETT_WARMUP_STATUS)
    status["enabled"] = _jackett_warmup_enabled()
    status["interval_seconds"] = JACKETT_WARMUP_INTERVAL_SECONDS
    status["query"] = JACKETT_WARMUP_QUERY
    status["batch_size"] = JACKETT_WARMUP_BATCH_SIZE
    status["next_check"] = _format_warmup_dt(_next_jackett_warmup_at)
    return status


async def _jackett_warmup_indexer_pool() -> tuple[list[str], str]:
    configured = _jackett_warmup_configured_indexers()
    if configured is not None:
        return configured, ""
    if jackett_client is None:
        return [], "disabled"
    try:
        indexers = await asyncio.to_thread(jackett_client.get_indexers_if_idle)
    except JackettError as exc:
        return [], str(exc)
    if indexers is None:
        return [], "busy"
    return [idx["id"] for idx in indexers if isinstance(idx, dict) and idx.get("id")], ""


async def _run_jackett_warmup_once() -> dict:
    now_ts = time.time()
    if not _jackett_warmup_enabled():
        _JACKETT_WARMUP_STATUS.update({
            "enabled": False,
            "last_state": "disabled",
            "last_checked": _format_warmup_dt(now_ts),
        })
        return dict(_JACKETT_WARMUP_STATUS)

    pool, pool_error = await _jackett_warmup_indexer_pool()
    if pool_error:
        state = "skipped" if pool_error == "busy" else "failed"
        _JACKETT_WARMUP_STATUS.update({
            "enabled": True,
            "last_state": state,
            "last_error": pool_error,
            "last_checked": _format_warmup_dt(now_ts),
        })
        logger.info("jackett_warmup: %s before search reason=%s", state, pool_error)
        return dict(_JACKETT_WARMUP_STATUS)

    batch = _jackett_warmup_next_batch(pool)
    if not batch:
        _JACKETT_WARMUP_STATUS.update({
            "enabled": True,
            "last_state": "skipped",
            "last_error": "no indexers",
            "last_checked": _format_warmup_dt(now_ts),
        })
        logger.info("jackett_warmup: skipped no indexers")
        return dict(_JACKETT_WARMUP_STATUS)

    assert jackett_client is not None
    result = await asyncio.to_thread(
        jackett_client.warmup,
        JACKETT_WARMUP_QUERY,
        indexers=batch,
        timeout=(5.0, 10.0),
    )
    checked_at = _format_warmup_dt(time.time())
    if result.get("ok"):
        _JACKETT_WARMUP_STATUS.update({
            "enabled": True,
            "last_state": "ok",
            "last_ok": checked_at,
            "last_checked": checked_at,
            "last_error": "",
            "last_indexers": batch,
            "last_results_count": int(result.get("results_count") or 0),
            "last_elapsed_seconds": result.get("elapsed_seconds"),
            "failed_indexers": result.get("failed_indexers") or [],
        })
        logger.info(
            "jackett_warmup: success indexers=%s results=%s elapsed=%ss",
            ",".join(batch),
            result.get("results_count"),
            result.get("elapsed_seconds"),
        )
    else:
        state = "skipped" if result.get("skipped") else "failed"
        error = str(result.get("reason") or result.get("error") or result.get("error_kind") or "unknown")
        _JACKETT_WARMUP_STATUS.update({
            "enabled": True,
            "last_state": state,
            "last_checked": checked_at,
            "last_error": error,
            "last_error_kind": result.get("error_kind", ""),
            "last_indexers": batch,
            "last_elapsed_seconds": result.get("elapsed_seconds"),
        })
        logger.info("jackett_warmup: %s indexers=%s error=%s", state, ",".join(batch), error)
    return dict(_JACKETT_WARMUP_STATUS)


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


def _download_added_tracker_lines(result: TrackerApplyResult | None) -> list[str]:
    if not result or not _public_trackers_enabled():
        return []
    if result.skipped_reason:
        return [f"Public-трекеры: {result.skipped_reason}"]
    if not result.added_count and not result.available_count:
        return ["Public-трекеры: список недоступен"]
    return []


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


async def _run_pending_downloads_gated(app: Application) -> None:
    """Wrap _run_pending_downloads_once with a per-cycle gate.

    The maintenance loop ticks every TRACKERS_BACKGROUND_INTERVAL_SECONDS, but
    we only want to attempt pending downloads at PENDING_DOWNLOADS_INTERVAL_SECONDS
    cadence (typically much longer, e.g. 5 min). The gate stores the next-allowed
    timestamp at module scope so it persists across cycles.
    """
    global _next_pending_check_at
    if not _pending_downloads_enabled():
        return
    now_ts = time.time()
    if _next_pending_check_at is not None and now_ts < _next_pending_check_at:
        return
    try:
        await _run_pending_downloads_once(app)
    finally:
        _next_pending_check_at = time.time() + PENDING_DOWNLOADS_INTERVAL_SECONDS


def _search_facts_catalog_refresh_needed(state: dict, now_ts: float | None = None) -> bool:
    catalog = state.get("catalog")
    if not isinstance(catalog, dict):
        return True
    if catalog.get("refresh_requested_at"):
        return True
    return not bool(catalog.get("initial_refresh_attempted_at"))


def _search_facts_catalog_refresh_gated(state: dict, now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    catalog = state.get("catalog")
    if not isinstance(catalog, dict):
        return True
    try:
        last_attempt = float(catalog.get("last_refresh_attempt_ts") or 0.0)
    except (TypeError, ValueError):
        last_attempt = 0.0
    return now_ts - last_attempt >= 24 * 60 * 60


async def _run_search_facts_catalog_refresh_once() -> None:
    if not GPT_ENABLED:
        return

    state = state_store.load_search_facts_state()
    now_ts = time.time()
    if not _search_facts_catalog_refresh_needed(state, now_ts):
        return
    if not _search_facts_catalog_refresh_gated(state, now_ts):
        return

    catalog_state = state.get("catalog")
    if not isinstance(catalog_state, dict):
        catalog_state = {}
        state["catalog"] = catalog_state
    catalog_state["initial_refresh_attempted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    catalog_state["last_refresh_attempt_ts"] = now_ts
    catalog_state["last_refresh_attempt_at"] = catalog_state["initial_refresh_attempted_at"]
    state_store.save_search_facts_state(state)

    facts, aliases, _markers = load_search_fact_catalog(SEARCH_FACTS_CATALOG_FILE)
    usage_sink: list[dict] = []
    catalog, error = await asyncio.to_thread(
        gpt_generate_search_fact_catalog,
        existing_facts=facts,
        existing_aliases=aliases,
        api_key=OPENAI_API_KEY,
        model=GPT_MODEL,
        target_count=max(100, len(facts)),
        usage_sink=usage_sink,
    )
    _gpt_record_usage(
        feature="search_fact_catalog",
        input_tokens=1200,
        output_tokens=3500,
        error_label=error,
        usage=(usage_sink[0] if usage_sink else None),
    )

    state = state_store.load_search_facts_state()
    catalog_state = state.get("catalog")
    if not isinstance(catalog_state, dict):
        catalog_state = {}
        state["catalog"] = catalog_state

    if error or catalog is None:
        error_label = error or "empty"
        catalog_state["last_refresh_error"] = error_label
        if error_label in _SEARCH_FACTS_REFRESH_RETRY_ERRORS:
            catalog_state["refresh_requested_at"] = (
                catalog_state.get("refresh_requested_at")
                or catalog_state.get("last_refresh_attempt_at")
            )
        state_store.save_search_facts_state(state)
        logger.warning("search_facts: GPT catalog refresh failed error=%s", error)
        return

    state_store.save_json_file(SEARCH_FACTS_CATALOG_FILE, catalog, "search facts runtime catalog")
    catalog_state.pop("refresh_requested_at", None)
    catalog_state["shown_unique_ids"] = []
    catalog_state["last_refresh_error"] = ""
    catalog_state["last_refresh_success_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    catalog_state["runtime_catalog_generated_at"] = str(catalog.get("generated_at") or "")
    state_store.save_search_facts_state(state)
    logger.info(
        "search_facts: runtime catalog refreshed facts=%d aliases=%d",
        len(catalog.get("facts") or []),
        len(catalog.get("aliases") or {}),
    )


async def _run_search_facts_catalog_refresh_gated() -> None:
    global _next_search_facts_catalog_check_at
    now_ts = time.time()
    if _next_search_facts_catalog_check_at is not None and now_ts < _next_search_facts_catalog_check_at:
        return
    try:
        await _run_search_facts_catalog_refresh_once()
    finally:
        _next_search_facts_catalog_check_at = time.time() + 60 * 60


async def _run_task_maintenance_cycle(app: Application) -> None:
    await _run_background_step("task notifications", lambda: _run_task_notifications_once(app))
    await _run_background_step("auto-delete finished tasks", _run_auto_delete_finished_once)
    await _run_background_step("pending downloads", lambda: _run_pending_downloads_gated(app))
    await _run_background_step("search facts catalog refresh", _run_search_facts_catalog_refresh_gated)
    await _run_background_step("storage snapshot", lambda: _run_storage_snapshot_gated(app))
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
        # Toggle only the notification axis. The download policy is preserved.
        mode_label = _admin_subscription_toggle_label(sub)
        if sub.get("type") == "jackett":
            rows.append([
                InlineKeyboardButton(
                    f"🗑️ {index}. Jackett",
                    callback_data=f"{SUB_CALLBACK_PREFIX}:admin_jackett_unsub:{key}",
                ),
                InlineKeyboardButton(
                    mode_label,
                    callback_data=f"{SUB_CALLBACK_PREFIX}:admin_set_mode:{key}",
                ),
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"🗑️ {index}. Rutracker",
                    callback_data=f"{SUB_CALLBACK_PREFIX}:admin_unsub:{key}",
                ),
                InlineKeyboardButton(
                    mode_label,
                    callback_data=f"{SUB_CALLBACK_PREFIX}:admin_set_mode:{key}",
                ),
            ])

    rows.append([
        InlineKeyboardButton(BUTTON_REFRESH, callback_data=f"{ADMIN_CALLBACK_PREFIX}:subscriptions"),
        InlineKeyboardButton("⬅️ Админ-панель", callback_data=f"{ADMIN_CALLBACK_PREFIX}:home"),
    ])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=f"{ADMIN_CALLBACK_PREFIX}:close")])
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
        mode_label = html_module.escape(policies_summary_ru(sub))
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
                f"   Режим: {mode_label}\n"
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
            f"   Серии: {ep_end} из {total}\n"
            f"   Режим: {mode_label}"
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


def _count_stuck_notifications() -> int:
    """Count tasks whose notification delivery has hit the failure cap for
    at least one recipient. Such tasks will never receive a push until the
    failure counter is reset (admin button «🔄 Сбросить счётчики»).
    """
    notified = _load_notified_tasks()
    cap = MAX_TASK_NOTIFICATION_FAILURES
    count = 0
    for entry in notified.values():
        if not isinstance(entry, dict):
            continue
        failures = entry.get("failures") or {}
        if not isinstance(failures, dict):
            continue
        for v in failures.values():
            try:
                if int(v or 0) >= cap:
                    count += 1
                    break
            except (TypeError, ValueError):
                continue
    return count


def _format_admin_stuck_notifications_line() -> str:
    """One-line status: are any task notifications stuck at failure cap?"""
    n = _count_stuck_notifications()
    if n == 0:
        return "• Уведомления о завершении: ✅ всё доставляется"
    return (
        f"• Уведомления о завершении: ⚠️ зависших {n} "
        f"{_plural(n, 'задача', 'задачи', 'задач')} — "
        f"тапни «🔄 Сбросить счётчики ({n})»"
    )


def _format_admin_search_defaults_line() -> str:
    quality = _SRCH_DEFAULT_SETTINGS.get("quality", "1080p")
    audio = "да" if _SRCH_DEFAULT_SETTINGS.get("audio") else "нет"
    subs = "да" if _SRCH_DEFAULT_SETTINGS.get("subs") else "нет"
    return f"• Поиск: {quality} · оригинальная дорожка: {audio} · субтитры: {subs}"


def _format_admin_search_facts_line() -> str:
    try:
        facts, _aliases, markers = load_search_fact_catalog(SEARCH_FACTS_CATALOG_FILE)
        state = state_store.load_search_facts_state()
    except Exception:
        logger.debug("Search facts admin status failed", exc_info=True)
        return "• Факты ожидания: статус недоступен"

    if not isinstance(state, dict):
        state = {}
    catalog_state = state.get("catalog")
    if not isinstance(catalog_state, dict):
        catalog_state = {}

    source = "встроенный" if markers.get("source") == "bundled" else "GPT-каталог"
    total = int(catalog_state.get("total_facts") or len(facts) or 0)
    shown_ids = catalog_state.get("shown_unique_ids")
    shown = len(shown_ids) if isinstance(shown_ids, list) else 0
    try:
        shown_percent = float(catalog_state.get("shown_percent") or 0.0)
    except (TypeError, ValueError):
        shown_percent = 0.0

    parts = [
        f"• Факты ожидания: {source}",
        f"{len(facts)} фактов",
        f"показано {shown}/{total} ({shown_percent * 100:.0f}%)",
    ]
    if catalog_state.get("refresh_requested_at"):
        parts.append("ожидает GPT-refresh" if GPT_ENABLED else "refresh ждёт включённый GPT")
    last_success = _short_admin_datetime(catalog_state.get("last_refresh_success_at"))
    last_error = str(catalog_state.get("last_refresh_error") or "").strip()
    if last_error:
        label = "обновление" if facts else "ошибка"
        parts.append(f"{label}: {html_module.escape(last_error)}")
    elif last_success:
        parts.append(f"GPT успех {last_success}")
    return " · ".join(parts)


def _short_admin_datetime(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return html_module.escape(raw)
    if dt.tzinfo:
        dt = dt.astimezone(DISPLAY_TIMEZONE)
    return dt.strftime("%d.%m %H:%M")


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


# ---------------------------------------------------------------------------
# Storage budget — /admin disk-usage block + background snapshot/alert
# ---------------------------------------------------------------------------

# One-shot alert flag (in-memory). Module-scope dict so it survives across
# task ticks but resets on process restart (acceptable — alert will simply
# re-fire on the first crossing after restart if usage is still high).
_STORAGE_ALERT_STATE: dict[str, bool] = {"above": False}

# Gate timestamp for storage snapshot (separate from pending-downloads gate).
_next_storage_snapshot_at: float | None = None


def _format_admin_voice_alert() -> str | None:
    """Surface a one-line warning in the main /admin panel when the voice
    search feature is configured but broken.

    Triggers on: invalid API key (last_error.type=="auth") or exhausted
    quota (last_error.type=="quota_exceeded"). Other transient errors stay
    in the diagnostics view; they self-recover.
    """
    if not VOICE_SEARCH_ENABLED:
        return None
    usage = state_store.load_voice_usage()
    last_error = usage.get("last_error") if isinstance(usage.get("last_error"), dict) else None
    if not last_error:
        return None
    err_type = str(last_error.get("type") or "")
    if err_type == "quota_exceeded":
        return "⚠️ 🎙 Голосовой поиск: исчерпан баланс/лимит OpenAI"
    if err_type == "auth":
        return "⚠️ 🎙 Голосовой поиск: ключ OpenAI невалиден"
    return None


def _format_admin_gpt_alert() -> str | None:
    """Mirror of _format_admin_voice_alert for the GPT chat usage side.

    Same trigger semantics — terminal OpenAI errors on chat completions
    bubble up to the main /admin panel so the operator sees them without
    drilling into Диагностика.
    """
    if not GPT_ENABLED:
        return None
    usage = state_store.load_gpt_usage()
    last_error = usage.get("last_error") if isinstance(usage.get("last_error"), dict) else None
    if not last_error:
        return None
    err_type = str(last_error.get("type") or "")
    if err_type == "quota_exceeded":
        return "⚠️ 🧠 GPT chat: исчерпан баланс/лимит OpenAI"
    if err_type == "auth":
        return "⚠️ 🧠 GPT chat: ключ OpenAI невалиден"
    return None


def _format_storage_forecast(info: StorageInfo) -> str | None:
    """7-day linear extrapolation to STORAGE_ALERT_PERCENT.

    Returns None if there's no usable history yet (need ≥2 snapshots
    spanning some time). Handles zero/negative growth gracefully.
    """
    history = state_store.load_storage_history()
    if len(history) < 2:
        return None

    from datetime import timedelta
    now_ts = datetime.now(DISPLAY_TIMEZONE)
    # Pick the entry closest to a week ago (older end); fall back to oldest.
    week_ago = now_ts - timedelta(days=7)
    older = None
    for entry in history:
        try:
            entry_ts = datetime.fromisoformat(entry["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        # Normalise to compare (drop tz if any — we store naive ISO).
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=DISPLAY_TIMEZONE)
        if entry_ts <= week_ago.replace(tzinfo=entry_ts.tzinfo):
            older = entry  # latest snapshot that's still ≥ 1 week old
    if older is None:
        older = history[0]  # nothing a full week old yet — use oldest available

    try:
        older_ts = datetime.fromisoformat(older["ts"])
        if older_ts.tzinfo is None:
            older_ts = older_ts.replace(tzinfo=DISPLAY_TIMEZONE)
    except (ValueError, TypeError, KeyError):
        return None

    delta_seconds = (now_ts - older_ts).total_seconds()
    delta_days = max(0.5, delta_seconds / 86400)  # avoid div-by-zero
    delta_used = info.used_bytes - int(older.get("used_bytes") or 0)
    rate_per_day = delta_used / delta_days

    if rate_per_day <= 0:
        return "Прогноз: темп заполнения нулевой или отрицательный"
    target = info.total_bytes * (STORAGE_ALERT_PERCENT / 100)
    bytes_until = target - info.used_bytes
    if bytes_until <= 0:
        return f"Порог {STORAGE_ALERT_PERCENT}% уже превышен"
    days_left = int(bytes_until / rate_per_day)
    suffix = _plural(days_left, "день", "дня", "дней")
    return f"Прогноз: ~{days_left} {suffix} до {STORAGE_ALERT_PERCENT}%"


def _format_admin_storage_section() -> str | None:
    """Build the «📀 Хранилище» admin block, or None when feature disabled.

    Uses the unified disk-info helper: prefers the `/storage` bind-mount
    (fast, history-friendly for the 7-day forecast), falls back to the DSM
    API (`SYNO.Core.Storage.Volume.list`). The block hides itself only when
    BOTH sources fail — most installs will see it light up automatically
    via DSM API even without configuring the bind-mount.
    """
    info = get_unified_disk_info(ds_client)
    if info is None:
        return None
    icon = "⚠️" if info.used_percent >= STORAGE_ALERT_PERCENT else "📀"
    lines = [
        f"{icon} <b>Хранилище</b>",
        f"• Занято: {format_bytes(info.used_bytes)} из {format_bytes(info.total_bytes)} "
        f"({info.used_percent:.0f}%)",
    ]
    forecast = _format_storage_forecast(info)
    if forecast:
        lines.append(f"• {forecast}")
    return "\n".join(lines)


async def _maybe_send_storage_alert(info: StorageInfo, app: "Application") -> None:
    """Push one-shot alert to admins when usage crosses STORAGE_ALERT_PERCENT.

    The `_STORAGE_ALERT_STATE["above"]` flag is set on first crossing into
    the alert region and cleared when usage drops below — so a sustained
    high-usage period sends exactly one push, not one per snapshot cycle.
    """
    above = info.used_percent >= STORAGE_ALERT_PERCENT
    if above and not _STORAGE_ALERT_STATE["above"]:
        _STORAGE_ALERT_STATE["above"] = True
        text = (
            f"⚠️ Хранилище NAS заполнено на {info.used_percent:.0f}%.\n"
            f"Занято {format_bytes(info.used_bytes)} из {format_bytes(info.total_bytes)} "
            f"({format_bytes(info.free_bytes)} свободно).\n"
            f"Откройте /admin для подробностей."
        )
        for admin_id in ADMIN_CHAT_IDS:
            try:
                await app.bot.send_message(chat_id=admin_id, text=text)
            except Exception:
                logger.warning("Storage alert send failed chat=%s", admin_id, exc_info=True)
    elif not above and _STORAGE_ALERT_STATE["above"]:
        _STORAGE_ALERT_STATE["above"] = False


async def _run_storage_snapshot_gated(app: "Application") -> None:
    """Take a disk-usage snapshot at most every 6h; fires alert on threshold crossing.

    Runs from `_run_task_maintenance_cycle` every 180s. The gate uses
    module-scope timestamp so the cadence persists across cycles. Feature
    is no-op when `/storage` mount isn't present.
    """
    global _next_storage_snapshot_at
    info = get_storage_info(STORAGE_MOUNT_PATH)
    if info is None:
        return  # mount missing → feature disabled

    now_ts = time.time()
    if _next_storage_snapshot_at is not None and now_ts < _next_storage_snapshot_at:
        # Even when gated for snapshot writes, still check the alert — fresh
        # readings shouldn't be ignored just because we don't want to spam
        # the history file. The alert itself is debounced via _STORAGE_ALERT_STATE.
        await _maybe_send_storage_alert(info, app)
        return

    state_store.append_storage_snapshot({
        "ts": datetime.now(DISPLAY_TIMEZONE).isoformat(timespec="seconds"),
        "used_bytes": info.used_bytes,
        "free_bytes": info.free_bytes,
    })
    _next_storage_snapshot_at = now_ts + 6 * 3600

    await _maybe_send_storage_alert(info, app)


def _format_admin_movie_discovery_summary() -> str:
    """Compact movie-discovery block for the main /admin panel.

    Shows only dynamic state worth glancing at: status, cache freshness +
    card count, KP API budget today (if KP configured), and subscriber
    count (if any). All static config (sources, filters, intervals, tracker
    rating breakdown, KP cache size) lives in the «🎬 Новинки» drill-down
    rendered by _format_admin_movie_discovery_details().
    """
    if not MOVIE_DISCOVERY_ENABLED:
        return "• Статус: выключены"

    cache = state_store.load_movie_discovery_cache()
    cards = cache.get("cards") if isinstance(cache.get("cards"), list) else []
    updated_at = str(cache.get("updated_at") or "ещё не обновлялись")
    sources_present = rutracker_client is not None or jackett_client is not None
    status = "включены" if sources_present else "включены, но нет источников"

    lines = [
        f"• Статус: {status}",
        f"• Кэш: {html_module.escape(updated_at)} · карточек: {len(cards)}",
    ]

    kp_stats_line = _format_kp_api_stats_line(cache)
    if kp_stats_line:
        lines.append(kp_stats_line)

    movie_sub_count = len(_get_movie_subscriptions())
    if movie_sub_count:
        lines.append(f"• Подписок на /new: {movie_sub_count}")

    return "\n".join(lines)


def _format_admin_movie_discovery_details() -> str:
    """Full movie-discovery configuration screen — drill-down from main panel.

    Includes everything that used to be inline on the main panel: source list,
    quality/year/age filters, Rutracker time-machine range, Jackett date
    constraints, auto-update interval, Jackett tracker rating breakdown,
    plus a separate KP-cache section (entry count + match/miss split + budget).
    """
    if not MOVIE_DISCOVERY_ENABLED:
        return "🎬 <b>Новинки</b>\n\n• Статус: выключены"

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

    lines = [
        "🎬 <b>Новинки</b> — настройки и обслуживание",
        "",
        f"• Источники: {source_text}",
        f"• Общие фильтры: {qualities} · КП от {MOVIE_DISCOVERY_MIN_KP_RATING:g}",
        f"• Rutracker: {_movie_rutracker_tm_label(MOVIE_DISCOVERY_RUTRACKER_TM)}",
        f"• Jackett: {'только с датой' if MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE else 'без строгой даты'} · "
        f"до {MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS} дн.",
        f"• Автообновление: раз в {MOVIE_DISCOVERY_INTERVAL_HOURS} ч",
        f"• Кэш: {html_module.escape(updated_at)} · карточек: {len(cards)}",
    ]

    # Tracker rating breakdown
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

    # KP API section — separate block. Only meaningful if KP is enabled.
    if KINOPOISK_ENABLED:
        kp_stats_line = _format_kp_api_stats_line(cache)
        kp_cache_dict = cache.get("kp_cache") if isinstance(cache.get("kp_cache"), dict) else {}
        total_entries = len(kp_cache_dict)
        matched = sum(1 for e in kp_cache_dict.values() if isinstance(e, dict) and e.get("kp_id"))

        lines.append("")
        lines.append("<b>KP API</b>")
        if kp_stats_line:
            lines.append(kp_stats_line)
        lines.append(f"• Записей в кэше: {total_entries} ({matched} с матчем)")

    # Subscriber count (kept here too — operator may want it next to other
    # operational details when troubleshooting why pushes go/don't go).
    movie_sub_count = len(_get_movie_subscriptions())
    if movie_sub_count:
        lines.append("")
        lines.append(f"• Подписок на /new: {movie_sub_count}")

    return "\n".join(lines)


def _admin_panel_kb_kwargs() -> dict:
    """Build the kwargs dict for _admin_panel_keyboard() based on current state.

    Centralised here so both /admin entry points (slash command + callback)
    render an identical panel without each duplicating the gathering logic.
    """
    stuck = _count_stuck_notifications()
    if not PLEX_ENABLED:
        return {
            "show_plex_unmatched": False,
            "stuck_notifications_count": stuck,
        }
    counts = _get_plex_unmatched_counts()
    return {
        "show_plex_unmatched": True,
        "plex_unmatched_count": counts["total"],
        "plex_unmatched_notify_enabled": _is_plex_unmatched_notify_enabled(),
        "stuck_notifications_count": stuck,
    }


_ADMIN_TASKS_TIMEOUT_SECONDS = 3.0


async def _build_admin_panel_text() -> str:
    tasks = None
    task_error = ""
    try:
        tasks = await asyncio.wait_for(
            asyncio.to_thread(ds_client.list_tasks),
            timeout=_ADMIN_TASKS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        task_error = f"Download Station не ответил за {_ADMIN_TASKS_TIMEOUT_SECONDS:g} с"
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
        _format_admin_stuck_notifications_line(),
        "",
        "⚙️ <b>Правила и интеграции</b>",
        _format_admin_configured_integrations(),
        _format_admin_auto_delete_line(),
        _format_admin_notifications_line(),
        _format_admin_search_defaults_line(),
        _format_admin_search_facts_line(),
        "<i>Живой статус сервисов — в разделе «Диагностика».</i>",
        "",
        "🎬 <b>Новинки</b>",
        _format_admin_movie_discovery_summary(),
    ]
    storage_block = _format_admin_storage_section()
    if storage_block:
        lines.append("")
        lines.append(storage_block)
    voice_alert = _format_admin_voice_alert()
    if voice_alert:
        lines.append("")
        lines.append(voice_alert)
    gpt_alert = _format_admin_gpt_alert()
    if gpt_alert:
        lines.append("")
        lines.append(gpt_alert)
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


def _task_owner_labels(owners: dict[str, int]) -> dict[int, str]:
    if not owners:
        return {}

    approved_users = state_store.load_approved_users()
    labels: dict[int, str] = {}
    for owner_id in set(owners.values()):
        info = approved_users.get(owner_id, {})
        name = str(info.get("name", "")).strip() if isinstance(info, dict) else ""
        labels[owner_id] = f"{name} ({owner_id})" if name else str(owner_id)
    return labels


def _format_tasks(
    tasks: list[dict],
    scope: str = TASK_LIST_SCOPE_ALL,
    total_count: int | None = None,
    page: int = 0,
) -> str:
    owners = _load_task_owners() if scope == TASK_LIST_SCOPE_ALL else {}
    auto_delete_enabled = _auto_delete_finished_enabled()
    return _view_format_tasks(
        tasks,
        scope=scope,
        updated_at=_format_updated_at(),
        owners=owners,
        owner_labels=_task_owner_labels(owners),
        total_count=total_count,
        page=page,
        page_size=TASK_LIST_PAGE_SIZE,
        scope_all=TASK_LIST_SCOPE_ALL,
        auto_delete_tasks=_load_auto_delete_tasks() if auto_delete_enabled else {},
        auto_delete_enabled=auto_delete_enabled,
        auto_delete_statuses=AUTO_DELETE_FINISHED_STATUSES,
        auto_delete_after_hours=AUTO_DELETE_FINISHED_AFTER_HOURS,
        display_timezone=DISPLAY_TIMEZONE,
    )


def _load_movie_discovery_cache() -> dict:
    return state_store.load_movie_discovery_cache()


def _save_movie_discovery_cache(cache: dict) -> None:
    state_store.save_movie_discovery_cache(cache)


def _load_movie_discovery_settings() -> dict:
    return state_store.load_movie_discovery_settings()


def _save_movie_discovery_settings(settings: dict) -> None:
    state_store.save_movie_discovery_settings(settings)


# --- Plex unmatched notification helpers (Block 2 of admin radar) ---

def _is_plex_unmatched_notify_enabled() -> bool:
    """Runtime toggle for push notifications about Plex-unmatched files.

    Persisted in ``movie_discovery_settings`` so it survives bot restarts and
    can be flipped through ``/admin`` without changing ``.env``. Defaults to
    False — the feature is opt-in.
    """
    return bool(_load_movie_discovery_settings().get("plex_unmatched_notify_enabled", False))


def _set_plex_unmatched_notify_enabled(value: bool) -> None:
    """Persist the runtime toggle for Plex-unmatched push notifications."""
    settings = _load_movie_discovery_settings()
    settings["plex_unmatched_notify_enabled"] = bool(value)
    _save_movie_discovery_settings(settings)


def _load_plex_unmatched_seen() -> dict:
    """Return the persisted set of rating_keys that were unmatched in the previous
    Plex refresh, split by ``movies`` / ``shows``.

    Used as the diff baseline for push notifications. The map is updated on every
    refresh regardless of the toggle state, so a quick off→on flip doesn't trigger
    a spam burst about previously-seen files.
    """
    seen = _load_movie_discovery_settings().get("plex_unmatched_seen") or {}
    return {
        "movies": list(seen.get("movies") or []),
        "shows":  list(seen.get("shows")  or []),
    }


def _save_plex_unmatched_seen(seen: dict) -> None:
    """Persist the rating_keys-of-unmatched snapshot. Stored sorted+deduped for
    deterministic JSON output (easier to compare across refreshes during debugging).
    """
    settings = _load_movie_discovery_settings()
    settings["plex_unmatched_seen"] = {
        "movies": sorted(set(seen.get("movies") or [])),
        "shows":  sorted(set(seen.get("shows")  or [])),
    }
    _save_movie_discovery_settings(settings)


# --- Per-user "seen in /new" tracking ---
# Tracks two independent per-user signals for each film card:
#
#   - notified_at  — bot has pushed the film to this user (blocks duplicate push)
#   - shown_at     — user has opened /new and seen the film in the rendered top-10
#                    (hides the 🆕 badge on subsequent /new opens)
#
# These are independent — a push lands in 'notified_at' WITHOUT touching 'shown_at',
# so the user still sees the 🆕 badge when they click "Открыть /new" and can
# visually locate the film they just got the push for.

def _card_identifiers(card: dict) -> list[str]:
    """Return all stable IDs for a card.

    A card's key flips between refreshes when KP enrichment status changes:
        - kp resolved later → key = "kp:N"
        - kp cache miss → key = movie_key(title, year)
    To match a previously-stored ID through such flips we store every stable
    handle the card already carries: original ``key``, ``kp:N`` (if known),
    plus ``movie_key(title, year)`` for both title and alt_title.
    """
    ids: list[str] = []

    def _append(value: str) -> None:
        if value and value not in ids:
            ids.append(value)

    key = str(card.get("key") or "")
    if key:
        _append(key)
    kp_id = card.get("kp_id")
    if kp_id:
        _append(f"kp:{kp_id}")
    try:
        year = int(card.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    if year:
        for title_field in (card.get("title"), card.get("alt_title")):
            title = str(title_field or "")
            if not title:
                continue
            try:
                _append(_movie_card_key(title, year))
            except (TypeError, ValueError):
                pass
    return ids


def _get_user_entries_from_settings(settings: dict, chat_id: int) -> dict:
    if not chat_id:
        return {}
    seen_by_user = settings.get("movie_seen_by_user") or {}
    user_entry = seen_by_user.get(str(chat_id)) if isinstance(seen_by_user, dict) else {}
    return user_entry if isinstance(user_entry, dict) else {}


def _entry_is_notified(entry) -> bool:
    """True iff a push was sent for this film to this user.

    Legacy entries (plain timestamp strings from the previous single-signal scheme)
    are treated as fully notified to avoid a wave of duplicate push at the
    migration point.
    """
    if isinstance(entry, str):
        return bool(entry)
    if isinstance(entry, dict):
        return bool(entry.get("notified_at"))
    return False


def _entry_is_shown_in_new(entry) -> bool:
    """True iff the user has opened /new and seen this film in the list.

    Legacy entries are treated as fully shown — the 🆕 badge won't appear
    on films the old single-signal scheme already considered 'seen'.
    """
    if isinstance(entry, str):
        return bool(entry)
    if isinstance(entry, dict):
        return bool(entry.get("shown_at"))
    return False


def _entry_is_handled_in_new(entry) -> bool:
    """True iff the user already acted on this film from /new."""
    if isinstance(entry, dict):
        return bool(entry.get("handled_at"))
    return False


def _get_user_entries(chat_id: int) -> dict:
    """Return the per-user dict of {film_id: entry}, where entry is either the
    new {notified_at, shown_at} dict or a legacy timestamp string."""
    return _get_user_entries_from_settings(_load_movie_discovery_settings(), chat_id)


def _card_has_signal(card: dict, entries: dict, predicate) -> bool:
    if not entries:
        return False
    for cid in _card_identifiers(card):
        if predicate(entries.get(cid)):
            return True
    return False


def _is_card_notified_in_entries(card: dict, entries: dict) -> bool:
    return _card_has_signal(card, entries, _entry_is_notified)


def _is_card_shown_in_new_in_entries(card: dict, entries: dict) -> bool:
    return _card_has_signal(card, entries, _entry_is_shown_in_new)


def _is_card_handled_in_new_in_entries(card: dict, entries: dict) -> bool:
    return _card_has_signal(card, entries, _entry_is_handled_in_new)


def _is_card_notified(card: dict, chat_id: int) -> bool:
    """True iff any of the card's identifiers has a notified_at in the user's dict.

    Used to skip duplicate push.
    """
    return _is_card_notified_in_entries(card, _get_user_entries(chat_id))


def _is_card_shown_in_new(card: dict, chat_id: int) -> bool:
    """True iff any of the card's identifiers has a shown_at in the user's dict.

    Used to hide the 🆕 badge in /new for already-shown films.
    """
    return _is_card_shown_in_new_in_entries(card, _get_user_entries(chat_id))


def _is_card_handled_in_new(card: dict, chat_id: int) -> bool:
    return _is_card_handled_in_new_in_entries(card, _get_user_entries(chat_id))


def _mark_user_signal(chat_id: int, cards: list[dict], *, signal: str) -> None:
    """Internal: set a per-user /new signal timestamp for each card identifier.
    Other fields of the entry are preserved.

    Legacy string entries are upgraded to the new dict format in place.
    Saves only when something actually changed (idempotent within the same minute).
    """
    assert signal in ("notified_at", "shown_at", "handled_at")
    if not chat_id or not cards:
        return
    now_text = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    settings = _load_movie_discovery_settings()
    seen_by_user = settings.get("movie_seen_by_user")
    if not isinstance(seen_by_user, dict):
        seen_by_user = {}
    user_key = str(chat_id)
    user_entry = seen_by_user.get(user_key)
    if not isinstance(user_entry, dict):
        user_entry = {}

    changed = False
    for card in cards:
        for cid in _card_identifiers(card):
            existing = user_entry.get(cid)
            if isinstance(existing, str):
                # Legacy upgrade: an old single-timestamp entry counts as both
                # notified and shown. Promote to dict and overwrite the target signal.
                existing = {"notified_at": existing, "shown_at": existing}
            elif not isinstance(existing, dict):
                existing = {}
            if existing.get(signal) == now_text:
                # No actual change for this signal this minute — skip.
                # But ensure dict form is persisted if existing was non-dict.
                if user_entry.get(cid) is not existing:
                    user_entry[cid] = existing
                    changed = True
                continue
            existing[signal] = now_text
            user_entry[cid] = existing
            changed = True

    if not changed:
        return
    seen_by_user[user_key] = user_entry
    settings["movie_seen_by_user"] = seen_by_user
    _save_movie_discovery_settings(settings)


def _mark_user_notified(chat_id: int, cards: list[dict]) -> None:
    """Set ``notified_at`` for each card's identifiers. Preserves ``shown_at``.
    Called after a successful push so the same film isn't pushed twice."""
    _mark_user_signal(chat_id, cards, signal="notified_at")


def _mark_user_shown_in_new(chat_id: int, cards: list[dict]) -> None:
    """Set ``shown_at`` for each card's identifiers. Preserves ``notified_at``.
    Called after rendering /new so the 🆕 badge disappears next time."""
    _mark_user_signal(chat_id, cards, signal="shown_at")


def _mark_user_handled_in_new(chat_id: int, cards: list[dict]) -> None:
    """Set ``handled_at`` for films successfully added from /new notification."""
    _mark_user_signal(chat_id, cards, signal="handled_at")


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


def _clear_movie_subscription_failures(chat_id: int) -> None:
    settings = _load_movie_discovery_settings()
    subs = settings.get("movie_subscriptions")
    if not isinstance(subs, dict):
        return
    entry = subs.get(str(chat_id))
    if not isinstance(entry, dict):
        return
    changed = False
    for key in ("failures", "last_failure_at", "last_failure_label"):
        if key in entry:
            entry.pop(key, None)
            changed = True
    if changed:
        _save_movie_discovery_settings(settings)


def _record_movie_subscription_failure(chat_id: int, label: str) -> tuple[int, bool]:
    """Record a permanent /new push failure.

    Returns ``(failure_count, unsubscribed)``. At the cap the subscription is
    removed so a dead chat does not get retried forever.
    """
    settings = _load_movie_discovery_settings()
    subs = settings.get("movie_subscriptions")
    if not isinstance(subs, dict):
        return 0, False
    key = str(chat_id)
    entry = subs.get(key)
    if not isinstance(entry, dict):
        entry = {}
    try:
        failure_count = int(entry.get("failures") or 0) + 1
    except (TypeError, ValueError):
        failure_count = 1
    if failure_count >= _MOVIE_NOTIFICATION_MAX_FAILURES:
        subs.pop(key, None)
        _save_movie_discovery_settings(settings)
        return failure_count, True
    entry["failures"] = failure_count
    entry["last_failure_at"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    entry["last_failure_label"] = label
    subs[key] = entry
    _save_movie_discovery_settings(settings)
    return failure_count, False


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


def _movie_discovery_card_token(card: dict) -> str:
    """Short stable token for a /new card callback.

    Telegram callback_data is limited, so the button carries index + token.
    The token lets us recover the same card if the cache order changes before
    the user taps the button.
    """
    raw = str(card.get("key") or "")
    if not raw and card.get("kp_id"):
        raw = f"kp:{card.get('kp_id')}"
    if not raw:
        raw = "|".join(
            str(card.get(key) or "")
            for key in ("title", "alt_title", "year")
        )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _find_movie_discovery_card(cards: list[dict], index: int, token: str = "") -> dict | None:
    if token:
        if 0 <= index < len(cards) and _movie_discovery_card_token(cards[index]) == token:
            return cards[index]
        for card in cards:
            if _movie_discovery_card_token(card) == token:
                return card
        return None
    if 0 <= index < len(cards):
        return cards[index]
    return None


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
            callback_data=f"new:show:{index - 1}:{_movie_discovery_card_token(card)}",
        )])
    is_subscribed = chat_id is not None and _is_movie_subscribed(chat_id)
    sub_label = "🔕 Отписаться от /new" if is_subscribed else "🔔 Подписаться на /new"
    sub_cb = "new:unsubscribe" if is_subscribed else "new:subscribe"
    rows.append([InlineKeyboardButton(sub_label, callback_data=sub_cb)])
    rows.append([
        InlineKeyboardButton(BUTTON_REFRESH, callback_data="new:refresh"),
        InlineKeyboardButton(BUTTON_CLOSE, callback_data="new:close"),
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


def _movie_countries_text(card: dict) -> str:
    countries = card.get("countries")
    if not isinstance(countries, list):
        return ""
    cleaned = [str(country).strip() for country in countries if str(country).strip()]
    return ", ".join(cleaned[:2])


def _format_movie_discovery_cache(cache: dict, chat_id: int | None = None) -> str:
    """Render the /new top-10 list.

    When ``chat_id`` is supplied, the «🆕» badge appears only on films that this
    specific user hasn't opened in /new yet (``shown_at`` is empty). Note: a push
    alone does NOT hide the badge — the user still needs to open /new to clear
    it, so they can visually locate the film they just got a push for. When
    ``chat_id`` is None (legacy or system callers without a user context) the
    badge is omitted.
    """
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
        lines.append(
            "\nПока нет подходящих новинок.\n\n"
            "Бот ищет фильмы и мультфильмы по фильтрам выше, сверяет рейтинг КП "
            "и доступные раздачи.\n\n"
            "Почему может быть пусто: источники временно ничего не отдали, "
            "список ещё прогревается или сейчас нет релизов, которые проходят фильтры.\n\n"
            "Что можно сделать: нажать «Обновить» или попробовать позже."
        )
        return "\n".join(lines)

    for index, card in enumerate(cards[:10], 1):
        main_title = html_module.escape(str(card.get("title") or "Без названия"))
        alt_title = html_module.escape(str(card.get("alt_title") or ""))
        title = f"{main_title} / {alt_title}" if alt_title else main_title
        year = html_module.escape(str(card.get("year") or ""))
        rating = card.get("rating")
        votes_fmt = _format_kp_votes(card.get("kp_votes"))
        votes_text = f" ({votes_fmt})" if votes_fmt else ""
        meta_parts = [year] if year else []
        countries_text = _movie_countries_text(card)
        if countries_text:
            meta_parts.append(html_module.escape(countries_text))
        if isinstance(rating, (int, float)):
            meta_parts.append(f"КП {rating:.1f}{votes_text}")
        meta_text = " · ".join(meta_parts)
        genres = ", ".join(card.get("genres") or [])
        genres_text = f"\n   Жанры: {html_module.escape(genres)}" if genres else ""
        kp_url = card.get("kp_url")
        kp_text = f"\n   <a href=\"{html_module.escape(str(kp_url))}\">Кинопоиск</a>" if kp_url else ""
        # Per-user badge: shown only when the user hasn't opened /new and seen
        # this film yet. A push alone does NOT clear the badge — the user must
        # actually open /new to confirm they've seen it.
        new_mark = " 🆕" if (
            chat_id
            and not _is_card_shown_in_new(card, chat_id)
            and not _is_card_handled_in_new(card, chat_id)
        ) else ""
        if card.get("in_plex"):
            plex_res = card.get("plex_resolution") or ""
            plex_mark = f" ✅ {html_module.escape(plex_res)}" if plex_res else " ✅"
        else:
            plex_mark = ""
        tracker_labels = _movie_card_tracker_labels(card)
        tracker_text = f" · {html_module.escape(tracker_labels)}" if tracker_labels else ""
        # PR2: GPT-generated 1-line «why this film» explanation, shown only
        # for top-10 cards that have a cached explanation. Italic + 💭 icon
        # to distinguish from objective metadata above.
        explanation_text = ""
        explanation = card.get("explanation")
        if explanation:
            explanation_text = f"\n   💭 <i>{html_module.escape(str(explanation))}</i>"
        lines.append(
            f"\n{index}. <b>{title}</b>{plex_mark}{new_mark}\n"
            f"   {meta_text}\n"
            f"   Лучшее: {html_module.escape(str(card.get('best_quality') or '?'))}, "
            f"{html_module.escape(str(card.get('best_size') or '?'))}, "
            f"сидов {html_module.escape(str(card.get('best_seeders') or 0))}\n"
            f"   Раздач: {html_module.escape(str(card.get('release_count') or len(card.get('releases') or [])))}{tracker_text}"
            f"{explanation_text}{genres_text}{kp_text}"
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


def _supplement_releases_for_failed_queries(
    *,
    failed_specs: list[tuple[int | None, str]],
    releases_out: list[dict],
    prev_all_releases: list,
) -> int:
    """Copy releases from `prev_all_releases` into `releases_out` (in place)
    for every (year, quality) spec that fetched zero results due to errors.

    Superseded in normal flow by _merge_releases_with_previous (Strategy 3
    always-merges). Kept around so tests and any future targeted-supplement
    callers still work — by-and-large redundant when always-merge is on.

    Returns the count of supplemented releases.
    """
    if not failed_specs or not isinstance(prev_all_releases, list):
        return 0
    failed_set = set(failed_specs)
    supplemented = 0
    for prev_rel in prev_all_releases:
        if not isinstance(prev_rel, dict):
            continue
        rel_year = prev_rel.get("year")
        rel_quality = (prev_rel.get("quality") or "").lower()
        if (rel_year, rel_quality) in failed_set:
            releases_out.append(prev_rel)
            supplemented += 1
    return supplemented


async def _refresh_movie_discovery_cache(
    max_stale_kp_refresh: int | None = _KP_MAX_STALE_REFRESH,
    *,
    force_refresh: bool = False,
) -> dict:
    global _movie_discovery_refresh_lock, _movie_discovery_refresh_current_mode
    global _movie_discovery_refresh_last_mode, _movie_discovery_refresh_last_finished_at

    mode = _movie_discovery_refresh_mode(max_stale_kp_refresh)
    if _movie_discovery_refresh_lock is None:
        _movie_discovery_refresh_lock = asyncio.Lock()

    async with _movie_discovery_refresh_lock:
        now_monotonic = time.monotonic()
        can_reuse = (
            not force_refresh
            and _movie_discovery_refresh_last_finished_at
            and now_monotonic - _movie_discovery_refresh_last_finished_at < _MOVIE_DISCOVERY_REFRESH_COALESCE_SECONDS
            and _movie_discovery_refresh_covers(_movie_discovery_refresh_last_mode, mode)
        )
        if can_reuse:
            logger.info(
                "movie_discovery: refresh coalesced requested_mode=%s completed_mode=%s",
                mode,
                _movie_discovery_refresh_last_mode,
            )
            return _load_movie_discovery_cache()

        _movie_discovery_refresh_current_mode = mode
        try:
            cache = await _refresh_movie_discovery_cache_inner(max_stale_kp_refresh=max_stale_kp_refresh)
        finally:
            _movie_discovery_refresh_current_mode = ""
        _movie_discovery_refresh_last_mode = mode
        _movie_discovery_refresh_last_finished_at = time.monotonic()
        return cache


def _movie_discovery_refresh_mode(max_stale_kp_refresh: int | None) -> str:
    return "kp_full" if max_stale_kp_refresh is None else "normal"


def _movie_discovery_refresh_covers(completed_mode: str, requested_mode: str) -> bool:
    return completed_mode == requested_mode or (completed_mode == "kp_full" and requested_mode == "normal")


def _movie_discovery_refresh_busy_mode() -> str:
    lock = _movie_discovery_refresh_lock
    if lock is not None and lock.locked():
        return _movie_discovery_refresh_current_mode or "normal"
    return ""


def _movie_discovery_refresh_wait_text(mode: str) -> str:
    if mode == "kp_full":
        return (
            "🎬 Идёт глубокое обновление новинок\n\n"
            "Проверяю трекеры, рейтинг и Plex-метки. Это может занять пару минут.\n"
            "Дождусь текущего обновления и покажу свежий список."
        )
    return (
        "🎬 Новинки уже обновляются\n\n"
        "Проверяю трекеры, рейтинг и Plex-метки. Это может занять пару минут.\n"
        "Дождусь текущего обновления и покажу свежий список."
    )


def _movie_discovery_refresh_start_text() -> str:
    return (
        "🎬 Обновляю новинки\n\n"
        "Проверяю трекеры, рейтинг и Plex-метки.\n"
        "Это может занять пару минут."
    )


def _movie_discovery_admin_refresh_wait_note(refresh_kind: str) -> str:
    if refresh_kind == "full":
        return "🔄 Идёт другое обновление. Полное обновление KP-кэша запустится сразу после него."
    return "🔄 Идёт другое обновление. Постепенное обновление KP-кэша запустится сразу после него."


def _movie_discovery_cache_has_gating_degradation(cache: dict) -> bool:
    specs = cache.get("last_failed_specs")
    enabled = cache.get("last_failed_indexer_ids")
    return bool(specs if isinstance(specs, list) else []) or bool(
        enabled if isinstance(enabled, list) else []
    )


def _movie_discovery_should_keep_previous_cache(
    *,
    degraded_for_rating: bool,
    previous_cards_count: int,
    new_cards_count: int,
) -> bool:
    min_stable_cards = min(_MOVIE_DISCOVERY_MIN_STABLE_CARDS, MOVIE_DISCOVERY_LIMIT)
    return (
        degraded_for_rating
        and previous_cards_count > new_cards_count
        and new_cards_count < min_stable_cards
    )


def _movie_discovery_guard_degraded_cache(
    cache: dict,
    previous: dict,
    *,
    failed_specs: list[list],
    failed_enabled: list[str],
    failed_disabled: list[str],
    prev_top10_kp_ids: list,
) -> tuple[dict, bool]:
    cache["last_failed_specs"] = failed_specs
    cache["last_failed_indexer_ids"] = failed_enabled
    cache["last_failed_indexer_ids_disabled"] = failed_disabled
    cache["prev_top10_kp_ids"] = prev_top10_kp_ids

    previous_cards_count = len(previous.get("cards") or [])
    new_cards_count = len(cache.get("cards") or [])
    degraded_for_rating = bool(failed_specs or failed_enabled)
    keep_previous = _movie_discovery_should_keep_previous_cache(
        degraded_for_rating=degraded_for_rating,
        previous_cards_count=previous_cards_count,
        new_cards_count=new_cards_count,
    )
    if not keep_previous:
        if degraded_for_rating:
            cache["last_degraded_refresh"] = {
                "rejected": False,
                "prev_cards": previous_cards_count,
                "new_cards": new_cards_count,
            }
        else:
            cache.pop("last_degraded_refresh", None)
        return cache, False

    preserved = dict(previous)
    preserved["last_failed_specs"] = failed_specs
    preserved["last_failed_indexer_ids"] = failed_enabled
    preserved["last_failed_indexer_ids_disabled"] = failed_disabled
    preserved["prev_top10_kp_ids"] = prev_top10_kp_ids
    for carried_key in ("kp_api_stats", "kp_cache"):
        if carried_key in cache:
            preserved[carried_key] = cache[carried_key]
    preserved["last_degraded_refresh"] = {
        "rejected": True,
        "prev_cards": previous_cards_count,
        "new_cards": new_cards_count,
    }
    return preserved, True


async def _refresh_movie_discovery_cache_inner(max_stale_kp_refresh: int | None = _KP_MAX_STALE_REFRESH) -> dict:
    now = datetime.now(DISPLAY_TIMEZONE)
    now_text = now.strftime("%Y-%m-%d %H:%M")

    # Diagnostic: snapshot pre-refresh state for the cold-start /new bug investigation.
    # See CLAUDE.md → "Movie discovery" markers for what this lets us reconstruct.
    _prev_for_log = _load_movie_discovery_cache()
    _prev_cards_for_log = _prev_for_log.get("cards") or []
    # Capture previous top-10 kp_ids for the consensus check in notifications
    # (C-lite: a film is pushed only when it appears in two consecutive top-10s).
    _prev_top10_kp_ids = [
        c.get("kp_id") for c in _prev_cards_for_log[:10] if c.get("kp_id")
    ]
    _rutracker_paused = False
    try:
        if rutracker_client is not None and hasattr(rutracker_client, "_cooldown_until"):
            cd = getattr(rutracker_client, "_cooldown_until", None)
            _rutracker_paused = bool(cd and cd > now.timestamp())
    except Exception:
        pass
    logger.info(
        "movie_discovery: refresh started prev_cards=%d rutracker_paused=%s jackett=%s",
        len(_prev_cards_for_log),
        _rutracker_paused,
        jackett_client is not None,
    )

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

    # Track which (year, quality) query specs ended up with errors AND no
    # results. For those, we'll supplement from the previous cache after the
    # loop instead of letting a partial fetch silently overwrite the good
    # state (the «2026 1080p timed out, 2025 1080p worked → /new becomes
    # all-2025» bug). One key per query string so we can also handle the
    # multi-quality case (1080p+2160p).
    failed_query_specs: list[tuple[int | None, str]] = []
    # Per-indexer failure tracking via Jackett's `Indexers` field (Status=1 or
    # Results=0+Error). Union across all queries — if any query saw an indexer
    # fail, we'll supplement its prev releases. This catches the case where
    # Jackett OVERALL responded fine but a specific indexer was silently
    # broken or returned a degraded snapshot — exactly the «cards=23,
    # added=12, removed=19» churn observed in production.
    failed_indexer_ids: set[str] = set()

    for search_query in queries:
        # Parse year and quality from query string «YYYY <quality>».
        parts = search_query.split()
        try:
            query_year: int | None = int(parts[0])
        except (ValueError, IndexError):
            query_year = None
        query_quality = parts[1].lower() if len(parts) > 1 else ""

        query_had_error = False
        query_had_results = False

        if jackett_client is not None:
            try:
                results = await asyncio.to_thread(
                    jackett_client.search,
                    search_query,
                    fetch_limit=JACKETT_FETCH_LIMIT,
                    categories="2000",
                )
                source_counts["jackett_raw"] += len(results)
                # Surface per-indexer statuses from this Jackett response.
                # Each entry tells us whether a specific indexer succeeded,
                # failed, or returned 0 results. We aggregate failures across
                # all queries so the post-loop supplement step can fill in
                # just the missing trackers from prev cache.
                try:
                    statuses = jackett_client.get_last_indexer_statuses()
                except Exception:
                    statuses = []
                for st in statuses:
                    if st.is_ok:
                        # Healthy or status>0 with Results>0 (Torznab warning,
                        # data still came back). Log at debug level — no action.
                        continue
                    failed_indexer_ids.add(st.indexer_id)
                    logger.warning(
                        "movie_discovery: Jackett indexer %r failed for %r "
                        "(status=%d results=%d error=%r) — will supplement from prev",
                        st.indexer_id, search_query, st.status, st.results, st.error[:120],
                    )
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
                        query_had_results = True
            except JackettError:
                logger.warning("Movie discovery Jackett search failed: %s", search_query, exc_info=True)
                reason_counts["jackett:error"] += 1
                query_had_error = True

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
                        query_had_results = True
            except RutrackerError:
                logger.warning("Movie discovery Rutracker search failed: %s", search_query, exc_info=True)
                reason_counts["rutracker:error"] += 1
                query_had_error = True

        # If a query had ANY upstream error AND 0 accepted results, that's
        # a SUSPICIOUS empty (probably the «year ABC» fetch was broken, not
        # genuinely empty). Distinguish from clean-zero (no errors, just
        # nothing to find) which we leave alone.
        if query_had_error and not query_had_results and query_year is not None:
            failed_query_specs.append((query_year, query_quality))
            logger.warning(
                "movie_discovery: query %r had errors and 0 accepted results — "
                "will supplement from prev cache to avoid losing year=%s/quality=%s",
                search_query, query_year, query_quality,
            )

    # Per-indexer aware supplement: for each Jackett indexer that this
    # refresh's response marks as failed (Status=1 / Results=0 / Error in
    # Indexers field), take the prev refresh's releases that came FROM
    # that specific tracker. We don't need to supplement for indexers
    # that responded cleanly — those replies are authoritative.
    if failed_indexer_ids or failed_query_specs:
        prev_for_supplement = _load_movie_discovery_cache()
        prev_all = prev_for_supplement.get("all_releases") or []
        supplemented = 0
        if failed_indexer_ids and isinstance(prev_all, list):
            # Match prev releases by tracker field — those came from indexers
            # that just failed in the current refresh.
            failed_indexer_set = set(failed_indexer_ids)
            for prev_rel in prev_all:
                if not isinstance(prev_rel, dict):
                    continue
                tracker = (prev_rel.get("tracker") or "").lower()
                if tracker in failed_indexer_set:
                    releases.append(prev_rel)
                    supplemented += 1
        # Backwards-compat: also supplement for (year, quality) specs where
        # NO source returned anything at all (covers Jackett-process-down case
        # where we don't have per-indexer info — supplement everything for
        # those year-quality combos).
        if failed_query_specs:
            supplemented += _supplement_releases_for_failed_queries(
                failed_specs=failed_query_specs,
                releases_out=releases,
                prev_all_releases=prev_all,
            )
        logger.info(
            "movie_discovery: supplemented %d releases from prev cache "
            "(failed_indexers=%s failed_specs=%s)",
            supplemented, failed_indexer_ids or "none",
            failed_query_specs or "none",
        )

    logger.info(
        "movie_discovery: sources fetched jackett_raw=%d rutracker_raw=%d accepted=%d errors=jackett:%d,rutracker:%d",
        source_counts.get("jackett_raw", 0),
        source_counts.get("rutracker_raw", 0),
        len(releases),
        reason_counts.get("jackett:error", 0),
        reason_counts.get("rutracker:error", 0),
    )

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
        kp_match_validator=_gpt_validate_kp_match,
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

    # Diagnostic: log cards diff vs previous cache. This is the smoking-gun line
    # for the cold-start /new bug — shows which kp_ids appeared/disappeared between
    # consecutive refreshes (e.g. transient Rutracker cooldown after restart).
    _new_cards_log = cache.get("cards") or []
    _prev_ids_log = {c.get("kp_id") for c in _prev_cards_for_log if c.get("kp_id")}
    _new_ids_log = {c.get("kp_id") for c in _new_cards_log if c.get("kp_id")}
    _added_log = _new_ids_log - _prev_ids_log
    _removed_log = _prev_ids_log - _new_ids_log
    logger.info(
        "movie_discovery: cache built cards=%d prev_cards=%d added=%d removed=%d top10=[%s]",
        len(_new_cards_log),
        len(_prev_cards_for_log),
        len(_added_log),
        len(_removed_log),
        ", ".join(
            f"{(c.get('title') or '?')!s}={c.get('kp_id') or '-'}"
            for c in _new_cards_log[:10]
        ),
    )
    if _added_log or _removed_log:
        logger.info(
            "movie_discovery: cards diff added_kp=%s removed_kp=%s",
            ",".join(map(str, list(_added_log)[:20])) or "-",
            ",".join(map(str, list(_removed_log)[:20])) or "-",
        )

    # Persist failure signals so the discovery loop can detect a partial
    # outage and schedule an opportunistic retry sooner than the normal 12h
    # interval. Three signals split by user intent:
    #   - last_failed_specs: queries where NO source returned anything (whole-
    #     query outage, e.g. Jackett process down).
    #   - last_failed_indexer_ids_enabled: indexers the USER wants in /new
    #     rating (via /admin → 🎬 Трекеры новинок) that failed. These ARE
    #     degradation — we should retry to recover them, and they block the
    #     «ready» notification until backoff exhausts.
    #   - last_failed_indexer_ids_disabled: indexers user doesn't include in
    #     /new rating but Jackett still queries (different layer of config).
    #     Logged for visibility, but NOT degradation — user already opted out.
    # Partition is based on jackett_trackers_enabled. None means «all enabled».
    enabled_for_rating = (
        set(md_settings["jackett_trackers_enabled"])
        if md_settings.get("jackett_trackers_enabled") is not None
        else None
    )
    if enabled_for_rating is None:
        failed_enabled = set(failed_indexer_ids)
        failed_disabled: set[str] = set()
    else:
        failed_enabled = failed_indexer_ids & enabled_for_rating
        failed_disabled = failed_indexer_ids - enabled_for_rating
    failed_specs_payload = [[year, quality] for (year, quality) in failed_query_specs]
    failed_enabled_payload = sorted(failed_enabled)  # gating signal
    failed_disabled_payload = sorted(failed_disabled)  # info-only
    if failed_disabled:
        logger.info(
            "movie_discovery: failed indexers not in rating (info-only, no retry): %s",
            sorted(failed_disabled),
        )

    # Persist previous top-10 alongside new cards. The consensus filter in
    # _run_movie_discovery_notifications reads this to decide whether a kp_id
    # in the new top-10 is "confirmed" (was also in the previous top-10) or
    # "transient" (appeared just now — wait for next cycle to validate).
    cache, degraded_cache_rejected = _movie_discovery_guard_degraded_cache(
        cache,
        previous,
        failed_specs=failed_specs_payload,
        failed_enabled=failed_enabled_payload,
        failed_disabled=failed_disabled_payload,
        prev_top10_kp_ids=_prev_top10_kp_ids,
    )
    if degraded_cache_rejected:
        degraded_info = cache.get("last_degraded_refresh") or {}
        logger.warning(
            "movie_discovery: degraded cache rejected, keeping previous cache "
            "prev_cards=%d new_cards=%d failed_specs=%s failed_enabled=%s",
            degraded_info.get("prev_cards", 0),
            degraded_info.get("new_cards", 0),
            failed_specs_payload or "none",
            failed_enabled_payload or "none",
        )

    # PR2: enrich top-10 cards with GPT explanations + KP synopsis.
    # Runs synchronously but each call is bounded (~2-3 sec) and cached
    # forever in kp_cache, so steady-state cost is near-zero.
    if GPT_ENABLED:
        await _enrich_top10_with_explanations(cache)

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
        "degraded_cache_rejected": degraded_cache_rejected,
        "last_degraded_refresh": cache.get("last_degraded_refresh"),
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


_MOVIE_NOTIFICATION_ACTION_LIMIT = 3
_MOVIE_NOTIFICATION_SNAPSHOT_TTL_HOURS = 72
_MOVIE_NOTIFICATION_SNAPSHOT_MAX = 100
_MOVIE_NOTIFICATION_GPT_TIE_DELTA = 180.0
_MOVIE_NOTIFICATION_MAX_FAILURES = MAX_TASK_NOTIFICATION_FAILURES


def _movie_notification_target_quality(defaults: dict) -> str:
    quality = str(defaults.get("quality") or "1080p")
    return {"4K": "2160p", "1080p": "1080p", "720p": "720p"}.get(quality, "")


def _movie_release_quality_label(result: dict) -> str:
    return str(result.get("quality") or _movie_detect_quality(str(result.get("title") or "")) or "")


def _movie_notification_release_score(result: dict, defaults: dict) -> float:
    """Soft-rank a release for one-tap /new downloads using user defaults."""
    title = str(result.get("title") or "")
    score = _score_result(result)
    target_quality = _movie_notification_target_quality(defaults)
    if target_quality and _movie_release_quality_label(result) == target_quality:
        score += 350
    if defaults.get("audio") and _detect_has_original_audio(title):
        score += 180
    if defaults.get("subs") and _detect_has_subs(title):
        score += 120
    voices = _normalise_preferred_voices(defaults.get("preferred_voices"))
    if voices and _result_has_preferred_voice(result, voices):
        score += 250
    return score


def _movie_notification_gpt_candidate_payload(result: dict, score: float) -> dict:
    title = str(result.get("title") or "")
    return {
        "title": title,
        "score": score,
        "quality": _movie_release_quality_label(result),
        "has_original": _detect_has_original_audio(title),
        "has_subs": _detect_has_subs(title),
        "tracker": str(result.get("tracker_name") or result.get("source") or ""),
        "size": str(result.get("size") or ""),
        "seeders": result.get("seeders"),
    }


async def _gpt_choose_movie_notification_release(
    card: dict,
    defaults: dict,
    scored_releases: list[tuple[float, dict]],
) -> tuple[dict | None, str]:
    if not GPT_ENABLED or len(scored_releases) < 2:
        return None, ""
    top_score = scored_releases[0][0]
    if top_score - scored_releases[1][0] > _MOVIE_NOTIFICATION_GPT_TIE_DELTA:
        return None, ""
    close = [
        (score, result)
        for score, result in scored_releases[:3]
        if top_score - score <= _MOVIE_NOTIFICATION_GPT_TIE_DELTA
    ]
    if len(close) < 2:
        return None, ""

    sink: list = []
    try:
        pick_idx, reason, error = await asyncio.to_thread(
            gpt_choose_movie_notification_release,
            title=str(card.get("title") or ""),
            year=card.get("year") if isinstance(card.get("year"), int) else None,
            defaults=defaults,
            candidates=[
                _movie_notification_gpt_candidate_payload(result, score)
                for score, result in close
            ],
            api_key=OPENAI_API_KEY,
            model=GPT_MODEL,
            usage_sink=sink,
        )
    except Exception:
        logger.warning("movie notification GPT tie-break failed", exc_info=True)
        return None, ""
    _gpt_record_usage(
        feature="movie_notification_release",
        input_tokens=320,
        output_tokens=90,
        error_label=error,
        usage=(sink[0] if sink else None),
    )
    if pick_idx is None or not (0 <= pick_idx < len(close)):
        return None, ""
    return close[pick_idx][1], reason


def _movie_notification_preference_notes(
    selected: dict,
    releases: list[dict],
    defaults: dict,
) -> list[str]:
    notes: list[str] = []
    target_quality = _movie_notification_target_quality(defaults)
    if target_quality and _movie_release_quality_label(selected) != target_quality:
        if not any(_movie_release_quality_label(r) == target_quality for r in releases):
            notes.append(f"{target_quality} не нашёл, выбрал лучшее доступное")
        else:
            notes.append(f"выбрал не {target_quality}: другой вариант заметно сильнее")

    title = str(selected.get("title") or "")
    if defaults.get("audio") and not _detect_has_original_audio(title):
        if not any(_detect_has_original_audio(str(r.get("title") or "")) for r in releases):
            notes.append("Original не нашёл")
    if defaults.get("subs") and not _detect_has_subs(title):
        if not any(_detect_has_subs(str(r.get("title") or "")) for r in releases):
            notes.append("субтитры не нашёл")
    voices = _normalise_preferred_voices(defaults.get("preferred_voices"))
    if voices and not _result_has_preferred_voice(selected, voices):
        if not any(_result_has_preferred_voice(r, voices) for r in releases):
            notes.append(f"перевод {' / '.join(voices)} не нашёл")
    return notes


def _movie_notification_pick_release(card: dict, chat_id: int | None) -> tuple[dict | None, list[str]]:
    releases = [
        _movie_release_to_search_result(release)
        for release in (card.get("releases") or [])
        if isinstance(release, dict)
    ]
    if not releases:
        return None, []
    defaults = _search_defaults_for_chat(chat_id)
    releases.sort(
        key=lambda r: _movie_notification_release_score(r, defaults),
        reverse=True,
    )
    selected = releases[0]
    return selected, _movie_notification_preference_notes(selected, releases, defaults)


async def _movie_notification_pick_release_for_snapshot(
    card: dict,
    chat_id: int | None,
) -> tuple[dict | None, list[str]]:
    releases = [
        _movie_release_to_search_result(release)
        for release in (card.get("releases") or [])
        if isinstance(release, dict)
    ]
    if not releases:
        return None, []
    defaults = _search_defaults_for_chat(chat_id)
    scored = sorted(
        (
            (_movie_notification_release_score(result, defaults), result)
            for result in releases
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    selected = scored[0][1]
    gpt_selected, gpt_reason = await _gpt_choose_movie_notification_release(
        card, defaults, scored,
    )
    if gpt_selected is not None:
        selected = gpt_selected
    notes = _movie_notification_preference_notes(selected, releases, defaults)
    if gpt_selected is not None and gpt_reason:
        notes.append(f"GPT выбрал: {gpt_reason}")
    return selected, notes


def _movie_notification_snapshot_card(card: dict) -> dict:
    keep = (
        "key", "title", "alt_title", "year", "kp_id", "kp_url", "rating",
        "kp_votes", "genres", "countries", "poster_url", "poster_preview_url",
        "best_quality", "best_size", "best_seeders", "release_count",
        "in_plex", "plex_resolution",
    )
    snap = {key: card.get(key) for key in keep if key in card}
    snap["releases"] = [
        dict(release)
        for release in (card.get("releases") or [])[:8]
        if isinstance(release, dict)
    ]
    return snap


def _movie_notification_snapshot_items(cards: list[dict], chat_id: int | None) -> list[dict]:
    items: list[dict] = []
    for card in cards[:_MOVIE_NOTIFICATION_ACTION_LIMIT]:
        if not isinstance(card, dict):
            continue
        selected, notes = _movie_notification_pick_release(card, chat_id)
        items.append({
            "card": _movie_notification_snapshot_card(card),
            "result": selected,
            "notes": notes,
        })
    return items


async def _movie_notification_snapshot_items_with_gpt(
    cards: list[dict],
    chat_id: int | None,
) -> list[dict]:
    items: list[dict] = []
    for card in cards[:_MOVIE_NOTIFICATION_ACTION_LIMIT]:
        if not isinstance(card, dict):
            continue
        selected, notes = await _movie_notification_pick_release_for_snapshot(card, chat_id)
        items.append({
            "card": _movie_notification_snapshot_card(card),
            "result": selected,
            "notes": notes,
        })
    return items


def _prune_movie_notification_snapshots(snapshots: dict, *, now: datetime) -> dict:
    if not isinstance(snapshots, dict):
        return {}
    cutoff = now - timedelta(hours=_MOVIE_NOTIFICATION_SNAPSHOT_TTL_HOURS)
    kept: dict[str, dict] = {}
    for push_id, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        try:
            created_at = datetime.fromisoformat(str(snapshot.get("created_at") or ""))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=DISPLAY_TIMEZONE)
        except ValueError:
            continue
        if created_at >= cutoff:
            kept[str(push_id)] = snapshot
    if len(kept) > _MOVIE_NOTIFICATION_SNAPSHOT_MAX:
        ordered = sorted(
            kept.items(),
            key=lambda item: str(item[1].get("created_at") or ""),
        )
        kept = dict(ordered[-_MOVIE_NOTIFICATION_SNAPSHOT_MAX:])
    return kept


def _save_movie_notification_snapshot(
    chat_id: int,
    cards: list[dict],
    *,
    items: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    settings = _load_movie_discovery_settings()
    now = datetime.now(DISPLAY_TIMEZONE)
    snapshots = _prune_movie_notification_snapshots(
        settings.get("movie_notification_snapshots") or {},
        now=now,
    )
    push_id = uuid.uuid4().hex[:10]
    if items is None:
        items = _movie_notification_snapshot_items(cards, chat_id)
    snapshots[push_id] = {
        "chat_id": str(chat_id),
        "created_at": now.isoformat(timespec="seconds"),
        "items": items,
    }
    settings["movie_notification_snapshots"] = snapshots
    _save_movie_discovery_settings(settings)
    return push_id, items


def _load_movie_notification_snapshot(push_id: str, chat_id: int | None) -> dict | None:
    settings = _load_movie_discovery_settings()
    now = datetime.now(DISPLAY_TIMEZONE)
    snapshots = _prune_movie_notification_snapshots(
        settings.get("movie_notification_snapshots") or {},
        now=now,
    )
    if snapshots != (settings.get("movie_notification_snapshots") or {}):
        settings["movie_notification_snapshots"] = snapshots
        _save_movie_discovery_settings(settings)
    snapshot = snapshots.get(str(push_id))
    if not isinstance(snapshot, dict):
        return None
    if chat_id is not None and str(snapshot.get("chat_id") or "") != str(chat_id):
        return None
    return snapshot


def _movie_notification_cards_from_items(items: list[dict]) -> list[dict]:
    return [
        item.get("card")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("card"), dict)
    ]


def _movie_notification_result_label(result: dict | None) -> str:
    if not isinstance(result, dict):
        return "раздача не выбрана"
    parts: list[str] = []
    quality = _movie_release_quality_label(result)
    if quality:
        parts.append(quality)
    size = str(result.get("size") or "").strip()
    if size:
        parts.append(size)
    seeders = result.get("seeders")
    if seeders not in (None, ""):
        parts.append(f"сидов {seeders}")
    return " · ".join(parts) or "лучшая раздача"


def _movie_notification_total_size_gb(items: list[dict]) -> float:
    total = 0.0
    for item in items:
        if not isinstance(item, dict) or item.get("skip_reason"):
            continue
        result = item.get("result")
        if isinstance(result, dict):
            total += _movie_parse_size_gb(str(result.get("size") or ""))
    return total


def _format_movie_notification_text(cards: list) -> str:
    """Build the HTML message body for a /new notification."""
    import html as _html
    lines = ["🎬 <b>Новые фильмы в /new:</b>", ""]
    for card in cards[:_MOVIE_NOTIFICATION_ACTION_LIMIT]:
        title_str = _html.escape(str(card.get("title") or ""))
        alt = card.get("alt_title") or ""
        if alt:
            title_str = f"{title_str} / {_html.escape(str(alt))}"
        kp_url = str(card.get("kp_url") or "").strip()
        if kp_url.startswith(("http://", "https://")):
            title_str = f"<a href=\"{_html.escape(kp_url, quote=True)}\">{title_str}</a>"
        year = card.get("year") or ""
        meta_parts = [str(year)] if year else []
        countries_text = _movie_countries_text(card)
        if countries_text:
            meta_parts.append(countries_text)
        meta_text = f" ({_html.escape(', '.join(meta_parts))})" if meta_parts else ""
        rating = card.get("rating")
        rating_text = f" · КП {rating:.1f}" if isinstance(rating, (int, float)) else ""
        best_parts = [
            str(value) for value in (card.get("best_quality"), card.get("best_size"))
            if value
        ]
        best_text = f" · {html_module.escape(', '.join(best_parts))}" if best_parts else ""
        lines.append(f"• {title_str}{meta_text}{rating_text}{best_text}")
    return "\n".join(lines)


def _movie_notification_downloadable_indices(items: list[dict]) -> list[int]:
    indices: list[int] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        card = item.get("card")
        result = item.get("result")
        if isinstance(card, dict) and isinstance(result, dict) and not card.get("in_plex"):
            indices.append(index)
    return indices


def _movie_notification_keyboard(
    push_id: str = "",
    count: int = 0,
    downloadable_indices: list[int] | None = None,
) -> "InlineKeyboardMarkup":
    rows: list[list[InlineKeyboardButton]] = []
    if downloadable_indices is None:
        downloadable_indices = list(range(max(0, count)))
    if push_id and downloadable_indices:
        rows.append([
            InlineKeyboardButton(f"⬇️ {index + 1}", callback_data=f"new:dl:{push_id}:{index}")
            for index in downloadable_indices
        ])
        if len(downloadable_indices) > 1:
            rows.append([
                InlineKeyboardButton(f"⬇️ Скачать все {len(downloadable_indices)}", callback_data=f"new:bulk:{push_id}")
            ])
    rows.extend([
        [
            InlineKeyboardButton("🎬 Открыть /new", callback_data="new:open"),
            InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:new_unsub"),
        ],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])
    return InlineKeyboardMarkup(rows)


def _movie_notification_poster_url(cards: list) -> str:
    """Return the best poster URL for the top /new notification card."""
    if not cards:
        return ""
    card = cards[0] if isinstance(cards[0], dict) else {}
    url = str(card.get("poster_preview_url") or card.get("poster_url") or "").strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        return ""
    return url


async def _send_movie_notification_push_to_user_result(
    cards: list, chat_id: int, app: "Application",
) -> tuple[bool, str | None, bool]:
    """Send a /new notification.

    Returns ``(sent, failure_label, is_permanent)``.
    """
    cards = [c for c in cards[:_MOVIE_NOTIFICATION_ACTION_LIMIT] if c.get("kp_id")]
    if not cards:
        return False, "no_eligible_cards", False
    _enrich_cards_with_plex(cards)
    cards = [c for c in cards if not c.get("in_plex")]
    if not cards:
        return False, "already_in_plex", False
    items = await _movie_notification_snapshot_items_with_gpt(cards, chat_id)
    push_id, items = _save_movie_notification_snapshot(chat_id, cards, items=items)
    text = _format_movie_notification_text(cards)
    keyboard = _movie_notification_keyboard(
        push_id,
        len(items),
        _movie_notification_downloadable_indices(items),
    )
    poster_url = _movie_notification_poster_url(cards)
    if poster_url:
        try:
            await app.bot.send_photo(
                chat_id=chat_id,
                photo=poster_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info("Sent /new photo notification to chat_id=%s (%d films)", chat_id, len(cards))
            return True, None, False
        except Exception as exc:
            logger.warning("Failed to send /new poster to %s, falling back to text: %s", chat_id, exc)

    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        logger.info("Sent /new notification to chat_id=%s (%d films)", chat_id, len(cards))
        return True, None, False
    except Exception as exc:
        label, is_permanent = await _classify_send_error(exc)
        logger.warning("Failed to send /new notification to %s (%s): %s", chat_id, label, exc)
        return False, label, is_permanent


async def _send_movie_notification_push_to_user(
    cards: list, chat_id: int, app: "Application",
) -> bool:
    """Send a /new notification to a specific subscriber. Returns True on success."""
    sent, _label, _is_permanent = await _send_movie_notification_push_to_user_result(cards, chat_id, app)
    return sent


async def _run_movie_discovery_notifications(
    cache: dict,
    app: "Application",
    *,
    skip_push: bool = False,
) -> None:
    """Send /new notifications to subscribers about films they haven't seen yet.

    Per-user semantics: each subscriber has their own ``movie_seen_by_user`` set.
    A film is sent in a push iff none of its identifiers are in the subscriber's
    seen-set. After a successful push the film's IDs are added to that set — so
    the same film is never sent twice to the same person.

    Quiet hours (_NOTIFY_WINDOW_START_HOUR – _NOTIFY_WINDOW_END_HOUR): outside
    the window we DON'T mark anything as seen. The diff against seen-set self-heals
    on the next in-window refresh — no separate pending queue needed.

    False-push protection (four layers, all motivated by the observed bug
    where cold-Jackett-after-restart produced a transient top-10 containing a
    film that vanished on the next stable refresh):

      A. Degraded refresh signal: if a source/query used for rating failed,
         skip push even when the cache still has cards from the previous state.

      B. ``skip_push=True`` — caller (the movie discovery loop) sets this on the
         very first refresh after startup. Cold Jackett can't be trusted; we
         still write the cache so `/new` works, but don't notify anyone.

      C. Regression guard — if removed_pct from the previous top-10 exceeds
         60%, the refresh is "unstable" (likely the same Jackett warm-up
         scenario carrying over to a non-first cycle). Skip push entirely.

      D. Eligibility + consensus filter — push only KP-enriched cards that are
         not already in Plex, and only when their kp_id appears in BOTH the
         current top-10 AND the previous top-10 (stored in
         ``cache.prev_top10_kp_ids``). A genuinely-new film waits one cycle for
         confirmation; a transient dies before reaching the user.
    """
    top_cards = (cache.get("cards") or [])[:10]
    if not top_cards:
        logger.info("movie_discovery: notify skipped — no cards in cache")
        return

    if _movie_discovery_cache_has_gating_degradation(cache):
        logger.info("movie_discovery: notify skipped — degraded refresh")
        return

    if skip_push:
        # Layer B: first refresh after startup. The cache is now updated and
        # available via /new, but we don't push — the next regular refresh
        # will reconfirm what's actually stable.
        logger.info("movie_discovery: notify skipped — first refresh after startup")
        return

    settings = _load_movie_discovery_settings()
    subs = settings.get("movie_subscriptions") or {}
    if not isinstance(subs, dict):
        subs = {}
    if not subs:
        logger.info("movie_discovery: notify skipped — no subscribers")
        return

    if not _is_in_notification_window():
        # Quiet hours — don't push and don't mark anything seen. The next in-window
        # refresh will compute the same (or larger) diff and deliver naturally.
        logger.info("movie_discovery: notify skipped — out of notification window")
        return

    # Layer C: regression guard. If most of the previous top-10 has disappeared,
    # this refresh is unstable — likely cold-Jackett aftermath, or a transient
    # outage of one of the sources. Skip the whole push cycle.
    prev_top10_kp_ids: list[int] = list(cache.get("prev_top10_kp_ids") or [])
    prev_top10_set = {kp for kp in prev_top10_kp_ids if kp}
    if prev_top10_set:
        current_kp_set = {c.get("kp_id") for c in top_cards if c.get("kp_id")}
        common = prev_top10_set & current_kp_set
        removed_pct = (len(prev_top10_set) - len(common)) / len(prev_top10_set) * 100
        if removed_pct > 60:
            logger.warning(
                "movie_discovery: notify skipped — regression detected "
                "removed_pct=%.0f%% prev_top10=%d current_common=%d",
                removed_pct, len(prev_top10_set), len(common),
            )
            return

    _enrich_cards_with_plex(top_cards)

    logger.info(
        "movie_discovery: notify start subscribers=%d top10_kp=[%s] prev_top10_kp=[%s]",
        len(subs),
        ",".join(str(c.get("kp_id") or "-") for c in top_cards),
        ",".join(str(kp) for kp in prev_top10_kp_ids) or "-",
    )

    for chat_id_str in list(subs.keys()):
        try:
            chat_id = int(chat_id_str)
        except (TypeError, ValueError):
            continue

        # Skip film when EITHER signal is already set:
        #   - notified_at: bot has already pushed it once → avoid duplicate push
        #   - shown_at: user has already opened /new and seen the film → push
        #     would be redundant
        # Plus Layer D: only push KP-enriched cards that are not already in Plex
        # and whose kp_id is confirmed by appearing in the previous top-10. A
        # film entering the top-10 for the first time has to survive one more
        # cycle before its push is allowed.
        user_entries = _get_user_entries_from_settings(settings, chat_id)
        new_for_user = [
            c for c in top_cards
            if c.get("kp_id")
            and not c.get("in_plex")
            and not _is_card_notified_in_entries(c, user_entries)
            and not _is_card_shown_in_new_in_entries(c, user_entries)
            and not _is_card_handled_in_new_in_entries(c, user_entries)
            and (not prev_top10_set or c.get("kp_id") in prev_top10_set)
        ]
        if not new_for_user:
            logger.info(
                "movie_discovery: notify chat=%s no_new (all top10 already notified/shown)",
                chat_id,
            )
            continue

        logger.info(
            "movie_discovery: notify chat=%s candidates=%d kp_ids=[%s]",
            chat_id,
            len(new_for_user),
            ",".join(str(c.get("kp_id") or "-") for c in new_for_user),
        )

        push_cards = new_for_user[:_MOVIE_NOTIFICATION_ACTION_LIMIT]
        sent, failure_label, is_permanent = await _send_movie_notification_push_to_user_result(
            push_cards, chat_id, app
        )
        if sent:
            # Only set notified_at — shown_at remains empty so the 🆕 badge stays
            # visible when the user clicks "Открыть /new" from the push.
            _mark_user_notified(chat_id, push_cards)
            _clear_movie_subscription_failures(chat_id)
            logger.info(
                "movie_discovery: notify sent chat=%s pushed=%d kp_ids=[%s]",
                chat_id,
                len(push_cards),
                ",".join(str(c.get("kp_id") or "-") for c in push_cards),
            )
        else:
            if is_permanent:
                failure_count, unsubscribed = _record_movie_subscription_failure(
                    chat_id, failure_label or "permanent"
                )
                if unsubscribed:
                    logger.warning(
                        "movie_discovery: notify failed permanently chat=%s label=%s "
                        "attempt=%s/%s — unsubscribed from /new",
                        chat_id, failure_label, failure_count, _MOVIE_NOTIFICATION_MAX_FAILURES,
                    )
                else:
                    logger.warning(
                        "movie_discovery: notify failed permanently chat=%s label=%s "
                        "attempt=%s/%s",
                        chat_id, failure_label, failure_count, _MOVIE_NOTIFICATION_MAX_FAILURES,
                    )
            else:
                log_level = logging.ERROR if failure_label == "message_format_bug" else logging.INFO
                logger.log(
                    log_level,
                    "movie_discovery: notify deferred chat=%s label=%s candidates=%d kp_ids=[%s]",
                    chat_id,
                    failure_label,
                    len(push_cards),
                    ",".join(str(c.get("kp_id") or "-") for c in push_cards),
                )


# Exponential backoff schedule when a refresh comes back with failed query
# specs (partial Jackett/Rutracker outage). After this many consecutive
# failures we give up and fall back to the normal interval — bashing the
# upstream every 30 minutes when it's clearly down for hours hurts more
# than it helps.
_MOVIE_DISCOVERY_RETRY_BACKOFF = {1: 180, 2: 600, 3: 1800}  # 3 min / 10 min / 30 min
_JACKETT_WARMUP_RETRY_BACKOFF = {1: 60, 2: 180}


async def _notify_admins(app: "Application", text: str) -> None:
    """Send a one-off informational message to every ADMIN_CHAT_IDS chat.

    Used for startup-ready / recovery signals — failures are silent
    (admin chat might be unreachable too; we just log and move on).
    """
    for admin_chat_id in ADMIN_CHAT_IDS:
        try:
            await app.bot.send_message(chat_id=admin_chat_id, text=text)
        except Exception:
            logger.warning("Failed to send admin notification to %s", admin_chat_id, exc_info=True)


async def _jackett_warmup_loop(app: "Application") -> None:
    if not _jackett_warmup_enabled():
        logger.info("Jackett warmup disabled")
        return

    global _next_jackett_warmup_at
    logger.info(
        "jackett_warmup: loop started interval=%ss batch=%s query=%r",
        JACKETT_WARMUP_INTERVAL_SECONDS,
        JACKETT_WARMUP_BATCH_SIZE,
        JACKETT_WARMUP_QUERY,
    )
    delay = 30
    fail_streak = 0
    try:
        while True:
            _next_jackett_warmup_at = time.time() + delay
            _JACKETT_WARMUP_STATUS["next_check"] = _format_warmup_dt(_next_jackett_warmup_at)
            await asyncio.sleep(delay)

            try:
                status = await _run_jackett_warmup_once()
            except Exception as exc:  # noqa: BLE001 - keep the warmup loop alive.
                logger.warning("jackett_warmup: unexpected failure", exc_info=True)
                checked_at = _format_warmup_dt(time.time())
                _JACKETT_WARMUP_STATUS.update({
                    "enabled": True,
                    "last_state": "failed",
                    "last_checked": checked_at,
                    "last_error": str(exc),
                    "last_error_kind": "unexpected",
                })
                status = dict(_JACKETT_WARMUP_STATUS)
            last_state = str(status.get("last_state") or "")
            error_kind = str(status.get("last_error_kind") or "")
            if last_state == "failed" and error_kind in {"startup", "timeout", "network", ""}:
                fail_streak += 1
                delay = _JACKETT_WARMUP_RETRY_BACKOFF.get(fail_streak, JACKETT_WARMUP_INTERVAL_SECONDS)
            else:
                fail_streak = 0
                delay = JACKETT_WARMUP_INTERVAL_SECONDS
    except asyncio.CancelledError:
        logger.info("Jackett warmup loop stopped")
        raise


async def _movie_discovery_loop(app: "Application") -> None:
    if not _movie_discovery_enabled():
        logger.info("Movie discovery disabled")
        return

    interval = MOVIE_DISCOVERY_INTERVAL_HOURS * 3600

    first_refresh_done = False

    async def _refresh_and_notify() -> None:
        nonlocal first_refresh_done
        is_first = not first_refresh_done
        if is_first:
            logger.info("movie_discovery: first refresh after startup BEGIN")
        cache = await _refresh_movie_discovery_cache()
        # Layer B of false-push protection: the very first refresh after
        # startup runs against a cold Jackett (its indexer cache hasn't
        # warmed up yet), so the resulting top-10 may contain transients.
        # We update the cache so /new works, but suppress the push.
        await _run_movie_discovery_notifications(cache, app, skip_push=is_first)
        if is_first:
            logger.info("movie_discovery: first refresh after startup DONE")
            first_refresh_done = True

    # Track refresh outcomes across iterations:
    # - fail_streak counts consecutive degraded refreshes (sets shorter retry
    #   interval, falling back to the normal one after _MOVIE_DISCOVERY_RETRY_BACKOFF
    #   exhausts — no point bashing upstream forever).
    # - startup_ready_notified ensures the «✅ Bot ready» admin push fires
    #   exactly once per process lifetime, on the first clean refresh.
    fail_streak = 0
    startup_ready_notified = False
    current_interval = interval

    def _read_degradation_signal() -> tuple[list, list, list]:
        """Return (failed_specs, failed_enabled_ids, failed_disabled_ids).

        - failed_specs / failed_enabled_ids are the «gating» signals: either
          non-empty triggers retry backoff and blocks the ready notification.
        - failed_disabled_ids is info-only: indexers Jackett queries but the
          user excluded from /new rating (e.g. broken Cloudflare-protected
          ones). Surfaced in the ready message so the admin sees them, but
          doesn't drive retry/backoff.
        """
        cache = _load_movie_discovery_cache()
        specs = cache.get("last_failed_specs")
        enabled = cache.get("last_failed_indexer_ids")
        disabled = cache.get("last_failed_indexer_ids_disabled")
        return (
            list(specs) if isinstance(specs, list) else [],
            list(enabled) if isinstance(enabled, list) else [],
            list(disabled) if isinstance(disabled, list) else [],
        )

    def _read_card_count() -> int:
        cache = _load_movie_discovery_cache()
        return len(cache.get("cards") or [])

    async def _maybe_send_startup_ready(
        failed_enabled: list,
        failed_disabled: list,
        card_count: int,
    ) -> bool:
        """Send the «poisk razogret» admin notification when conditions are met.

        Ready = bot is functional and /new has content. Does NOT require all
        indexers to be perfectly healthy — some indexers (e.g. noname-club
        without FlareSolverr) may be permanently degraded; we shouldn't
        block the startup signal on them.

        Two failure groups (partitioned in _refresh_movie_discovery_cache):
          - failed_enabled: user wants these in /new rating → they GATE the
            ready signal (defer while retrying, ready once backoff exhausts).
          - failed_disabled: user excluded these from /new → info-only,
            never gates ready, just listed for admin awareness.
        """
        if startup_ready_notified or card_count <= 0:
            return False
        if failed_enabled and fail_streak < len(_MOVIE_DISCOVERY_RETRY_BACKOFF):
            # Still retrying enabled-rating failures — defer ready.
            return False
        text = "✅ Поиск разогрет, бот полноценно функционирует."
        if failed_enabled:
            text += (
                f"\n⚠️ Индексеры рейтинга с проблемами: {', '.join(failed_enabled)}"
                "\n   Содержимое /new восполнено из прошлого кэша."
            )
        if failed_disabled:
            text += (
                f"\nℹ️ Прочие индексеры (не влияют на /new): {', '.join(failed_disabled)}"
                "\n   — отключены или сломаны на стороне трекера."
            )
        await _notify_admins(app, text)
        return True

    try:
        logger.info("movie_discovery: loop started — first refresh now, interval=%dh", MOVIE_DISCOVERY_INTERVAL_HOURS)
        # Immediate first refresh.
        await _run_background_step("movie discovery refresh", _refresh_and_notify)
        failed_specs, failed_enabled, failed_disabled = _read_degradation_signal()
        card_count = _read_card_count()
        if failed_specs or failed_enabled:
            fail_streak = 1
            current_interval = _MOVIE_DISCOVERY_RETRY_BACKOFF.get(fail_streak, interval)
            logger.info(
                "movie_discovery: first refresh degraded (failed_specs=%s, "
                "failed_enabled=%s, failed_disabled=%s, cards=%d) — retry in %ds",
                failed_specs or "none", failed_enabled or "none",
                failed_disabled or "none", card_count, current_interval,
            )
            # Try to send ready signal even on degraded refresh — if we have
            # cards in cache, the bot IS functional. The condition inside
            # _maybe_send_startup_ready prevents firing too early when we
            # still plan retries.
            if await _maybe_send_startup_ready(failed_enabled, failed_disabled, card_count):
                startup_ready_notified = True
        else:
            # All clean — send ready immediately, but still mention disabled
            # failures if there were any (info-only).
            if failed_disabled:
                await _notify_admins(
                    app,
                    "✅ Поиск разогрет, бот полноценно функционирует."
                    f"\nℹ️ Прочие индексеры (не влияют на /new): {', '.join(failed_disabled)}"
                    "\n   — отключены или сломаны на стороне трекера.",
                )
            else:
                await _notify_admins(
                    app, "✅ Поиск разогрет, бот полноценно функционирует.",
                )
            startup_ready_notified = True

        while True:
            await asyncio.sleep(current_interval)
            await _run_background_step("movie discovery refresh", _refresh_and_notify)
            failed_specs, failed_enabled, failed_disabled = _read_degradation_signal()
            card_count = _read_card_count()
            if failed_specs or failed_enabled:
                fail_streak += 1
                current_interval = _MOVIE_DISCOVERY_RETRY_BACKOFF.get(fail_streak, interval)
                logger.info(
                    "movie_discovery: degraded refresh (streak=%d, failed_specs=%s, "
                    "failed_enabled=%s, failed_disabled=%s, cards=%d) — retry in %ds",
                    fail_streak, failed_specs or "none", failed_enabled or "none",
                    failed_disabled or "none", card_count, current_interval,
                )
                # After backoff is exhausted (fail_streak >= len(BACKOFF)) the
                # partial failure is persistent — fire ready anyway so the
                # admin isn't left hanging forever on a permanently broken
                # indexer (e.g. noname-club without FlareSolverr).
                if await _maybe_send_startup_ready(failed_enabled, failed_disabled, card_count):
                    startup_ready_notified = True
            else:
                was_degraded = fail_streak > 0
                if was_degraded:
                    logger.info(
                        "movie_discovery: recovered after %d failed refreshes",
                        fail_streak,
                    )
                    await _notify_admins(
                        app, f"✅ Поиск восстановился после {fail_streak} неудачных попыток.",
                    )
                if not startup_ready_notified:
                    await _notify_admins(
                        app, "✅ Поиск разогрет, бот полноценно функционирует.",
                    )
                    startup_ready_notified = True
                fail_streak = 0
                current_interval = interval
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

    Strategy:
      1. If ``year > 0`` — try year-bounded match with ±1 tolerance (covers
         off-by-one regional dates).
      2. Title-only fallback — match by normalised title across all years.
         Necessary because for series the user's ``meta.year`` typically
         reflects the season/episode year (e.g. 2026 for Good Omens S3E1),
         while Plex caches the show under its PREMIERE year (e.g. 2019).
         Without this fallback, _plex_poll_lookup_target never finds the
         show and the «✅ добавлен в Plex» notification is never sent.
    """
    norm = _normalize_movie_title(series_query).lower()
    if not norm:
        return None
    if year > 0:
        for dy in (0, 1, -1):
            hit = _plex_shows_library.get((norm, year + dy))
            if hit is not None:
                return hit
        # Fall through to title-only — series years frequently disagree with
        # Plex's premiere year, so we don't return None here.
    for (cached_title, _cached_year), show in _plex_shows_library.items():
        if cached_title == norm:
            return show
    return None


async def _plex_ensure_show_seasons(show: "PlexShow") -> dict[int, "PlexSeason"]:
    """Lazily populate ``show.seasons`` via :meth:`PlexClient.get_show_seasons`.

    First call hits the network (one request per season + season list).
    Subsequent calls reuse the cached dict on the show instance. Returns
    an empty dict on any failure.
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


async def _plex_ensure_show_seasons_lite(
    show: "PlexShow", *, focus_season: int | None,
) -> dict[int, "PlexSeason"]:
    """R.2 optimisation #1: lazy season-fetch that only reads episode files
    for the season the user is actively checking. Other seasons return with
    ``resolution=""`` and empty ``file_paths`` — fine for showing context
    («✅ В Plex уже есть: S1 (8 эп.), S3 (12 эп.)») without paying for
    N episode-children requests.

    On a 5-season show this collapses the cold-path from 6 HTTP requests
    (1 list + 5 per-season) to 2 (1 list + 1 for the focused season).

    The cache merges: if a previous call already cached the full set, we
    return it as-is. If a previous lite call cached only one season's
    resolution, a follow-up call for a different focus_season triggers
    another fetch and merges into the cached dict. Worst-case repeats are
    rare because most subscribe flows ask for one season per session.
    """
    # Already fully cached (resolution known for the focus, or full snapshot).
    # Check cache BEFORE the plex_client guard so callers with pre-populated
    # PlexShow.seasons (notably unit tests) don't accidentally short-circuit
    # to an empty dict.
    if show.seasons:
        if focus_season is None or focus_season not in show.seasons:
            return show.seasons
        cached_season = show.seasons[focus_season]
        if cached_season.resolution:
            return show.seasons
        # Cached but missing resolution for the requested season → top up
        # by fetching just this one season's episode files. Cheap: 1 request.
        if plex_client is None:
            return show.seasons
        try:
            top_up = await asyncio.to_thread(
                plex_client.get_show_seasons_lite,
                show.rating_key,
                fetch_resolution_for=[focus_season],
            )
        except Exception as exc:
            logger.debug("Plex seasons-lite top-up failed for %r: %s",
                         show.title, exc)
            return show.seasons
        refreshed = top_up.get(focus_season)
        if refreshed and refreshed.resolution:
            show.seasons[focus_season] = refreshed
        return show.seasons

    # Cold cache — first fetch for this show.
    if plex_client is None:
        return {}
    fetch_for = [focus_season] if focus_season else []
    try:
        seasons = await asyncio.to_thread(
            plex_client.get_show_seasons_lite,
            show.rating_key,
            fetch_resolution_for=fetch_for,
        )
    except Exception as exc:
        logger.debug("Plex seasons-lite fetch failed for %r: %s", show.title, exc)
        return {}
    if seasons:
        show.seasons = seasons
    return seasons


# Background task store for Plex season pre-warm (optimisation #3). Mapping
# chat_id → asyncio.Task so we can cancel stale ones if the user navigates
# away. Same pattern as _didmean_prefetch tasks.
_plex_prewarm_tasks: dict[int, "asyncio.Task[None]"] = {}


def _cancel_plex_prewarm(chat_id: int) -> None:
    """Cancel any in-flight Plex season pre-warm for this chat. Idempotent."""
    task = _plex_prewarm_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _plex_prewarm_show_seasons(
    chat_id: int, series_query: str, season_num: int | None,
) -> None:
    """Pre-warm the season cache for the show the user is likely about to
    subscribe to. Fires after results render so by the time they tap «🔔 N»
    → preset picker → confirm, the Plex pre-check is instant.

    Cheap: at most 2 HTTP requests to Plex (1 + focus season). No-op when
    Plex is disabled, the show isn't in cache, or the seasons are already
    fetched. Exceptions are swallowed — this is best-effort UX polish, not
    correctness path.
    """
    if not PLEX_ENABLED or plex_client is None:
        return
    if not series_query or not season_num:
        return
    try:
        show = _plex_show_find(series_query)
        if show is None:
            return
        # Already cached with the resolution we need → nothing to do.
        if show.seasons and (
            season_num not in show.seasons
            or show.seasons[season_num].resolution
        ):
            return
        await _plex_ensure_show_seasons_lite(show, focus_season=season_num)
        logger.debug(
            "Plex pre-warm done: chat=%s show=%r season=%s seasons_cached=%d",
            chat_id, show.title, season_num, len(show.seasons),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Plex pre-warm failed for %r S%s: %s", series_query, season_num, exc)


def _schedule_plex_prewarm(
    context: "ContextTypes.DEFAULT_TYPE",
    chat_id: int | None,
    series_query: str,
    season_num: int | None,
) -> None:
    """Fire-and-forget pre-warm scheduler. Stashes the task so navigation
    can cancel it. Safe to call multiple times — older task gets cancelled."""
    if chat_id is None or not season_num:
        return
    _cancel_plex_prewarm(chat_id)
    try:
        task = asyncio.create_task(
            _plex_prewarm_show_seasons(chat_id, series_query, season_num)
        )
    except RuntimeError:
        # No running loop (rare: called from sync context in tests) — skip.
        return
    _plex_prewarm_tasks[chat_id] = task


def _maybe_prewarm_plex_for_results(
    context: "ContextTypes.DEFAULT_TYPE",
    chat_id: int | None,
    results_data: list[dict],
) -> None:
    """If the rendered results contain a partial-season match, pre-warm Plex's
    season cache for that show. R.2 optimisation #3 — by the time the user
    taps «🔔 N» → preset picker → confirm, the Plex pre-check is instant.

    We only pre-warm for the FIRST partial result the user is most likely
    to interact with. Multi-result pre-warming is over-engineering: most
    cold-paths take ~200ms and pre-warm runs in parallel with the user
    reading the results, so a single show is enough for the UX win.
    """
    if not PLEX_ENABLED or chat_id is None:
        return
    for r in results_data:
        if not r.get("partial"):
            continue
        title = str(r.get("title") or r.get("movie_title") or "")
        if not title:
            continue
        series_query = _extract_series_base_query(title) or ""
        season_num = _extract_season_from_query(title)
        if series_query and season_num:
            _schedule_plex_prewarm(context, chat_id, series_query, season_num)
        return  # only the first partial result


def _get_plex_unmatched_lists() -> tuple[list[PlexMovie], list[PlexShow]]:
    """Return current unmatched movies + shows from the in-memory caches.

    'Unmatched' = Plex couldn't match the file with any metadata agent
    (guid starts with 'local://' or is empty). Used both for the /admin
    pull view and as the source for diff-based push notifications.
    """
    movies = [m for m in _plex_library.values() if _plex_is_unmatched(m)]
    shows  = [s for s in _plex_shows_library.values() if _plex_is_unmatched(s)]
    return movies, shows


def _get_plex_unmatched_counts() -> dict:
    """Return ``{"movies": N, "shows": M, "total": N+M}`` for /admin badges."""
    movies, shows = _get_plex_unmatched_lists()
    return {"movies": len(movies), "shows": len(shows), "total": len(movies) + len(shows)}


def _format_unmatched_short_label(entry) -> str:
    """Return a short human-readable label for an unmatched Plex entry.

    Picks the last component of ``file_paths[0]`` when available (it's the
    real filename Plex couldn't match), falling back to title or rating_key.
    """
    file_paths = getattr(entry, "file_paths", None) or []
    if file_paths:
        first = file_paths[0]
        # Strip path separator (works for both / and \)
        last = re.split(r"[\\/]", first)[-1]
        if last:
            return last
    title = getattr(entry, "title", "") or ""
    if title:
        return title
    return f"#{getattr(entry, 'rating_key', '?')}"


def _plex_web_details_link(rating_key: str = "", machine_id: str = "") -> str:
    if not rating_key or not machine_id:
        return ""
    return (
        f"https://app.plex.tv/desktop/#!/server/{machine_id}"
        f"/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
    )


def _format_unmatched_entry_link(entry) -> str:
    label = html_module.escape(_format_unmatched_short_label(entry))
    url = _plex_web_details_link(getattr(entry, "rating_key", ""), _plex_machine_id)
    if not url:
        return f"<code>{label}</code>"
    return f'<a href="{html_module.escape(url, quote=True)}">{label}</a>'


def _plex_cache_info() -> dict:
    """Return metadata dict for diagnostics, including health state."""
    import time, datetime

    def _fmt(ts: float) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""

    unmatched_counts = _get_plex_unmatched_counts()
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
        "unmatched_movies": unmatched_counts["movies"],
        "unmatched_shows": unmatched_counts["shows"],
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


async def _refresh_plex_library(
    app: "Application | None" = None,
    *,
    check_unmatched: bool = True,
) -> None:
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

        if check_unmatched:
            # Admin radar: check for newly-appeared unmatched files. Runs after both
            # movie and show caches are fresh so the diff is consistent. The seen
            # snapshot is updated unconditionally — that way an off→on toggle later
            # doesn't dump every existing unmatched file at once.
            await _check_plex_unmatched_against_seen(app=app)


async def _check_plex_unmatched_against_seen(app: "Application | None") -> None:
    """Compare current unmatched lists to the persisted ``plex_unmatched_seen``
    snapshot and, when the toggle is on, schedule a push to admins about either
    the initial inventory (first enable) or the newly-appeared entries.

    ``app`` is passed when the caller has the bot instance handy (e.g. from the
    notification loop). When called inside ``_refresh_plex_library`` we don't
    have ``app`` — push is skipped in that case, but the snapshot is still
    updated. Once the wired-up scheduler in main passes its app reference, the
    push path will be exercised.
    """
    movies, shows = _get_plex_unmatched_lists()
    current_movies = {m.rating_key for m in movies if m.rating_key}
    current_shows  = {s.rating_key for s in shows  if s.rating_key}

    seen = _load_plex_unmatched_seen()
    prev_movies = set(seen["movies"])
    prev_shows  = set(seen["shows"])

    if app is not None and _is_plex_unmatched_notify_enabled():
        if not prev_movies and not prev_shows and (current_movies or current_shows):
            asyncio.create_task(_notify_admins_unmatched(app, movies, shows, kind="initial"))
        else:
            new_movies_list = [m for m in movies if m.rating_key in current_movies - prev_movies]
            new_shows_list  = [s for s in shows  if s.rating_key in current_shows  - prev_shows]
            if new_movies_list or new_shows_list:
                asyncio.create_task(
                    _notify_admins_unmatched(app, new_movies_list, new_shows_list, kind="new")
                )

    _save_plex_unmatched_seen({
        "movies": sorted(current_movies),
        "shows":  sorted(current_shows),
    })


async def _notify_admins_unmatched(
    app: "Application",
    movies: list,
    shows: list,
    *,
    kind: str,
) -> None:
    """Push a Telegram message to every ADMIN_CHAT_IDS about unmatched files.

    ``kind`` is either ``"initial"`` (sent once when the toggle is first enabled
    and the seen-snapshot is empty) or ``"new"`` (sent on every refresh that
    discovers files not in the previous snapshot).
    """
    if not ADMIN_CHAT_IDS:
        return
    text = _format_unmatched_push(movies, shows, kind=kind)
    for chat_id in sorted(ADMIN_CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            logger.warning(
                "Plex-unmatched notification failed for admin chat_id=%s", chat_id, exc_info=True
            )


def _format_unmatched_list(movies: list, shows: list) -> str:
    """Build the HTML body for the /admin → 📋 Несматчено screen.

    Shows up to 25 entries per kind to stay within Telegram's 4096-char
    message limit even for libraries with hundreds of unmatched files.
    Falls back to a clean confirmation when everything is matched.
    """
    if not movies and not shows:
        return "✅ <b>Все файлы Plex успешно сматчены.</b>"

    lines: list[str] = [
        "📋 <b>Несматченные файлы в Plex</b>",
        "",
    ]

    def _bullets(items: list, limit: int = 25) -> list[str]:
        out = [
            f"• {_format_unmatched_entry_link(x)}"
            for x in items[:limit]
        ]
        extra = len(items) - limit
        if extra > 0:
            out.append(f"• …и ещё {extra}")
        return out

    if movies:
        lines.append(f"🎬 <b>Фильмы ({len(movies)})</b>")
        lines.extend(_bullets(movies))
    if shows:
        if movies:
            lines.append("")
        lines.append(f"📺 <b>Сериалы ({len(shows)})</b>")
        lines.extend(_bullets(shows))
    return "\n".join(lines)


def _format_unmatched_push(movies: list, shows: list, *, kind: str) -> str:
    """Build the HTML body for an admin push about unmatched Plex files.

    Shows up to 5 of each kind; longer lists fall back to a 'и ещё K' suffix
    and a hint to open ``/admin → 📋 Несматчено`` for the full picture.
    """
    total = len(movies) + len(shows)
    if kind == "initial":
        head = (
            f"📋 <b>Включены уведомления о несматченных в Plex</b>\n"
            f"Сейчас в библиотеке {total} {_plural(total, 'файл', 'файла', 'файлов')}:"
        )
    else:
        head = (
            f"⚠️ <b>В Plex появились новые несматченные файлы ({total})</b>"
        )

    def _bullets(items: list, limit: int = 5) -> str:
        head_part = "\n".join(
            f"• {_format_unmatched_entry_link(x)}"
            for x in items[:limit]
        )
        extra = len(items) - limit
        if extra > 0:
            head_part += f"\n• …и ещё {extra}"
        return head_part

    lines: list[str] = [head]
    if movies:
        lines.append("")
        lines.append(f"🎬 <b>Фильмы ({len(movies)}):</b>")
        lines.append(_bullets(movies))
    if shows:
        lines.append("")
        lines.append(f"📺 <b>Сериалы ({len(shows)}):</b>")
        lines.append(_bullets(shows))
    lines.append("")
    lines.append("Полный список: /admin → 📋 Несматчено")
    return "\n".join(lines)


async def _plex_cache_loop(app: "Application | None" = None) -> None:
    if plex_client is None:
        logger.info("Plex not configured — cache loop disabled")
        return
    try:
        await _run_background_step(
            "initial Plex library cache",
            lambda: _refresh_plex_library(app),
        )
        while True:
            await asyncio.sleep(_PLEX_CACHE_INTERVAL)
            await _run_background_step(
                "Plex library cache refresh",
                lambda: _refresh_plex_library(app),
            )
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


def _recompute_and_resort_cards(cards: list[dict]) -> None:
    """Recompute ``score`` with the current formula+year and resort in-place.

    The cache stores ``score`` snapshotted at the last refresh. The score
    depends on ``current_year`` (year-boundary changes the recency component,
    see ``_compute_card_score`` in ``movie_discovery.py``) and on the formula
    constants/weights — both can drift between cache write and next display
    (year rollover, deploy with formula tweaks). On cache hit we recompute to
    avoid showing a stale order until the next background refresh.

    Pure CPU, no network. O(N log N) over ~10–50 cards.
    """
    if not cards:
        return
    current_year = datetime.now(DISPLAY_TIMEZONE).year
    for card in cards:
        card["score"] = _movie_compute_card_score(card, current_year)
    cards.sort(key=lambda c: c.get("score") or 0, reverse=True)


# Accepts "S01E02", "1x02", "Сезон 3", "Сезон: 3", "Сезон:3", "СЕЗОН 3",
# "1-й сезон" — the colon-form is the most common on Rutracker. Case-insensitive
# matching is applied via re.IGNORECASE; the literal "сезон" is enough since the
# flag covers Latin/Cyrillic case mixing.
_SERIES_RE = re.compile(
    r"s\d+e\d+|\d+x\d+|сезон[:\s]+\d+|"
    r"\b\d{1,2}\s*(?:[-‑–—]?\s*(?:й|ый|ой))?\s+сезон\b",
    re.IGNORECASE,
)


def _plex_is_series(title: str) -> bool:
    """Return True if *title* looks like a TV series episode (skip Plex check for those)."""
    return bool(_SERIES_RE.search(title))


def _plex_deep_link(rating_key: str = "", machine_id: str = "") -> str:
    """Build a Plex deep-link URL for use in a Telegram inline button.

    Telegram rejects ``plex://`` URLs in inline-button URLs since May 2026
    ("unsupported url protocol"). So we always return an https URL.

    Two modes, depending on whether ``PLEX_DEEPLINK_BASE_URL`` is configured:

    1. **Empty (default)** — fallback to ``https://app.plex.tv/desktop`` (Plex
       Web). Opens in Safari/browser on iOS because Plex does NOT publish
       Universal Links for app.plex.tv (checked their AASA file; only
       watch.plex.tv has them, and only for Plex Discover public catalog,
       not for personal servers).

    2. **Configured** — append ``?key=...&server=...`` query params to that
       base URL. The user is expected to host a tiny redirect page at that
       URL that reads the params and does
       ``location.href = "plex://preplay/?metadataKey=...&server=..."`` —
       which DOES launch the native Plex app on iOS/Android via custom URL
       scheme (Safari accepts plex:// in location.href; only Telegram
       inline-button URLs reject it). See README for the redirect snippet.
    """
    base = (PLEX_DEEPLINK_BASE_URL or "").strip()
    if not base:
        # Default: Plex Web. Best we can do without a redirect page.
        if not rating_key or not machine_id:
            return "https://app.plex.tv/desktop"
        return (
            f"https://app.plex.tv/desktop/#!/server/{machine_id}"
            f"/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
        )
    # Configured redirect: append key+server. Empty rating_key/machine_id
    # → just the base (placeholder for "open Plex").
    if not rating_key or not machine_id:
        return base
    from urllib.parse import urlencode
    sep = "&" if "?" in base else "?"
    qs = urlencode({
        "key": f"/library/metadata/{rating_key}",
        "server": machine_id,
    })
    return f"{base}{sep}{qs}"


def _plex_quality_from_result(result: dict) -> str:
    """Return a Plex-normalised quality string from a search result dict."""
    q = result.get("quality") or ""
    if q:
        return _plex_normalise_resolution(q)
    return _plex_normalise_resolution(_movie_detect_quality(result.get("title") or "") or "")


def _plex_quality_from_title(title: str) -> str:
    """Return a Plex-normalised quality string extracted from a raw file/torrent name."""
    return _plex_normalise_resolution(_movie_detect_quality(title) or "")


def _positive_ints(values) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    return result


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


async def _plex_result_hint(result: dict, requested_quality: str) -> dict | None:
    if not PLEX_ENABLED or not requested_quality:
        return None
    title = str(result.get("title") or "")
    season_num = _extract_season_from_query(title)
    series_query = _extract_series_base_query(title)
    if series_query and season_num and _plex_shows_library:
        check = await _plex_pre_check_series(series_query, season_num, requested_quality)
        if check is not None:
            return {
                "kind": "series",
                "action": check.action,
                "quality": check.season.resolution,
                "season": season_num,
                "confidence": "high",
            }
        show = _plex_show_find(series_query)
        if show is not None:
            seasons = await _plex_ensure_show_seasons_lite(show, focus_season=None)
            other = sorted(s for s in show.seasons if s != season_num)
            if other:
                return {
                    "kind": "series",
                    "action": "other_seasons",
                    "seasons": other,
                    "season": season_num,
                    "confidence": "medium",
                }
        return None
    year = _movie_extract_year(title) or 0
    check = _plex_pre_check(title, year, requested_quality)
    if check is None:
        return None
    return {
        "kind": "movie",
        "action": check.action,
        "quality": check.plex_movie.resolution,
        "confidence": "high",
    }


async def _enrich_results_with_plex_hints(
    results: list[dict],
    requested_quality: str | None,
    *,
    max_n: int = 15,
) -> None:
    if not requested_quality:
        return
    requested_quality = _plex_normalise_resolution(requested_quality)
    if not requested_quality:
        return
    for result in results[:max_n]:
        if result.get("plex_hint"):
            continue
        hint = await _plex_result_hint(result, requested_quality)
        if hint:
            result["plex_hint"] = hint


async def _cluster_plex_hint(cluster: dict, requested_quality: str | None) -> dict | None:
    if not PLEX_ENABLED or not requested_quality:
        return None
    title = str(cluster.get("title") or "")
    if cluster.get("kind") == "series":
        show = _plex_show_find(title)
        if show is None:
            return None
        seasons = _positive_ints(cluster.get("seasons") or [])
        if len(seasons) == 1:
            season_num = seasons[0]
            await _plex_ensure_show_seasons_lite(show, focus_season=season_num)
            season = show.seasons.get(season_num)
            if season is None:
                return None
            check = _plex_check_before_download_season(show, season, requested_quality)
            return {
                "kind": "series",
                "action": check.action,
                "quality": season.resolution,
                "season": season_num,
                "confidence": "high",
            }
        await _plex_ensure_show_seasons_lite(show, focus_season=None)
        present = sorted(s for s in seasons if s in show.seasons)
        if present and len(present) == len(seasons):
            return {
                "kind": "series",
                "action": "warn_same",
                "seasons": present,
                "confidence": "medium",
            }
        if present:
            return {
                "kind": "series",
                "action": "other_seasons",
                "seasons": present,
                "confidence": "medium",
            }
        return None

    year = int(cluster.get("year") or 0)
    match = _plex_library_find(title, year)
    if match is None:
        return None
    check = _plex_check_before_download(match, requested_quality)
    return {
        "kind": "movie",
        "action": check.action,
        "quality": match.resolution,
        "confidence": "high",
    }


async def _enrich_clusters_with_plex_hints(clusters: list[dict], requested_quality: str | None) -> None:
    if not requested_quality:
        return
    requested_quality = _plex_normalise_resolution(requested_quality)
    if not requested_quality:
        return
    for cluster in clusters:
        if cluster.get("plex_hint"):
            continue
        hint = await _cluster_plex_hint(cluster, requested_quality)
        if hint:
            cluster["plex_hint"] = hint


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

    R.2: uses the lite-variant season fetcher so cold-path issues only
    1+1 = 2 Plex requests instead of 1+N.
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
    seasons = await _plex_ensure_show_seasons_lite(show, focus_season=season_num)
    season = seasons.get(season_num)
    if season is None:
        return None
    return _plex_check_before_download_season(show, season, requested_quality)


def _plex_other_seasons_context(
    show: "PlexShow", focus_season: int,
) -> list["PlexSeason"]:
    """Return sorted list of seasons OTHER than the focus one, for context.

    Used by the R.2 confirm dialog to show «✅ В Plex уже есть: S1, S3, S4»
    alongside the warning/upgrade message about the focus season. The
    seasons come from the in-memory cache populated by the lite fetcher —
    resolution is not guaranteed (we deliberately didn't fetch it for
    these), so callers should treat empty ``resolution`` as «unknown»,
    not «SD».
    """
    return sorted(
        (s for n, s in show.seasons.items() if n != focus_season),
        key=lambda s: s.season_number,
    )


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
                logger.info(
                    "Plex lookup: show %r found but season %d missing (have: %s)",
                    show.title, season_num, sorted(seasons.keys()),
                )
            else:
                logger.info(
                    "Plex lookup: series show not found query=%r year=%s shows_cached=%d",
                    series_query, meta.get("year"), len(_plex_shows_library),
                )
        else:
            logger.info(
                "Plex lookup: series meta incomplete query=%r season_num=%s",
                series_query, season_num,
            )
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

    logger.info(
        "Plex lookup: movie not found task_title=%r meta_title=%r year=%s movies_cached=%d",
        task_title,
        (meta or {}).get("title", ""),
        (meta or {}).get("year", 0),
        len(_plex_library),
    )
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
    cancelled = False
    try:
        for attempt in range(max_attempts):
            if attempt > 0:
                await asyncio.sleep(interval_seconds)

            # Refresh Plex library, then look for the file. Unmatched admin radar
            # stays on the regular cache loop so Plex has time to finish matching.
            await _refresh_plex_library(app, check_unmatched=False)
            # Heuristic: if the global failure counter is at 0,
            # this refresh succeeded.
            if _plex_consecutive_failures == 0:
                refresh_succeeded_at_least_once = True
            target, metadata_type, found_title = await _plex_poll_lookup_target(task_title, meta)

            if target is not None:
                # Build deep link
                machine_id = _plex_machine_id
                rating_key = getattr(target, "rating_key", "")
                # Build Plex deep-link. Honors PLEX_DEEPLINK_BASE_URL if set
                # (user-hosted redirect page → native Plex app on iOS);
                # otherwise falls back to Plex Web at app.plex.tv.
                deep_link = _plex_deep_link(rating_key, machine_id) if machine_id and rating_key else ""

                text = f"✅ <b>{html_module.escape(found_title)}</b> добавлен в Plex."
                close_btn = InlineKeyboardButton(
                    BUTTON_CLOSE, callback_data=_task_callback("close", ""),
                )
                if deep_link:
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("▶️ Смотреть в Plex", url=deep_link)],
                        [close_btn],
                    ])
                else:
                    keyboard = InlineKeyboardMarkup([[close_btn]])
                await _delete_hint_messages()
                _record_download_history(
                    "plex_found",
                    chat_ids=chat_ids,
                    task_id=task_id,
                    meta=meta,
                    title=found_title,
                    plex_rating_key=rating_key,
                    plex_metadata_type=metadata_type,
                )
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
        _record_download_history(
            "plex_not_found",
            chat_ids=chat_ids,
            task_id=task_id,
            meta=meta,
            title=task_title,
            reason=log_reason,
            timeout_minutes=timeout_min,
        )
        keyboard = _final_notification_keyboard(task_id, show_plex=False)
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
                    "Plex poll: failed to send timeout-notification chat_id=%s", cid, exc_info=True
                )
        logger.info(
            "Plex polling: gave up on %r after %d attempt(s) — reason=%s",
            task_title, max_attempts, log_reason,
        )
    except asyncio.CancelledError:
        cancelled = True
        await _delete_hint_messages()
        logger.info("Plex polling task cancelled for task_id=%s", task_id)
        raise
    finally:
        if cancelled:
            _PLEX_POLLING_TASKS.pop(task_id, None)
        else:
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


def _format_other_seasons_context(other_seasons: list["PlexSeason"]) -> str:
    """Render the «✅ В Plex уже есть: S1 (8 эп.), S3 (12 эп.)» line for the
    confirm dialog. R.2 context block — gives the user the surrounding
    state of the show so they don't have to switch apps to remember what's
    already in their library.

    Returns "" when no other seasons exist (don't add an empty section).
    Resolution is included only when known (lite-fetcher skipped fetching
    episodes for these seasons, so resolution may be empty).
    """
    if not other_seasons:
        return ""
    parts = [_format_single_season_context(s) for s in other_seasons]
    return "✅ В Plex уже есть: " + ", ".join(parts)


def _format_single_season_context(season: "PlexSeason") -> str:
    """One-season context fragment used in the «уже есть» line."""
    n = season.season_number
    ep = f"{season.episode_count} эп." if season.episode_count else ""
    res = season.resolution.upper() if season.resolution else ""
    if ep and res:
        return f"S{n} ({ep}, {res})"
    if ep:
        return f"S{n} ({ep})"
    if res:
        return f"S{n} ({res})"
    return f"S{n}"


def _plex_series_confirm_text(
    check: "PlexSeriesCheckResult",
    display_title: str,
    requested_quality: str,
) -> str:
    """Format the pre-download Plex warning for a TV season (HTML).

    R.2: adds an «другие сезоны в Plex» context block above the warning
    so the user sees the surrounding library state in one screen.
    """
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
        prompt = "Скачать всё равно?"
    elif check.action == "warn_better":
        req_display = requested_quality.upper() if requested_quality else "неизвестное качество"
        verb = f"уже есть в Plex в лучшем качестве ({plex_res_display} &gt; {req_display})"
        prompt = "Скачать всё равно?"
    else:  # offer_upgrade
        req_display = requested_quality.upper() if requested_quality else "неизвестное качество"
        verb = f"есть в Plex в худшем качестве ({plex_res_display}), запрошено {req_display}"
        prompt = "Заменить версией получше или скачать дубликатом?"

    # R.2 context block — other seasons of the same show already in Plex.
    other_seasons = _plex_other_seasons_context(check.show, season_num)
    context_line = _format_other_seasons_context(other_seasons)
    context_block = f"\n{html_module.escape(context_line)}" if context_line else ""

    return (
        f"⚠️ <b>{head}</b> {verb}.{context_block}\n"
        f"<i>Из раздачи: {title_esc}</i>\n"
        f"{prompt}"
    )


def _make_task_keyboard(task_id: str, status: str = "", task_type: str = "") -> InlineKeyboardMarkup:
    """Bot-level wrapper: injects tracker-button visibility state into the stateless _task_keyboard."""
    return _task_keyboard(
        task_id, status, task_type,
        show_trackers=_tracker_button_visible(task_id, status, task_type),
    )


def _notification_keyboard(task_id: str, status: str = "", task_type: str = "") -> InlineKeyboardMarkup:
    if (status or "").lower() in {"finished", "seeding"}:
        return _final_notification_keyboard(task_id, show_plex=False)

    return _make_task_keyboard(task_id, status, task_type)


def _format_task_card(task: dict, chat_id: int | None = None) -> str:
    auto_delete_enabled = _auto_delete_finished_enabled()
    return _view_format_task_card(
        task,
        is_admin=_is_admin_chat(chat_id),
        auto_delete_tasks=_load_auto_delete_tasks() if auto_delete_enabled else {},
        auto_delete_enabled=auto_delete_enabled,
        auto_delete_statuses=AUTO_DELETE_FINISHED_STATUSES,
        auto_delete_after_hours=AUTO_DELETE_FINISHED_AFTER_HOURS,
        display_timezone=DISPLAY_TIMEZONE,
    )


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


def _merge_task_meta_release(meta: dict, parsed: dict | None) -> dict:
    if not isinstance(parsed, dict):
        return meta
    release = {
        key: parsed.get(key)
        for key in ("quality", "source", "hdr", "audio", "langs", "release_group", "edition")
        if parsed.get(key) not in (None, "", [], {})
    }
    if not release:
        return meta
    enriched = dict(meta)
    enriched["release"] = _history_jsonable(release)
    if not enriched.get("quality") and release.get("quality"):
        enriched["quality"] = str(release["quality"])
    return enriched


async def _build_task_meta_from_title_with_gpt(title: str, *, source: str) -> dict:
    meta = _build_task_meta_from_title(title, source=source)
    if not GPT_ENABLED or not title:
        return meta

    try:
        cache = state_store.load_torrent_titles_cache()
    except Exception:
        logger.warning("manual title cache load failed", exc_info=True)
        cache = {}
    title_hash = _title_hash(title)
    cached = cache.get(title_hash) if isinstance(cache, dict) else None
    if isinstance(cached, dict):
        return _merge_task_meta_release(meta, cached)

    sink: list = []
    try:
        parsed, error = await asyncio.to_thread(
            gpt_features_parse_torrent_title,
            title=title,
            api_key=OPENAI_API_KEY,
            model=GPT_MODEL,
            usage_sink=sink,
        )
    except Exception:
        logger.warning("manual title parse failed for %r", title[:80], exc_info=True)
        return meta

    _gpt_record_usage(
        feature="manual_title_parse",
        input_tokens=200,
        output_tokens=80,
        error_label=error,
        usage=(sink[0] if sink else None),
    )
    if not parsed:
        return meta

    if isinstance(cache, dict):
        cache[title_hash] = parsed
        if len(cache) > _TITLE_CACHE_MAX_ENTRIES:
            for old_hash in list(cache.keys())[:_TITLE_CACHE_EVICT_BATCH]:
                cache.pop(old_hash, None)
        try:
            state_store.save_torrent_titles_cache(cache)
        except Exception:
            logger.warning("manual title cache save failed", exc_info=True)
    return _merge_task_meta_release(meta, parsed)


def _history_jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(k): _history_jsonable(v)
            for k, v in value.items()
            if v not in (None, "", [], {})
        }
    if isinstance(value, (list, tuple, set)):
        return [_history_jsonable(v) for v in value if v not in (None, "", [], {})]
    return str(value)


def _history_safe_topic_url(url: object) -> str:
    text = str(url or "").strip()
    lowered = text.lower()
    if not text or lowered.startswith("magnet:"):
        return ""
    if "apikey" in lowered or "api_key" in lowered or "/dl/" in lowered:
        return ""
    return text


def _history_fields_from_result(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}

    topic_url = _history_safe_topic_url(result.get("url"))
    if not topic_url:
        topic_url = _history_safe_topic_url(result.get("topic_url"))
    topic_id = str(result.get("topic_id") or "")
    if not topic_id and topic_url:
        topic_id = _extract_rutracker_topic_id(topic_url)

    fields: dict = {
        "title": result.get("title") or "",
        "canonical_title": result.get("movie_title") or "",
        "source": result.get("source") or "",
        "tracker": result.get("tracker_name") or result.get("tracker") or result.get("category") or "",
        "indexer": result.get("indexer") or result.get("tracker_name") or "",
        "topic_id": topic_id,
        "topic_url": topic_url,
        "quality": result.get("quality") or _plex_quality_from_result(result),
    }
    for key in ("year", "size", "size_bytes", "seeders", "leechers"):
        if result.get(key) not in (None, ""):
            fields[key] = result.get(key)

    parsed = result.get("parsed_meta")
    if isinstance(parsed, dict):
        release = {
            key: parsed.get(key)
            for key in ("quality", "source", "hdr", "audio", "langs", "release_group", "edition")
            if parsed.get(key) not in (None, "", [], {})
        }
        if release:
            fields["release"] = release
            if not fields.get("quality") and release.get("quality"):
                fields["quality"] = release["quality"]

    return {k: _history_jsonable(v) for k, v in fields.items() if v not in (None, "", [], {})}


def _history_fields_from_meta(meta: dict | None) -> dict:
    if not isinstance(meta, dict):
        return {}
    fields = {
        "kind": meta.get("kind"),
        "canonical_title": meta.get("title"),
        "year": meta.get("year"),
        "quality": meta.get("quality"),
        "series_query": meta.get("series_query"),
        "season": meta.get("season_num"),
        "meta_source": meta.get("source"),
        "release": meta.get("release"),
    }
    return {k: _history_jsonable(v) for k, v in fields.items() if v not in (None, "", [], {}, -1)}


def _history_fields_from_task(task: dict | None) -> dict:
    if not isinstance(task, dict):
        return {}
    additional = task.get("additional") if isinstance(task.get("additional"), dict) else {}
    transfer = additional.get("transfer") if isinstance(additional.get("transfer"), dict) else {}
    downloaded = transfer.get("size_downloaded")
    size = task.get("size")
    fields = {
        "title": task.get("title") or "",
        "ds_status": task.get("status") or "",
        "task_type": task.get("type") or "",
        "size": size,
        "downloaded": downloaded,
        "progress_percent": _progress_percent(downloaded, size),
    }
    status_extra = task.get("status_extra") if isinstance(task.get("status_extra"), dict) else {}
    error_detail = status_extra.get("error_detail")
    if not error_detail:
        detail = additional.get("detail") if isinstance(additional.get("detail"), dict) else {}
        error_detail = detail.get("error_detail")
    if error_detail:
        fields["error_detail"] = error_detail
    return {k: _history_jsonable(v) for k, v in fields.items() if v not in (None, "", [], {})}


def _record_download_history(
    event: str,
    *,
    chat_id: int | None = None,
    chat_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    task_id: str = "",
    result: dict | None = None,
    meta: dict | None = None,
    task: dict | None = None,
    **extra,
) -> None:
    entry: dict = {
        "ts": datetime.now(DISPLAY_TIMEZONE).isoformat(timespec="seconds"),
        "event": event,
    }
    if chat_id is not None:
        try:
            entry["chat_id"] = int(chat_id)
        except (TypeError, ValueError):
            pass
    if chat_ids:
        ids: list[int] = []
        for raw_id in chat_ids:
            try:
                ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        if ids:
            entry["chat_ids"] = sorted(set(ids))
            if "chat_id" not in entry and len(set(ids)) == 1:
                entry["chat_id"] = ids[0]
    if task_id:
        entry["task_id"] = str(task_id)

    entry.update(_history_fields_from_result(result))
    entry.update(_history_fields_from_meta(meta))
    entry.update(_history_fields_from_task(task))
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            entry[key] = _history_jsonable(value)

    append = getattr(state_store, "append_download_history", None)
    if not callable(append):
        return
    try:
        append(entry)
    except Exception:
        logger.warning("Failed to record download history event=%s", event, exc_info=True)


def _record_download_added_history(
    task_id: str,
    chat_id: int | None,
    result: dict | None,
    *,
    method: str,
    meta_source: str,
    subscribe: bool = False,
    notify_policy: str | None = None,
    download_policy: str | None = None,
) -> None:
    meta = _build_task_meta_from_result(result, source=meta_source) if isinstance(result, dict) else None
    _record_download_history(
        "download_added",
        chat_id=chat_id,
        task_id=task_id,
        result=result,
        meta=meta,
        method=method,
        subscribe=subscribe,
        notify_policy=notify_policy,
        download_policy=download_policy,
    )


def _record_download_added_from_title_history(
    task_id: str,
    chat_id: int | None,
    title: str,
    *,
    method: str,
    meta_source: str,
    meta: dict | None = None,
) -> None:
    _record_download_history(
        "download_added",
        chat_id=chat_id,
        task_id=task_id,
        meta=meta or (_build_task_meta_from_title(title, source=meta_source) if title else None),
        title=title,
        method=method,
    )


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
    """Resolve who should receive a notification for ``task_id``.

    Primary path delegates to ``task_policies.notification_recipients`` which
    walks: explicit NOTIFY_CHAT_IDS → task_owners → fallback admins (if
    ``TASK_NOTIFY_EXTERNAL_TASKS=true``).

    Self-healing fallback: if the primary path returns an empty set but the
    task has a registered card in ``TASK_CARD_MESSAGES``, the chat_id(s)
    displaying that card are implicitly the owner — they clicked «Скачать»
    and are waiting for a result. This catches cases where the on-disk
    ``task_owners.json`` record was lost (mid-write crash, manual JSON edit,
    state pruning that ran too aggressively). Recovered chat_ids are
    filtered through ``_all_allowed_chat_ids()`` so we never notify a
    non-authorised user even if the card got registered with one.
    """
    recipients = _policy_notification_recipients(
        task_id,
        explicit_chat_ids=_explicit_notification_chat_ids(),
        task_owners=_load_task_owners(),
        notify_external_tasks=TASK_NOTIFY_EXTERNAL_TASKS,
        fallback_chat_ids=_all_allowed_chat_ids(),
        allowed_chat_ids=_all_allowed_chat_ids(),
    )
    if recipients:
        return recipients

    card_chat_ids = {chat_id for chat_id, _ in TASK_CARD_MESSAGES.get(str(task_id), set())}
    if not card_chat_ids:
        return recipients
    allowed = _all_allowed_chat_ids()
    if allowed:
        card_chat_ids = {c for c in card_chat_ids if c in allowed}
    if card_chat_ids:
        logger.info(
            "Task notification recipients recovered from task-card registry: "
            "task=%s chat_ids=%s",
            task_id, sorted(card_chat_ids),
        )
    return card_chat_ids


def _notification_status_key(status: str) -> str:
    return _policy_notification_status_key(status)


def _is_complete_despite_error(task: dict) -> bool:
    return _policy_is_complete_despite_error(task)


def _download_history_event_for_status(
    status: str,
    notification_status: str,
    complete_despite_error: bool,
) -> str:
    if complete_despite_error:
        return "download_soft_completed"
    if notification_status in {"finished", "seeding"}:
        return "download_completed"
    if status == "error":
        return "download_failed"
    return ""


def _record_task_notification_history(
    task: dict,
    *,
    notification_status: str,
    complete_despite_error: bool,
    recipients: set[int],
    plex_polling_started: bool,
) -> None:
    task_id = str(task.get("id") or "")
    event = _download_history_event_for_status(
        str(task.get("status") or "").lower(),
        notification_status,
        complete_despite_error,
    )
    if not task_id or not event:
        return
    owner_id = _task_owner(task_id)
    _record_download_history(
        event,
        chat_id=owner_id,
        chat_ids=recipients,
        task_id=task_id,
        task=task,
        meta=_get_task_meta(task_id),
        notification_status=notification_status,
        soft_completed=complete_despite_error,
        plex_polling_started=plex_polling_started,
    )


def _auto_delete_notice(status: str) -> str:
    return _policy_auto_delete_notice(
        status,
        enabled=_auto_delete_finished_enabled(),
        finished_statuses=AUTO_DELETE_FINISHED_STATUSES,
        delete_after_hours=AUTO_DELETE_FINISHED_AFTER_HOURS,
    )


def _format_task_notification(task: dict, *, plex_polling_started: bool = False) -> str:
    return _policy_format_task_notification(
        task,
        auto_delete_enabled=_auto_delete_finished_enabled(),
        auto_delete_statuses=AUTO_DELETE_FINISHED_STATUSES,
        auto_delete_after_hours=AUTO_DELETE_FINISHED_AFTER_HOURS,
        plex_polling_started=plex_polling_started,
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
    raw_state: object | None = None,
) -> dict:
    entry = {
        "status": notification_key,
        "sent": sorted(sent),
        "failures": {
            chat_id: count
            for chat_id, count in sorted(failures.items())
            if count > 0
        },
    }
    if isinstance(raw_state, dict):
        subscribers = {
            str(chat_id)
            for chat_id in raw_state.get("subscribers", [])
            if chat_id
        }
        subscribers -= sent
        if subscribers:
            entry["subscribers"] = sorted(subscribers)
        if raw_state.get("plex_done"):
            entry["plex_done"] = True
    return entry


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
                [InlineKeyboardButton(BUTTON_SHOW_TASK, callback_data=_task_callback("info", orig_id))],
                [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
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
                    InlineKeyboardButton(BUTTON_SHOW_TASK, callback_data=_task_callback("info", orig_id)),
                    InlineKeyboardButton("🔔 Уведомить когда готово", callback_data=_task_callback("sub_notify", orig_id)),
                ],
                [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
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
                    InlineKeyboardButton(BUTTON_SHOW_TASK, callback_data=_task_callback("info", orig_id)),
                    InlineKeyboardButton("▶️ Запустить", callback_data=_task_callback("resume", orig_id)),
                ],
                [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
            ])
    else:
        # Original not found — it may have been deleted already.
        text = (
            f"📌 Такой торрент уже был добавлен ранее\n\n"
            f"🎬 {dup_title}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
        ])

    try:
        await app.bot.send_message(chat_id=owner_id, text=text, reply_markup=kb)
    except Exception:
        logger.warning("Failed to send duplicate notification to %s", owner_id, exc_info=True)


async def _classify_send_error(exc: Exception) -> tuple[str, bool]:
    """Classify a Telegram ``send_message`` exception for the notification loop.

    Returns ``(label, is_permanent)``:

    - ``label`` — short tag for logging (``rate_limit``, ``timeout``, ``network``,
      ``blocked``, ``chat_not_found``, ``permanent``).
    - ``is_permanent`` — True if the error should count against the per-chat
      ``MAX_TASK_NOTIFICATION_FAILURES`` threshold; False for transient errors
      that should be retried on the next cycle **without** penalty.

    Side effect: for ``RetryAfter`` we honour the server's hint by awaiting
    ``asyncio.sleep(retry_after)`` so the next send in this cycle isn't blocked
    immediately. Capped at 30 s as a safety bound.

    Order matters: ``TimedOut`` inherits from ``NetworkError`` in
    python-telegram-bot; check it first.
    """
    if isinstance(exc, RetryAfter):
        sleep_for = min(float(getattr(exc, "retry_after", 1) or 1), 30.0)
        await asyncio.sleep(sleep_for)
        return "rate_limit", False
    if isinstance(exc, Forbidden):
        # User blocked the bot or left the chat — permanent.
        return "blocked", True
    # IMPORTANT: BadRequest is a subclass of NetworkError in python-telegram-bot —
    # check it BEFORE NetworkError so "chat not found" / "user is deactivated"
    # are classified as permanent (not retried as transient).
    if isinstance(exc, BadRequest):
        # Discriminate: some BadRequests are about the message FORMAT (our bug),
        # not about the chat. Penalising chat_id failures for those would let a
        # malformed message blackhole a healthy chat permanently — exactly the
        # plex:// regression in May 2026.
        msg = str(exc).lower()
        format_bug_markers = (
            "inline keyboard button",
            "button_url_invalid",
            "url is invalid",
            "unsupported url protocol",
            "can't parse entities",
            "message text is empty",
        )
        if any(marker in msg for marker in format_bug_markers):
            # Format bug — do NOT count against the chat. Same content will
            # keep failing on every cycle, but ERROR-level log makes it loudly
            # operator-visible so the underlying code bug gets fixed quickly.
            return "message_format_bug", False
        return "chat_not_found", True
    if isinstance(exc, TimedOut):
        return "timeout", False
    if isinstance(exc, NetworkError):
        # NetworkError covers DNS failure, ConnectionReset, BadGateway, etc.
        return "network", False
    # Unknown — treat as permanent so we don't busy-retry forever on our bugs.
    return "permanent", True


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
        complete_despite_error = _is_complete_despite_error(task)
        notification_status = "finished" if complete_despite_error else status
        if not task_id or (
            status not in TASK_NOTIFICATION_STATUSES
            and notification_status not in TASK_NOTIFICATION_STATUSES
        ):
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
                _record_download_history(
                    "download_failed",
                    chat_id=owner_id,
                    task_id=task_id,
                    task=task,
                    meta=_get_task_meta(task_id),
                    error_detail="torrent_duplicate",
                )
                changed = True
            continue

        notification_key = _notification_status_key(notification_status)
        raw_notification_state = notified.get(task_id)
        history_already_recorded = (
            raw_notification_state == notification_key
            or (
                isinstance(raw_notification_state, dict)
                and raw_notification_state.get("status") == notification_key
            )
        )
        sent_recipients, failed_recipients, legacy_done = _notification_delivery_state(
            raw_notification_state,
            notification_key,
        )
        if legacy_done:
            logger.info(
                "Task notification skipped task=%s status=%s key=%s: legacy_done "
                "(plain-string state from old format, treated as already delivered)",
                task_id, status, notification_key,
            )
            continue

        recipients = _notification_recipients(task_id)

        # Also notify any users who subscribed via "🔔 Уведомить когда готово".
        if notification_status in {"finished", "seeding"}:
            raw_state = notified.get(task_id)
            if isinstance(raw_state, dict):
                for sub_id_str in raw_state.get("subscribers", []):
                    try:
                        recipients.add(int(sub_id_str))
                    except (TypeError, ValueError):
                        pass

        if not recipients:
            owners_now = _load_task_owners() or {}
            logger.info(
                "Task notification skipped task=%s status=%s key=%s: no recipients "
                "(owner_in_json=%s, external_enabled=%s, explicit_count=%s, "
                "task_card_registered=%s)",
                task_id, status, notification_key,
                task_id in owners_now,
                TASK_NOTIFY_EXTERNAL_TASKS,
                len(_explicit_notification_chat_ids()),
                bool(TASK_CARD_MESSAGES.get(str(task_id))),
            )
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
            and notification_status in {"finished", "seeding"}
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
                logger.debug(
                    "Recipient already notified task=%s chat=%s key=%s",
                    task_id, chat_id, notification_key,
                )
                continue
            if failed_recipients.get(recipient_key, 0) >= MAX_TASK_NOTIFICATION_FAILURES:
                logger.info(
                    "Recipient skipped (failures cap) task=%s chat=%s failures=%s/%s key=%s",
                    task_id, chat_id, failed_recipients[recipient_key],
                    MAX_TASK_NOTIFICATION_FAILURES, notification_key,
                )
                continue

            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=_format_task_notification(task, plex_polling_started=plex_should_poll),
                    reply_markup=_notification_keyboard(task_id, notification_status, task.get("type", "")),
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
                            text="🔄 Ищем файл в библиотеке Plex — сообщим, как только появится.",
                        )
                        plex_hint_msgs[chat_id] = hint.message_id
                    except Exception:
                        pass
            except Exception as exc:
                label, is_permanent = await _classify_send_error(exc)
                if is_permanent:
                    failure_count = failed_recipients.get(recipient_key, 0) + 1
                    failed_recipients[recipient_key] = failure_count
                    task_changed = True
                    logger.warning(
                        "Task notification failed (permanent: %s) chat_id=%s task_id=%s attempt=%s/%s",
                        label, chat_id, task_id, failure_count, MAX_TASK_NOTIFICATION_FAILURES,
                        exc_info=True,
                    )
                else:
                    # Transient — retry on next cycle without penalty. We do NOT
                    # set task_changed, so the per-chat state isn't rewritten
                    # and the failure counter stays at its previous value.
                    # Message-format bugs are our own code defect: ERROR level so
                    # they don't hide in a sea of INFO entries.
                    log_level = logging.ERROR if label == "message_format_bug" else logging.INFO
                    logger.log(
                        log_level,
                        "Task notification deferred (transient: %s) chat_id=%s task_id=%s — will retry",
                        label, chat_id, task_id,
                    )

        if task_changed:
            notified[task_id] = _make_notification_delivery_state(
                notification_key,
                sent_recipients,
                failed_recipients,
                notified.get(task_id),
            )
            if not history_already_recorded:
                _record_task_notification_history(
                    task,
                    notification_status=notification_status,
                    complete_despite_error=complete_despite_error,
                    recipients=recipients,
                    plex_polling_started=plex_should_poll,
                )
            # Persist after each task so a crash mid-cycle loses at most one
            # task's worth of state (instead of the whole cycle, which would
            # cause duplicate notifications on restart).
            _save_notified_tasks(notified)
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


# ---------------------------------------------------------------------------
# Pending download queue: re-try failed Jackett/Rutracker downloads in the
# background until success or TTL expiry.
# ---------------------------------------------------------------------------


async def _attempt_pending_download(entry: dict) -> tuple[str, str]:
    """Try to deliver a queued download. Returns (task_id, method) on success.

    Walks the same chain as the interactive path:
      1. Jackett proxy (if entry.torrent_url and jackett_client available)
      2. rutracker_client direct (for Rutracker results)
      3. Magnet (if entry.magnet_url is set — only meaningful for public trackers)

    Raises ``JackettError``/``RutrackerError``/``DownloadStationError``/``RuntimeError``
    when none of the paths succeeds — caller increments attempts and stores the
    last_error message.
    """
    title = str(entry.get("title") or "untitled")
    safe_name = _safe_filename(f"{title}.torrent")
    temp_path = _temp_path(safe_name)
    tracker = (entry.get("tracker") or "").lower()
    topic_url = entry.get("topic_url") or ""
    torrent_url = entry.get("torrent_url") or ""
    magnet_url = entry.get("magnet_url") or ""

    last_err: Exception | None = None
    try:
        # Step 1: Jackett proxy
        if torrent_url and jackett_client is not None:
            try:
                torrent_bytes = await asyncio.to_thread(
                    jackett_client.download_torrent, torrent_url
                )
                temp_path.write_bytes(torrent_bytes)
                task_id = await asyncio.to_thread(
                    ds_client.create_torrent_file, temp_path, safe_name
                )
                if not task_id:
                    raise _missing_task_id_error("для torrent-файла")
                return task_id, "torrent-файл"
            except JackettMagnetRedirect as e:
                magnet = e.magnet_url or magnet_url
                if magnet:
                    task_id = await asyncio.to_thread(ds_client.create_magnet, magnet)
                    if not task_id:
                        task_id = await _wait_for_magnet_task_id(magnet, set(), None)
                    return task_id, "magnet"
                last_err = e
            except JackettError as e:
                last_err = e  # fall through to next step

        # Step 2: rutracker_client direct
        if rutracker_client is not None and "rutracker" in tracker:
            topic_id = _extract_rutracker_topic_id(topic_url)
            if topic_id:
                try:
                    torrent_bytes = await asyncio.to_thread(
                        rutracker_client.download_torrent, topic_id
                    )
                    temp_path.write_bytes(torrent_bytes)
                    task_id = await asyncio.to_thread(
                        ds_client.create_torrent_file, temp_path, safe_name
                    )
                    if not task_id:
                        raise _missing_task_id_error("для torrent-файла (Rutracker direct)")
                    return task_id, "torrent-файл (Rutracker direct)"
                except RutrackerError as e:
                    last_err = e

        # Step 3: magnet
        if magnet_url:
            task_id = await asyncio.to_thread(ds_client.create_magnet, magnet_url)
            if not task_id:
                task_id = await _wait_for_magnet_task_id(magnet_url, set(), None)
            return task_id, "magnet"

        # Nothing worked.
        if last_err is not None:
            raise last_err
        raise RuntimeError("Нет источников для скачивания (torrent_url / magnet_url отсутствуют)")
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


async def _notify_pending_success(
    app: Application, entry: dict, task_id: str, method: str,
) -> None:
    chat_id = entry.get("chat_id")
    if not chat_id:
        return
    title = entry.get("title") or "загрузка"

    # If the original action was «⬇️📺 Серии» / «⬇️🎯 Сезон» (subscribe=True),
    # recreate the subscription now that the download actually succeeded.
    # Without this, queueing a series after a download failure would silently
    # downgrade to a one-shot — the user's original intent is lost.
    subscribe_restored = False
    if entry.get("subscribe"):
        notify_policy, download_policy = _coerce_subscription_policies(
            entry.get("notify_policy"), entry.get("download_policy")
        )
        source = str(entry.get("source") or "")
        try:
            if source == "jackett":
                sub_key = f"jackett:{uuid.uuid4().hex[:8]}"
                subs = state_store.load_topic_subscriptions()
                now_text = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                subs[sub_key] = build_jackett_subscription(
                    chat_id=int(chat_id),
                    query=entry.get("title") or "",
                    result=_pending_entry_to_search_result(entry),
                    seen_results=[],
                    added_at=now_text,
                    notify_policy=notify_policy,
                    download_policy=download_policy,
                )
                state_store.save_topic_subscriptions(subs)
                subscribe_restored = True
                logger.info(
                    "Pending-success: restored Jackett subscription key=%s policy=%s/%s",
                    sub_key, subs[sub_key].get("notify_policy"),
                    subs[sub_key].get("download_policy"),
                )
            else:
                # Rutracker subscription needs an episode-info-bearing title.
                topic_id = _extract_rutracker_topic_id(entry.get("topic_url") or "")
                episode_info = _parse_episode_info(title)
                if topic_id and episode_info:
                    subs = state_store.load_topic_subscriptions()
                    new_sub = {
                        "chat_id": int(chat_id),
                        "title": title,
                        "last_episode_end": episode_info[0],
                        "total_episodes": episode_info[1],
                        "added_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                        "notify_policy": notify_policy,
                        "download_policy": download_policy,
                    }
                    subs[topic_id] = new_sub
                    state_store.save_topic_subscriptions(subs)
                    subscribe_restored = True
                    logger.info(
                        "Pending-success: restored Rutracker subscription topic=%s policy=%s/%s",
                        topic_id, new_sub.get("notify_policy"),
                        new_sub.get("download_policy"),
                    )
        except Exception:  # noqa: BLE001 — subscription restore is best-effort
            logger.warning(
                "Pending-success: failed to restore subscription for %s",
                title, exc_info=True,
            )

    text = (
        f"✅ Отложенная загрузка стартовала: «{title}».\n"
        f"Метод: {method}."
    )
    if task_id:
        text += " Слежу за прогрессом."
    else:
        text += f"\n⚠️ {_magnet_without_task_id_note()}"
    if entry.get("subscribe") and subscribe_restored:
        text += "\n🔔 Подписка восстановлена — слежу за новыми сериями."
    elif entry.get("subscribe") and not subscribe_restored:
        text += (
            "\n⚠️ Подписка не была восстановлена автоматически — "
            "добавьте её вручную через поиск."
        )
    try:
        await app.bot.send_message(chat_id=int(chat_id), text=text)
    except Exception:
        logger.warning("Failed to notify pending-success for chat_id=%s", chat_id, exc_info=True)
    if task_id:
        _remember_task_owner(task_id, int(chat_id))
        result = _pending_entry_to_search_result(entry)
        _remember_task_meta(task_id, _build_task_meta_from_result(result, source="pending"))
        _record_download_added_history(
            task_id,
            int(chat_id),
            result,
            method=method,
            meta_source="pending",
            subscribe=bool(entry.get("subscribe")),
            notify_policy=entry.get("notify_policy"),
            download_policy=entry.get("download_policy"),
        )


async def _notify_pending_dropped(app: Application, entry: dict) -> None:
    chat_id = entry.get("chat_id")
    if not chat_id:
        return
    title = entry.get("title") or "загрузка"
    attempts = int(entry.get("attempts") or 0)
    last_error = _stored_error_user_text(entry.get("last_error"))
    ttl_h = PENDING_DOWNLOADS_TTL_HOURS
    text = (
        f"⌛ Не удалось скачать «{title}» за {ttl_h:g}ч ({attempts} попыток).\n"
        f"Последняя ошибка: {last_error}.\n"
        "Попробуйте найти раздачу заново."
    )
    try:
        await app.bot.send_message(chat_id=int(chat_id), text=text)
    except Exception:
        logger.warning("Failed to notify pending-dropped for chat_id=%s", chat_id, exc_info=True)


def _stored_error_user_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return "не получилось добавить загрузку"
    lower = text.lower()
    if "no space" in lower or "not enough space" in lower:
        return "на диске недостаточно места"
    technical_markers = (
        "http ",
        "url:",
        "path=",
        "traceback",
        "exception",
        "stack trace",
        "client error",
        "server error",
        "dsm api",
        "errno",
    )
    if any(marker in lower for marker in technical_markers):
        return "сервис загрузки или трекер временно не отдал файл"
    return text


def _pending_series_bulk_meta(entry: dict) -> tuple[str, int] | None:
    meta = entry.get("series_bulk")
    if not isinstance(meta, dict):
        return None
    job_id = str(meta.get("job_id") or "")
    try:
        season = int(meta.get("season"))
    except (TypeError, ValueError):
        season = 0
    if not job_id or season <= 0:
        return None
    return job_id, season


def _series_bulk_job_status_from_entries(job: dict) -> str:
    seasons = job.get("seasons")
    if not isinstance(seasons, dict):
        return str(job.get("status") or "planned")
    statuses = {
        str(entry.get("runtime_status") or "")
        for entry in seasons.values()
        if isinstance(entry, dict)
    }
    has_pending = "pending_retry" in statuses
    has_failed = bool(statuses & {
        "failed",
        "pending_failed",
        "partial_downloaded_subscription_failed",
    })
    has_downloaded = bool(statuses & {"downloaded", "pack_downloaded"})
    if has_pending:
        return "batch_completed_with_errors" if has_failed else "batch_completed_with_pending"
    if has_failed:
        return "batch_completed_with_errors" if has_downloaded else "batch_failed"
    if has_downloaded:
        return "batch_completed"
    return str(job.get("status") or "planned")


def _series_bulk_record_pending_retry_result(
    entry: dict,
    runtime_status: str,
    *,
    task_id: str = "",
    method: str = "",
    error: str = "",
    summary: str = "",
) -> None:
    meta = _pending_series_bulk_meta(entry)
    if meta is None:
        return
    job_id, season = meta
    jobs = state_store.load_series_bulk_jobs()
    job = jobs.get(job_id)
    if not isinstance(job, dict):
        logger.info("Series bulk pending retry: job not found job_id=%s season=%s", job_id, season)
        return
    seasons = job.setdefault("seasons", {})
    if not isinstance(seasons, dict):
        seasons = {}
        job["seasons"] = seasons
    season_entry = seasons.setdefault(str(season), {"season": season})
    if not isinstance(season_entry, dict):
        season_entry = {"season": season}
        seasons[str(season)] = season_entry

    season_entry["runtime_status"] = runtime_status
    season_entry["season"] = season
    if task_id:
        season_entry["task_id"] = str(task_id)
    if method:
        season_entry["method"] = str(method)
    if summary:
        season_entry["summary"] = str(summary)
    if error:
        season_entry["error"] = str(error)
    else:
        season_entry.pop("error", None)
    season_entry["result"] = _series_bulk_result_snapshot(
        _pending_entry_to_search_result(entry)
    )
    season_entry.pop("pending_entry_id", None)
    job["updated_at"] = _series_bulk_job_now()
    job["status"] = _series_bulk_job_status_from_entries(job)
    state_store.save_series_bulk_jobs(jobs)


async def _run_pending_downloads_once(app: Application) -> None:
    """One pass over the pending queue: retry each entry, drop expired ones."""
    if not _pending_downloads_enabled():
        return
    pending = _load_pending_downloads()
    if not pending:
        return

    now = datetime.now(DISPLAY_TIMEZONE)
    ttl = timedelta(hours=PENDING_DOWNLOADS_TTL_HOURS)
    changed = False

    for entry_id, entry in list(pending.items()):
        added_at_str = str(entry.get("added_at") or "")
        try:
            added_at = datetime.fromisoformat(added_at_str)
        except ValueError:
            added_at = now  # malformed → treat as just-added (give it a chance)
        if now - added_at > ttl:
            await _notify_pending_dropped(app, entry)
            _series_bulk_record_pending_retry_result(
                entry,
                "pending_failed",
                error=_stored_error_user_text(entry.get("last_error") or "истёк срок отложенных попыток"),
                summary="отложенный повтор не сработал",
            )
            del pending[entry_id]
            changed = True
            continue

        try:
            task_id, method = await _attempt_pending_download(entry)
        except Exception as exc:
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["last_attempt_at"] = now.isoformat()
            entry["last_error"] = _format_download_error(exc)[:200]
            logger.info(
                "Pending download retry failed: id=%s attempts=%s err=%s",
                entry_id, entry["attempts"], entry["last_error"],
            )
            changed = True
            continue

        # Success — notify and drop.
        logger.info("Pending download succeeded: id=%s task_id=%s method=%s",
                    entry_id, task_id, method)
        await _notify_pending_success(app, entry, task_id, method)
        _series_bulk_record_pending_retry_result(
            entry,
            "downloaded",
            task_id=task_id or "",
            method=method,
            summary=f"скачан после отложенного повтора: {task_id or method}",
        )
        del pending[entry_id]
        changed = True

    if changed:
        _save_pending_downloads(pending)


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
    # Sticky owner: ensure task_owners.json records this chat_id as the task's
    # owner. _remember_task_owner is idempotent (see state_store L185-189), so a
    # repeat call is a no-op without I/O. This is a safety net — every spot in
    # the codebase that creates a task should already call _remember_task_owner
    # explicitly, but if any of them silently fails (mid-write crash, empty
    # task_id from a magnet poll), the active task-card guarantees recovery.
    _remember_task_owner(str(task_id), chat_id)


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
                    text=_format_task_card(task, chat_id),
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
    accepted_without_task_id: bool = False,
) -> str:
    intro = (
        "✅ Magnet отправлен в очередь скачивания"
        if accepted_without_task_id
        else "✅ Задача добавлена в очередь скачивания"
    )
    lines = [intro]

    if title:
        lines.append("")
        label = "Раздача" if task_id else "Имя"
        lines.append(f"{label}: {title}")
    if task_id:
        next_line = (
            "статус будет обновляться автоматически. Когда загрузка завершится, "
            "бот сообщит об этом, затем проверит Plex и сообщит, когда файл появится в библиотеке."
            if PLEX_ENABLED
            else "статус будет обновляться автоматически. Когда загрузка завершится, бот сообщит об этом."
        )
        lines.extend([
            "",
            "Что дальше:",
            next_line,
        ])

    tracker_lines = _download_added_tracker_lines(tracker_result)
    if tracker_lines:
        lines.extend(["", *tracker_lines])

    return "\n".join(lines)


class MissingTaskIdError(DownloadStationError):
    """Download Station accepted a create request but exposed no task id."""


def _missing_task_id_error(method: str) -> MissingTaskIdError:
    return MissingTaskIdError(
        f"Download Station принял запрос {method}, но не вернул ID задачи."
    )


def _magnet_without_task_id_note() -> str:
    return (
        "Ссылка принята, но бот пока не видит созданную задачу. "
        "Иногда Download Station показывает такие задачи с задержкой.\n\n"
        "Что можно сделать:\n"
        "через минуту откройте список загрузок и нажмите «Обновить». "
        "Если задачи там нет, проверьте Download Station или попробуйте добавить magnet ещё раз."
    )


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


async def _delete_command_message_safely(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    label: str = "command",
) -> None:
    chat = update.effective_chat
    message = update.effective_message
    message_id = getattr(message, "message_id", None)
    if not chat or not isinstance(message_id, int):
        return
    await _delete_message_safely(context, chat.id, message_id, label)


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

        if progress_message is not None:
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


def _default_search_setting_sources() -> dict[str, str]:
    return {"quality": "default", "audio": "default", "subs": "default"}


def _search_setting_source(context: ContextTypes.DEFAULT_TYPE, key: str) -> str:
    sources = context.user_data.get("srch_setting_sources")
    if isinstance(sources, dict) and sources.get(key) in {"default", "explicit"}:
        return str(sources[key])
    return "explicit"


def _mark_search_setting_source(context: ContextTypes.DEFAULT_TYPE, key: str, source: str) -> None:
    sources = context.user_data.setdefault("srch_setting_sources", _default_search_setting_sources())
    if isinstance(sources, dict):
        sources[key] = source


def _normalise_preferred_voices(values) -> list[str]:
    allowed = {label.lower(): label for label in KNOWN_VOICE_LABELS}
    voices: list[str] = []
    raw_values = values if isinstance(values, (list, tuple, set)) else []
    for value in raw_values:
        label = allowed.get(str(value).strip().lower())
        if label and label not in voices:
            voices.append(label)
    return voices[:2]


def _global_search_defaults() -> dict:
    defaults = dict(_SRCH_DEFAULT_SETTINGS)
    defaults["preferred_voices"] = []
    return defaults


def _search_defaults_for_chat(chat_id: int | None) -> dict:
    defaults = _global_search_defaults()
    if chat_id is None:
        return defaults
    try:
        personal = state_store.load_user_search_defaults(int(chat_id))
    except Exception:
        logger.warning("Failed to load user search defaults chat_id=%s", chat_id, exc_info=True)
        return defaults
    if not isinstance(personal, dict):
        return defaults
    quality = personal.get("quality")
    if quality in {"4K", "1080p", "720p", "any"}:
        defaults["quality"] = quality
    defaults["audio"] = bool(personal.get("audio", defaults.get("audio", False)))
    defaults["subs"] = bool(personal.get("subs", defaults.get("subs", False)))
    defaults["preferred_voices"] = _normalise_preferred_voices(personal.get("preferred_voices"))
    return defaults


def _search_settings_for_chat(chat_id: int | None) -> dict:
    defaults = _search_defaults_for_chat(chat_id)
    return {
        "quality": defaults.get("quality", "1080p"),
        "audio": bool(defaults.get("audio", False)),
        "subs": bool(defaults.get("subs", False)),
    }


def _save_search_defaults(chat_id: int, defaults: dict) -> None:
    state_store.save_user_search_defaults(chat_id, {
        "quality": defaults.get("quality", "1080p"),
        "audio": bool(defaults.get("audio", False)),
        "subs": bool(defaults.get("subs", False)),
        "preferred_voices": _normalise_preferred_voices(defaults.get("preferred_voices")),
    })


def _quality_label(value: str) -> str:
    return {"4K": "4K", "1080p": "1080p", "720p": "720p", "any": "любое"}.get(value, "1080p")


def _settings_voice_label(voices: list[str]) -> str:
    return f"предпочитаю {' / '.join(voices[:2])}" if voices else "без предпочтений"


def _settings_text(defaults: dict) -> str:
    voices = _normalise_preferred_voices(defaults.get("preferred_voices"))
    return "\n".join([
        "⚙️ Предпочтения поиска",
        "",
        "Учитываю в новых поисках как пожелания. Если точного варианта нет, покажу альтернативы.",
        "",
        f"• Качество: предпочитаю {_quality_label(str(defaults.get('quality') or '1080p'))}",
        f"• Original: {'предпочитаю' if defaults.get('audio') else 'не важно'}",
        f"• Субтитры: {'предпочитаю' if defaults.get('subs') else 'не важно'}",
        f"• Переводы: {_settings_voice_label(voices)}",
    ])


def _settings_keyboard(defaults: dict, *, voices_expanded: bool = False) -> InlineKeyboardMarkup:
    quality = str(defaults.get("quality") or "1080p")
    audio = bool(defaults.get("audio", False))
    subs = bool(defaults.get("subs", False))
    voice_list = _normalise_preferred_voices(defaults.get("preferred_voices"))
    voices = set(voice_list)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            f"🎞 Качество: предпочитаю {_quality_label(quality)}",
            callback_data=f"{SETTINGS_CALLBACK_PREFIX}:quality",
        )],
        [InlineKeyboardButton(
            f"🎧 Original: {'предпочитаю' if audio else 'не важно'}",
            callback_data=f"{SETTINGS_CALLBACK_PREFIX}:audio",
        )],
        [InlineKeyboardButton(
            f"💬 Субтитры: {'предпочитаю' if subs else 'не важно'}",
            callback_data=f"{SETTINGS_CALLBACK_PREFIX}:subs",
        )],
        [InlineKeyboardButton(
            f"🎙 Переводы: {_settings_voice_label(voice_list)}",
            callback_data=f"{SETTINGS_CALLBACK_PREFIX}:voices",
        )],
    ]
    if voices_expanded:
        for idx, label in enumerate(KNOWN_VOICE_LABELS):
            mark = "☑️" if label in voices else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} {label}",
                callback_data=f"{SETTINGS_CALLBACK_PREFIX}:voice:{idx}",
            )])
        if voices:
            rows.append([InlineKeyboardButton(
                "🚫 Без предпочтений",
                callback_data=f"{SETTINGS_CALLBACK_PREFIX}:voices_clear",
            )])
    rows.extend([
        [InlineKeyboardButton(
            "↩️ Вернуть стартовые предпочтения",
            callback_data=f"{SETTINGS_CALLBACK_PREFIX}:reset",
        )],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])
    return InlineKeyboardMarkup(rows)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return
    chat_id = _chat_id(update)
    defaults = _search_defaults_for_chat(chat_id)
    msg = await update.message.reply_text(
        _settings_text(defaults),
        reply_markup=_settings_keyboard(defaults),
    )
    context.user_data["settings_ui_msg_id"] = msg.message_id
    context.user_data["settings_ui_chat_id"] = chat_id
    await _delete_command_message_safely(update, context, "settings command")


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = _chat_id_from_query(query)
    if not isinstance(chat_id, int) or not _is_allowed(update):
        await _safe_answer(query)
        return
    defaults = _search_defaults_for_chat(chat_id)
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    voices_expanded = bool(context.user_data.get("settings_voices_expanded"))

    if action == "quality":
        order = ["4K", "1080p", "720p", "any"]
        current = str(defaults.get("quality") or "1080p")
        defaults["quality"] = order[(order.index(current) + 1) % len(order)] if current in order else "1080p"
    elif action == "audio":
        defaults["audio"] = not bool(defaults.get("audio", False))
    elif action == "subs":
        defaults["subs"] = not bool(defaults.get("subs", False))
    elif action == "voices":
        voices_expanded = not voices_expanded
        context.user_data["settings_voices_expanded"] = voices_expanded
    elif action == "voice" and len(parts) > 2:
        try:
            idx = int(parts[2])
        except ValueError:
            idx = -1
        if 0 <= idx < len(KNOWN_VOICE_LABELS):
            voices = _normalise_preferred_voices(defaults.get("preferred_voices"))
            label = KNOWN_VOICE_LABELS[idx]
            if label in voices:
                voices.remove(label)
            elif len(voices) >= 2:
                await _safe_answer(query, "Можно выбрать до двух переводов", show_alert=True)
            else:
                voices.append(label)
            defaults["preferred_voices"] = voices
            voices_expanded = True
            context.user_data["settings_voices_expanded"] = True
    elif action == "voices_clear":
        defaults["preferred_voices"] = []
        voices_expanded = True
        context.user_data["settings_voices_expanded"] = True
    elif action == "reset":
        state_store.reset_user_search_defaults(chat_id)
        context.user_data["settings_voices_expanded"] = False
        defaults = _global_search_defaults()
        await _safe_answer(query, "Сброшено")
        await query.edit_message_text(
            _settings_text(defaults),
            reply_markup=_settings_keyboard(defaults),
        )
        return
    else:
        await _safe_answer(query)
        return

    if action not in {"voices"}:
        _save_search_defaults(chat_id, defaults)
        await _safe_answer(query, "Сохранено")
    else:
        await _safe_answer(query)
    await query.edit_message_text(
        _settings_text(defaults),
        reply_markup=_settings_keyboard(defaults, voices_expanded=voices_expanded),
    )


_ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY = "admin_last_diagnostics_report"
_ADMIN_DIAGNOSTICS_REPORT_TS_CACHE_KEY = "admin_last_diagnostics_report_at"


def _diagnostics_snapshot_time() -> str:
    return datetime.now(DISPLAY_TIMEZONE).strftime("%d.%m %H:%M")


def _cache_diagnostics_report(context: ContextTypes.DEFAULT_TYPE, report) -> str:
    snapshot_at = _diagnostics_snapshot_time()
    chat_data = getattr(context, "chat_data", None)
    if isinstance(chat_data, dict):
        chat_data[_ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY] = report
        chat_data[_ADMIN_DIAGNOSTICS_REPORT_TS_CACHE_KEY] = snapshot_at
    return snapshot_at


def _cached_diagnostics_report(context: ContextTypes.DEFAULT_TYPE):
    chat_data = getattr(context, "chat_data", None)
    if not isinstance(chat_data, dict):
        return None
    return chat_data.get(_ADMIN_DIAGNOSTICS_REPORT_CACHE_KEY)


def _cached_diagnostics_snapshot_time(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    chat_data = getattr(context, "chat_data", None)
    if not isinstance(chat_data, dict):
        return None
    snapshot_at = chat_data.get(_ADMIN_DIAGNOSTICS_REPORT_TS_CACHE_KEY)
    return str(snapshot_at) if snapshot_at else None


def _with_diagnostics_snapshot_time(text: str, snapshot_at: str | None) -> str:
    snapshot_at = snapshot_at or _diagnostics_snapshot_time()
    lines = text.splitlines()
    if not lines:
        return f"Снимок: {snapshot_at}"
    return "\n".join([lines[0], f"Снимок: {snapshot_at}", *lines[1:]])


async def _build_diagnostics_report():
    return await asyncio.to_thread(
        run_diagnostics,
        rutracker_client=rutracker_client,
        jackett_client=jackett_client,
        jackett_warmup_status=_jackett_warmup_status_snapshot(),
        ds_client=ds_client,
        tracker_service=_tracker_service(),
        display_timezone=DISPLAY_TIMEZONE,
        plex_client=plex_client,
        plex_cache_info=_plex_cache_info() if plex_client else None,
        plex_deeplink_base_url=PLEX_DEEPLINK_BASE_URL,
        voice_search_enabled=VOICE_SEARCH_ENABLED,
        openai_api_key=OPENAI_API_KEY,
        voice_usage=state_store.load_voice_usage(),
        gpt_enabled=GPT_ENABLED,
        gpt_model=GPT_MODEL,
        gpt_usage=state_store.load_gpt_usage(),
    )


async def _build_diagnostics_text(context: ContextTypes.DEFAULT_TYPE | None = None) -> str:
    report = await _build_diagnostics_report()
    snapshot_at = _diagnostics_snapshot_time()
    if context is not None:
        snapshot_at = _cache_diagnostics_report(context, report)
    return _with_diagnostics_snapshot_time(format_diagnostics(report), snapshot_at)


async def _build_cached_diagnostics_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    report = _cached_diagnostics_report(context)
    if report is None:
        return await _build_diagnostics_text(context)
    return _with_diagnostics_snapshot_time(
        format_diagnostics(report),
        _cached_diagnostics_snapshot_time(context),
    )


async def _build_diagnostics_section_text(
    section: str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    *,
    refresh: bool = True,
) -> str:
    report = None if refresh or context is None else _cached_diagnostics_report(context)
    if report is None:
        report = await _build_diagnostics_report()
        snapshot_at = _diagnostics_snapshot_time()
        if context is not None:
            snapshot_at = _cache_diagnostics_report(context, report)
    else:
        snapshot_at = _cached_diagnostics_snapshot_time(context) if context is not None else None
    return _with_diagnostics_snapshot_time(format_diagnostics_section(report, section), snapshot_at)


def _kinopoisk_lookup_error_text() -> str:
    return (
        "⚠️ Не удалось получить данные из Кинопоиска\n\n"
        "Кинопоиск сейчас не ответил. Попробуйте ещё раз позже или введите название вручную."
    )


def _download_station_user_error_text(action: str, *, task_id: str = "") -> str:
    lines = [f"⚠️ {action}"]
    if task_id:
        lines.append(f"ID: {task_id}")
    lines.extend([
        "",
        "Download Station сейчас не ответил или отклонил команду.",
        "Попробуйте снова через минуту. Если проблема повторяется, администратору стоит проверить /admin.",
    ])
    return "\n".join(lines)


def _torrent_file_user_error_text() -> str:
    return (
        "⚠️ Не удалось обработать .torrent\n\n"
        "Файл не получилось добавить в Download Station. Попробуйте ещё раз или выберите другую раздачу."
    )


def _subscription_save_user_error_text(*, downloaded: bool) -> str:
    if downloaded:
        return "доступные серии добавлены, но подписку не создал"
    return "подписку не удалось создать для этого варианта"


async def kp_link_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ConversationHandler entry point: user sent a Kinopoisk URL.

    Fetches film/series info, stores the search base in user_data, then shows
    the quality-options keyboard so the user can kick off the normal search flow.
    """
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return ConversationHandler.END

    _clear_search_intent(context)
    text = (update.message.text or "").strip()
    kp_id = extract_kp_id(text)
    if not kp_id:
        return ConversationHandler.END

    msg = await update.message.reply_text("🎬 Получаю информацию из Кинопоиска…")
    try:
        info: KinopoiskInfo = await asyncio.to_thread(kinopoisk_client.get_film_info, kp_id)
    except KinopoiskError:
        logger.info("Kinopoisk lookup failed for kp_id=%s", kp_id, exc_info=True)
        await msg.edit_text(_kinopoisk_lookup_error_text())
        return ConversationHandler.END

    context.user_data["srch_query"] = info.search_base
    context.user_data["srch_from_kp_link"] = True
    context.user_data["srch_kp_info"] = {
        "kp_id": info.kp_id,
        "title_ru": info.title_ru,
        "title_en": info.title_en,
        "year": info.year,
        "type_label": info.type_label,
        "director": info.director,
    }
    draft = await _parse_search_intent_for_user_text(text.replace(str(kp_id), " "))
    if info.media_type in {"TV_SERIES", "MINI_SERIES", "TV_SHOW"} and draft.intent == INTENT_SERIES_MASTER:
        pass
    elif info.media_type == "FILM" and draft.intent == INTENT_SERIES_MASTER:
        draft = replace(draft, intent=INTENT_ONE_RELEASE, whole_series=False, conflicts=(*draft.conflicts, "kp_movie_series"))
    _apply_intent_to_search_state(
        context,
        draft,
        chat_id=_chat_id(update),
        fallback_text=info.search_base,
        force_base=info.search_base,
    )

    lines = [f"{info.type_label}: <b>{info.title_ru}</b>"]
    if info.title_en and info.title_en.lower() != info.title_ru.lower():
        lines.append(f"  {info.title_en}")
    if info.year:
        lines.append(f"📅 Год: {info.year}")
    if info.director:
        lines.append(f"🎬 Режиссёр: {info.director}")
    lines.append(f"\n🔍 Запрос для поиска: «{info.search_base}»")
    lines.append(f"\nЧто скачать: {_search_mode_label(_search_intent(context))}")

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_search_options_keyboard(
            _tracker_label_from_context(context),
            _search_intent(context),
        ),
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


def _search_intent(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.user_data.get("srch_intent")


def _search_is_series_master(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return _search_intent(context) == SEARCH_INTENT_SERIES_MASTER


def _clear_search_intent(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("srch_intent", None)
    context.user_data.pop("srch_intent_draft", None)
    context.user_data.pop("srch_setting_sources", None)
    context.user_data.pop("srch_voice_hints", None)
    context.user_data.pop("srch_voice_required", None)
    context.user_data.pop("srch_voice_source", None)
    context.user_data.pop("srch_partial_policy_hint", None)


async def _parse_search_intent_for_user_text(text: str) -> SearchIntentDraft:
    draft = parse_search_intent(text)
    if GPT_ENABLED and (draft.confidence == "low" or bool(draft.conflicts)):
        sink: list = []
        try:
            gpt_draft, error = await asyncio.to_thread(
                parse_search_intent_with_gpt,
                text,
                draft,
                api_key=OPENAI_API_KEY,
                model=GPT_MODEL,
                usage_sink=sink,
            )
        except Exception:
            logger.warning("intent parse GPT call failed", exc_info=True)
            return draft
        _gpt_record_usage(
            feature="intent_parse",
            input_tokens=200,
            output_tokens=80,
            error_label=error,
            usage=(sink[0] if sink else None),
        )
        if gpt_draft is not None:
            return gpt_draft
    return draft


def _base_query_from_intent(draft: SearchIntentDraft, fallback_text: str) -> str:
    base = (draft.base_query or fallback_text or "").strip()
    if draft.season is not None and _extract_season_from_query(base) is None:
        base = f"{base} Сезон: {draft.season}".strip()
    return _normalize_season_in_query(base)


def _apply_intent_to_search_state(
    context: ContextTypes.DEFAULT_TYPE,
    draft: SearchIntentDraft,
    *,
    chat_id: int | None,
    fallback_text: str,
    force_base: str | None = None,
) -> tuple[str, dict]:
    defaults = _search_defaults_for_chat(chat_id)
    settings = {
        "quality": defaults.get("quality", "1080p"),
        "audio": bool(defaults.get("audio", False)),
        "subs": bool(defaults.get("subs", False)),
    }
    sources = _default_search_setting_sources()
    if draft.quality in {"4K", "1080p", "720p", "any"}:
        settings["quality"] = draft.quality
        sources["quality"] = "explicit"
    if draft.audio_original is not None:
        settings["audio"] = bool(draft.audio_original)
        sources["audio"] = "explicit"
    if draft.subs is not None:
        settings["subs"] = bool(draft.subs)
        sources["subs"] = "explicit"

    if draft.intent == INTENT_SERIES_MASTER:
        context.user_data["srch_intent"] = SEARCH_INTENT_SERIES_MASTER

    explicit_voices = _normalise_preferred_voices(draft.voice_hints)
    preferred_voices = _normalise_preferred_voices(defaults.get("preferred_voices"))
    if explicit_voices:
        context.user_data["srch_voice_hints"] = explicit_voices
        context.user_data["srch_voice_required"] = bool(draft.voice_required)
        context.user_data["srch_voice_source"] = "explicit"
    elif preferred_voices:
        context.user_data["srch_voice_hints"] = preferred_voices
        context.user_data["srch_voice_required"] = False
        context.user_data["srch_voice_source"] = "default"
    else:
        context.user_data.pop("srch_voice_hints", None)
        context.user_data.pop("srch_voice_required", None)
        context.user_data.pop("srch_voice_source", None)

    if draft.partial_policy_hint:
        context.user_data["srch_partial_policy_hint"] = draft.partial_policy_hint

    if force_base:
        base = force_base.strip()
        if draft.season is not None and _extract_season_from_query(base) is None:
            base = f"{base} Сезон: {draft.season}".strip()
        base = _normalize_season_in_query(base)
    else:
        base = _base_query_from_intent(draft, fallback_text)
    context.user_data["srch_query"] = base
    context.user_data["srch_settings"] = settings
    context.user_data["srch_setting_sources"] = sources
    context.user_data["srch_intent_draft"] = {
        "confidence": draft.confidence,
        "conflicts": list(draft.conflicts),
        "voice_hints": list(draft.voice_hints),
        "voice_required": draft.voice_required,
    }
    return base, settings


def _should_auto_start_search(draft: SearchIntentDraft) -> bool:
    if draft.intent == INTENT_SERIES_MASTER:
        return False
    if draft.confidence != "high" or draft.conflicts:
        return False
    return draft.has_explicit_settings


def _strip_season_marker_from_query(base: str) -> str:
    normalized = _normalize_season_in_query(base)
    stripped = re.sub(r"\bСезон:\s*\d+\b", "", normalized, flags=re.IGNORECASE)
    stripped = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s{2,}", " ", stripped)
    return stripped.strip(" -–—:;/")


def _search_base_for_current_mode(context: ContextTypes.DEFAULT_TYPE, base: str) -> str:
    if not _search_is_series_master(context):
        return base
    normalized = _normalize_season_in_query(base)
    if "/" in normalized:
        extracted = _extract_series_base_query(normalized)
        if extracted:
            return extracted
    return _strip_season_marker_from_query(normalized) or normalized


def _build_current_mode_search_query(
    context: ContextTypes.DEFAULT_TYPE,
    base: str,
    settings: dict,
) -> str:
    return _build_search_query(_search_base_for_current_mode(context, base), settings)


def _search_options_text(
    base: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    escape_html: bool = False,
) -> str:
    shown_base = html_module.escape(base) if escape_html else base
    if _search_is_series_master(context):
        return (
            "📚 Скачать сериал целиком\n\n"
            f"Запрос: «{shown_base}»\n\n"
            "Настройте поиск эталонной раздачи. Эти параметры потом перейдут в план сезонов.\n"
            "Предпочитаемую озвучку можно будет выбрать после эталонной раздачи: "
            "я покажу варианты, которые реально нашёл в выбранном релизе."
        )

    text = (
        "🔍 Поиск\n\n"
        f"Запрос: «{shown_base}»\n\n"
        "Что скачать: одна раздача"
    )
    if _extract_season_from_query(base) is not None:
        text += (
            "\n\nПохоже, вы ищете сезон сериала. Можно найти одну раздачу "
            "или собрать план по всем сезонам."
        )
    return text


def _search_advanced_text(base: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    if _search_is_series_master(context):
        return _search_options_text(base, context)
    return (
        "🔍 Поиск\n\n"
        f"Запрос: «{base}»\n\n"
        "Настройте параметры поиска."
    )


async def search_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_text = (update.message.text or "").strip()
    if not raw_text:
        await update.message.reply_text("Введите текст для поиска или /cancel для отмены.")
        return ConversationHandler.END

    draft = await _parse_search_intent_for_user_text(raw_text)
    query_text, settings = _apply_intent_to_search_state(
        context,
        draft,
        chat_id=_chat_id(update),
        fallback_text=raw_text,
    )
    if not query_text:
        await update.message.reply_text("Введите название фильма или сериала.")
        return ConversationHandler.END
    if _should_auto_start_search(draft):
        return await _run_search(
            update.message.reply_text,
            context,
            _build_current_mode_search_query(context, query_text, settings),
        )
    msg = await update.message.reply_text(
        _search_options_text(query_text, context),
        reply_markup=_search_options_keyboard(
            _tracker_label_from_context(context),
            _search_intent(context),
        ),
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
    lines.append(f"Результаты по {_format_search_query_label(search_query, escape_html=True)}:")
    start = page * SEARCH_PAGE_SIZE
    visible_results = results_data[start : start + SEARCH_PAGE_SIZE]
    if any(r.get("partial") for r in visible_results):
        lines.append("⬇️ N — варианты скачивания; 🔔 N — варианты уведомлений.")
    for index, r in enumerate(visible_results, start=start):
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
        # PR3: if GPT pre-parsed the title, render compact badge lines below
        # the title. Falls back to old behaviour (just size+seeders) when
        # parsed_meta is absent (GPT disabled / cache miss / parse failed).
        meta_lines = _format_parsed_meta_lines(r.get("parsed_meta"))
        lines.append(
            f"\n{icon} {index + 1}. {tracker_prefix}{title_linked}"
            f"\n   📦 {r['size']} | 🌱 {r['seeders']}{ep_note}"
            f"{meta_lines}"
            f"{_format_plex_hint_line(r.get('plex_hint'))}"
        )
    return "\n".join(lines)


def _format_parsed_meta_lines(meta: dict | None) -> str:
    """Render GPT-parsed torrent metadata as a single extra indented line
    appended below the size/seeders row. Returns empty string when no meta.

    Format example:
        \\n   🎬 2160p UHD BDRemux · HDR10+/DV · TrueHD 7.1 Atmos · 🌐 RUS/UKR/ENG
    """
    if not isinstance(meta, dict):
        return ""
    badges: list[str] = []
    # Quality + source as one compact chunk
    qs_parts: list[str] = []
    if meta.get("quality"):
        qs_parts.append(str(meta["quality"]))
    if meta.get("source"):
        qs_parts.append(str(meta["source"]))
    if qs_parts:
        badges.append(" ".join(qs_parts))
    if meta.get("hdr"):
        badges.append(str(meta["hdr"]))
    if meta.get("audio"):
        badges.append(str(meta["audio"]))
    langs = meta.get("langs")
    if isinstance(langs, list) and langs:
        badges.append("🌐 " + "/".join(str(l) for l in langs))
    if meta.get("edition"):
        badges.append(str(meta["edition"]))
    if not badges:
        return ""
    return f"\n   🎬 {html_module.escape(' · '.join(badges))}"


def _display_quality_from_plex_resolution(value: str) -> str:
    return {
        "4k": "4K",
        "1080": "1080p",
        "720": "720p",
        "480": "480p",
        "sd": "SD",
    }.get((value or "").lower(), value or "")


def _format_plex_hint_line(hint: dict | None) -> str:
    if not isinstance(hint, dict):
        return ""
    action = hint.get("action")
    quality = _display_quality_from_plex_resolution(str(hint.get("quality") or ""))
    kind = hint.get("kind")
    season = hint.get("season")
    season_label = f"S{season}" if season else "Сезон"
    if kind == "series":
        if action == "warn_same":
            suffix = f": {season_label}, {quality}" if quality else f": {season_label}"
            return f"\n   ✅ Сезон уже есть в Plex{suffix}"
        if action == "warn_better":
            suffix = f": {season_label}, {quality}" if quality else f": {season_label}"
            return f"\n   ⚠️ В Plex есть этот сезон лучше{suffix}"
        if action == "offer_upgrade":
            suffix = f": {season_label}, {quality}" if quality else f": {season_label}"
            return f"\n   🔼 В Plex есть хуже{suffix}, можно улучшить"
        if action == "partial_exists":
            return f"\n   📺 В Plex уже есть часть сезона: {season_label}"
        if action == "other_seasons":
            labels = ", ".join(
                f"S{int(s)}" for s in (hint.get("seasons") or [])[:8]
                if isinstance(s, int) or str(s).isdigit()
            )
            return f"\n   📺 В Plex уже есть другие сезоны: {labels}" if labels else ""
    else:
        if action == "warn_same":
            suffix = f": {quality}" if quality else ""
            return f"\n   ✅ Уже есть в Plex{suffix}"
        if action == "warn_better":
            suffix = f": {quality}" if quality else ""
            return f"\n   ⚠️ В Plex есть лучше{suffix}"
        if action == "offer_upgrade":
            suffix = f": {quality}" if quality else ""
            return f"\n   🔼 В Plex есть хуже{suffix}, можно улучшить"
    return ""


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
        await edit_fn(
            _friendly_error("jackett", str(e), include_detail=False),
            reply_markup=_search_error_keyboard(),
            parse_mode="HTML",
        )
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
    confirm_label = "💾 Применить" if return_to in ("options", "advanced") else "🔍 Искать"
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
    base_query, preferred_quality, audio_required, subs_required = _split_query_settings(search_query)
    series_master = _search_is_series_master(context)
    await query.edit_message_text(f"🔍 Ищу {_format_search_query_label(search_query)} напрямую в Rutracker…")
    try:
        rt_results = await asyncio.to_thread(rutracker_client.search, base_query)
    except RutrackerError as rt_err:
        await query.edit_message_text(
            _friendly_error("rutracker", str(rt_err), include_detail=False),
            reply_markup=_search_error_keyboard(),
            parse_mode="HTML",
        )
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
            "series": _extract_series_base_query(r.title) is not None,
            "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
            "magnet_url": None,
            "torrent_url": None,
            "tracker_name": "rutracker",
        })
    results_data, filter_banner_parts = _apply_audio_subs_preferences(
        results_data,
        context,
        audio_required=audio_required,
        subs_required=subs_required,
    )
    quality_banner = ""
    if preferred_quality and results_data:
        buckets = _classify_results_by_quality(results_data)
        preferred_bucket = buckets.get(preferred_quality) or []
        if preferred_bucket:
            results_data = preferred_bucket
            other_stats = _format_quality_stats(buckets, exclude=preferred_quality)
            quality_banner = f"🎬 Показаны раздачи в {preferred_quality}."
            if other_stats:
                quality_banner += f" Также есть: {other_stats}."
        else:
            other_stats = _format_quality_stats(buckets)
            quality_banner = f"⚠️ В {preferred_quality} ничего не найдено. Показаны все качества: {other_stats}."
    series_master_banner = ""
    if series_master:
        before = len(results_data)
        results_data = [
            r for r in results_data
            if r.get("series") or _extract_series_base_query(str(r.get("title") or ""))
        ]
        if before != len(results_data):
            filter_banner_parts.append(f"📚 Оставлены сериальные раздачи: {len(results_data)}/{before}.")
        series_master_banner = (
            "📚 Эталонная раздача для сериала\n"
            "Выберите раздачу, по которой я пойму качество, тип релиза и возможные озвучки. "
            "Скачивание начнётся только после плана и подтверждения."
        )
    results_data, voice_banner = _apply_voice_preferences(results_data, context)
    if voice_banner:
        filter_banner_parts.append(voice_banner)
    if not results_data:
        has_quality, _ = _no_results_flags(context, search_query)
        suggestions = await _gpt_get_did_you_mean(base_query)
        suggestions = _remember_didmean_suggestions(context, suggestions)
        advice = None
        if not multi_tracker_coverage_lost and not original_kp_match:
            advice = await _gpt_get_search_failure_advice(
                search_query,
                base_query=base_query,
                preferred_quality=preferred_quality,
                audio_required=audio_required,
                subs_required=subs_required,
                has_quality=has_quality,
                jackett_can_expand=jackett_can_expand,
                season_requested=bool(_extract_season_from_query(search_query)),
                source_status="empty",
                suggestions=suggestions,
            )
            extra_suggestions = (advice or {}).get("suggested_queries") or []
            if extra_suggestions:
                suggestions = _remember_didmean_suggestions(
                    context, [*suggestions, *extra_suggestions],
                )
        advice = await _gpt_get_search_failure_advice(
            search_query,
            base_query=base_query,
            preferred_quality=preferred_quality,
            audio_required=audio_required,
            subs_required=subs_required,
            has_quality=has_quality,
            jackett_can_expand=False,
            season_requested=bool(_extract_season_from_query(search_query)),
            source_status="rutracker_empty",
            suggestions=suggestions,
        )
        extra_suggestions = (advice or {}).get("suggested_queries") or []
        if extra_suggestions:
            suggestions = _remember_didmean_suggestions(
                context, [*suggestions, *extra_suggestions],
            )
        # Direct Rutracker path — Jackett expansion is irrelevant here.
        if series_master:
            text = "Не нашёл сериальных раздач в Rutracker."
        else:
            text = f"По запросу {_format_search_query_label(search_query)} ничего не найдено в Rutracker."
        advice_text = _format_search_failure_advice(advice)
        if advice_text:
            text += f"\n\n{advice_text}"
        elif suggestions:
            text += (
                "\n\n🤖 Возможно вы имели в виду — попробуйте вариант ниже "
                "или измените запрос вручную."
            )
        await query.edit_message_text(
            text,
            reply_markup=_no_results_keyboard(
                has_quality=has_quality,
                jackett_can_expand=False,
                suggestions=suggestions,
            ),
        )
        return SEARCH_RESULTS
    results_data.sort(key=_search_result_sort_score, reverse=True)
    results_data[0]["recommended"] = True
    await _enrich_results_with_plex_hints(results_data, preferred_quality, max_n=15)
    banner = "\n".join(b for b in (series_master_banner, "🔗 Прямой поиск Rutracker", *filter_banner_parts, quality_banner) if b)
    context.user_data["srch_results"] = results_data
    context.user_data["srch_results_page"] = 0
    # R.2 pre-warm: kick off background Plex season fetch for the first
    # partial-season result so the eventual confirm dialog is instant.
    _maybe_prewarm_plex_for_results(
        context, _chat_id_from_query(query) if "query" in locals() else None,
        results_data,
    )
    context.user_data["srch_banner"] = banner
    context.user_data["srch_source"] = "rutracker"
    await query.edit_message_text(
        _build_results_text(results_data, search_query, 0, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=0,
            show_switch_trackers=False,
            show_retry_jackett=bool(jackett_client),  # offer back to Jackett
            show_direct_rutracker=False,              # already on direct RT
            series_master=series_master,
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


def _rutracker_topic_url(topic_id: str) -> str:
    return f"https://rutracker.org/forum/viewtopic.php?t={topic_id}"


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


_QUALITY_SUFFIX_RE = re.compile(
    r"\s+(2160p|4k|uhd|1080p|720p|480p|hdr)\s*$",
    re.IGNORECASE,
)

# Trailing filter tokens added by _build_search_query (audio/subs flags).
# Stripped before sending the query to trackers, then applied client-side
# via the _detect_has_original_audio / _detect_has_subs heuristics below.
_FILTER_TOKEN_RE = re.compile(
    r"\s+(Original|Sub)\b",
    re.IGNORECASE,
)

# Patterns for client-side filtering (Strategy 2 for audio/subs).
# Torrent titles use a wide vocabulary for «has original audio» and
# «has subtitles» — these regexes catch the most common variants; misses
# degrade to «result kept» (better false-positive than false-negative
# given that we're filtering for an OPTIONAL user preference).
_AUDIO_ORIG_RE = re.compile(
    r"\b(orig(?:inal)?|dual|mvo|avo|dvo|nvo|"
    r"Лиценз|"  # "Лиценз"(ия)
    r"Дубляж)\b",
    re.IGNORECASE,
)
_SUBS_RE = re.compile(
    r"\b(subs?|forced|hardsub|softsub|"
    r"субтит)\w*",
    re.IGNORECASE,
)


def _detect_has_original_audio(title: str) -> bool:
    """Heuristic: does the torrent title indicate presence of an original
    (non-dubbed) audio track? Looks for dual-track / MVO / AVO / DVO /
    Лицензия / Дубляж markers used by Russian release groups."""
    return bool(_AUDIO_ORIG_RE.search(title))


def _detect_has_subs(title: str) -> bool:
    """Heuristic: subtitles present? Matches Sub / Subs / Forced / Hardsub /
    Softsub / субтитры variants."""
    return bool(_SUBS_RE.search(title))


def _split_query_settings(search_query: str) -> tuple[str, str | None, bool, bool]:
    """Strip user-preference tokens from a search query and return
    (base_query, preferred_quality, audio_required, subs_required).

    Strategy 2: send the clean base to Jackett/Rutracker (one network call,
    independent of filters), then apply quality/audio/subs as CLIENT-SIDE
    filters. See _split_query_quality (legacy single-token helper) for the
    original rationale — this extends it to cover audio + subs.

    Quality tokens stripped: 1080p / 720p / 2160p / 480p / 4k / uhd / hdr.
    Filter flags detected: «Original» (audio preference), «Sub» (subs preference).
    Tokens may appear in any order and any amount of whitespace.

    Returns:
        (base_query, preferred_quality, audio_required, subs_required)
        Quality normalised to {1080p, 720p, 2160p, 480p} or None.
        Audio/subs default to False when not present.
    """
    s = search_query.strip()
    audio_required = False
    subs_required = False
    # Iteratively pull trailing filter tokens until no more match — they may
    # have been appended in any order ("base 1080p Original Sub" or
    # "base Sub Original 1080p").
    while True:
        m = _FILTER_TOKEN_RE.search(s)
        if not m:
            break
        token = m.group(1).lower()
        if token == "original":
            audio_required = True
        elif token == "sub":
            subs_required = True
        s = (s[: m.start()] + s[m.end():]).strip()
    # Quality is exclusive (only one quality token expected at a time).
    m = _QUALITY_SUFFIX_RE.search(s)
    preferred_quality: str | None = None
    if m:
        token = m.group(1).lower()
        preferred_quality = {
            "4k": "2160p", "uhd": "2160p", "2160p": "2160p",
            "1080p": "1080p",
            "720p": "720p",
            "480p": "480p",
            "hdr": "2160p",
        }.get(token, token)
        s = s[: m.start()].strip()
    return (s or search_query.strip(), preferred_quality, audio_required, subs_required)


_MEDIA_INTENT_MOVIE_RE = re.compile(
    r"(?<!\w)(фильм|фильма|фильмы|кино|мультфильм|мультик)(?!\w)",
    re.IGNORECASE,
)
_MEDIA_INTENT_SERIES_RE = re.compile(
    r"(?<!\w)(сериал|сериала|сериалы|сериалов|мультсериал|сезон|сезоны|серия|серии)(?!\w)",
    re.IGNORECASE,
)


def _detect_media_intent(search_query: str) -> str | None:
    """Return an explicit media intent from user wording, if unambiguous."""
    has_movie = bool(_MEDIA_INTENT_MOVIE_RE.search(search_query or ""))
    has_series = bool(_MEDIA_INTENT_SERIES_RE.search(search_query or ""))
    if has_movie == has_series:
        return None
    return "movie" if has_movie else "series"


def _apply_media_intent_filter(
    results_data: list[dict],
    media_intent: str | None,
) -> tuple[list[dict], str]:
    if media_intent not in {"movie", "series"}:
        return results_data, ""

    preferred = [r for r in results_data if _search_cluster_kind(r) == media_intent]
    if not preferred or len(preferred) == len(results_data):
        return results_data, ""

    hidden = len(results_data) - len(preferred)
    label = "фильмы" if media_intent == "movie" else "сериалы"
    return (
        preferred,
        f"⚙️ Показаны {label}: {len(preferred)}/{len(results_data)} (скрыто {hidden})",
    )


def _format_search_query_label(search_query: str, *, escape_html: bool = False) -> str:
    """Render user-facing query text without treating filters as title text.

    Example: ``Драйв 1080p Original`` -> ``«Драйв» (качество: 1080p, оригинальная дорожка)``.
    """
    base_query, preferred_quality, audio_required, subs_required = _split_query_settings(search_query)
    shown = base_query or search_query.strip()
    if escape_html:
        shown = html_module.escape(shown)

    filters: list[str] = []
    if preferred_quality:
        filters.append(f"качество: {preferred_quality}")
    if audio_required:
        filters.append("оригинальная дорожка")
    if subs_required:
        filters.append("субтитры")

    suffix = f" ({', '.join(filters)})" if filters else ""
    return f"«{shown}»{suffix}"


def _search_constraints_line(
    preferred_quality: str | None,
    audio_required: bool,
    subs_required: bool,
) -> str:
    constraints: list[str] = []
    if preferred_quality:
        constraints.append(preferred_quality)
    if audio_required:
        constraints.append("Original")
    if subs_required:
        constraints.append("субтитры")
    return ", ".join(constraints)


def _search_empty_text(
    search_query: str,
    *,
    preferred_quality: str | None = None,
    audio_required: bool = False,
    subs_required: bool = False,
    banner: str = "",
    body: str = "",
    action_hint: str = "",
) -> str:
    lines = [
        "🔍 Ничего не нашёл",
        "",
        f"По запросу {_format_search_query_label(search_query)} ничего не найдено.",
    ]
    constraints = _search_constraints_line(preferred_quality, audio_required, subs_required)
    if constraints:
        lines.append(f"Ограничения: {constraints}.")
    if banner:
        lines.extend(["", banner])
    if body:
        lines.extend(["", body])
    if action_hint:
        lines.extend(["", action_hint])
    return "\n".join(lines)


def _search_source_error_text(service: str, raw: str) -> str:
    return (
        "⚠️ Поиск сейчас не получился\n\n"
        f"{_friendly_error(service, raw, include_detail=False)}\n\n"
        "Что можно сделать: повторить поиск после паузы или проверить статус сервисов в /admin."
    )


def _classify_results_by_quality(results: list[dict]) -> dict[str, list[dict]]:
    """Group results by detected quality bucket (Plex-normalised string).

    Returns a dict mapping {"720p": [...], "1080p": [...], "2160p": [...],
    "480p": [...], "other": [...]} — only non-empty buckets are present.
    Reuses movie_discovery.detect_quality which already handles BDRip /
    BDRemux / WEB-DL / etc. fallbacks correctly.
    """
    buckets: dict[str, list[dict]] = {}
    for r in results:
        q = _movie_detect_quality(r.get("title", "")) or "other"
        buckets.setdefault(q, []).append(r)
    return buckets


def _format_quality_stats(buckets: dict[str, list[dict]], exclude: str | None = None) -> str:
    """Human-readable list of other-than-preferred quality counts.

    Example: buckets = {1080p: [..28..], 720p: [..12..], 2160p: [..7..]},
             exclude = "1080p"
             → "720p × 12, 2160p × 7"
    Order is fixed (highest quality first) for stable display.
    """
    order = ["2160p", "1080p", "720p", "480p", "other"]
    parts = [
        f"{q} × {len(buckets[q])}"
        for q in order
        if q in buckets and q != exclude and buckets[q]
    ]
    return ", ".join(parts)


def _result_has_preferred_voice(result: dict, voices: list[str]) -> bool:
    if not voices:
        return False
    release = release_profile_from_title(str(result.get("title") or ""), size=str(result.get("size") or ""))
    return bool(set(release.voices) & set(voices))


def _apply_voice_preferences(results: list[dict], context: ContextTypes.DEFAULT_TYPE) -> tuple[list[dict], str]:
    voices = _normalise_preferred_voices(context.user_data.get("srch_voice_hints"))
    if not voices or not results:
        return results, ""
    matched: list[dict] = []
    other: list[dict] = []
    for result in results:
        if _result_has_preferred_voice(result, voices):
            result["voice_preferred"] = True
            matched.append(result)
        else:
            result.pop("voice_preferred", None)
            other.append(result)
    label = " / ".join(voices)
    source = context.user_data.get("srch_voice_source")
    if matched and source == "explicit":
        return matched + other, f"🎙 Сначала варианты с {label}; другие озвучки оставил ниже."
    if matched:
        return matched + other, ""
    if source == "explicit":
        return results, f"🎙 {label} не нашёл, показываю другие озвучки."
    if source == "default":
        return results, f"🎙 Предпочитаемый перевод {label} не нашёл, показываю другие озвучки."
    return results, ""


def _apply_presence_preference(
    results: list[dict],
    *,
    enabled: bool,
    explicit: bool,
    predicate,
    flag: str,
    kept_label: str,
    missing_explicit: str,
    missing_default: str,
) -> tuple[list[dict], str]:
    if not enabled or not results:
        return results, ""
    matched: list[dict] = []
    other: list[dict] = []
    for result in results:
        if predicate(str(result.get("title") or "")):
            result[flag] = True
            matched.append(result)
        else:
            result.pop(flag, None)
            other.append(result)
    if not matched:
        return results, missing_explicit if explicit else missing_default
    if explicit:
        dropped = len(results) - len(matched)
        if dropped > 0:
            return matched, f"⚙️ Оставлены {len(matched)}/{len(results)} {kept_label} (скрыто {dropped})"
        return matched, ""
    return matched + other, ""


def _apply_audio_subs_preferences(
    results: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
    *,
    audio_required: bool,
    subs_required: bool,
) -> tuple[list[dict], list[str]]:
    banners: list[str] = []
    results, banner = _apply_presence_preference(
        results,
        enabled=audio_required,
        explicit=_search_setting_source(context, "audio") == "explicit",
        predicate=_detect_has_original_audio,
        flag="audio_preferred",
        kept_label="с оригинальной дорожкой",
        missing_explicit="🎧 Original не нашёл, показываю другие варианты.",
        missing_default="🎧 Предпочитаемую Original-дорожку не нашёл, показываю другие варианты.",
    )
    if banner:
        banners.append(banner)
    results, banner = _apply_presence_preference(
        results,
        enabled=subs_required,
        explicit=_search_setting_source(context, "subs") == "explicit",
        predicate=_detect_has_subs,
        flag="subs_preferred",
        kept_label="с субтитрами",
        missing_explicit="💬 Субтитры не нашёл, показываю варианты без них.",
        missing_default="💬 Предпочитаемые субтитры не нашёл, показываю варианты без них.",
    )
    if banner:
        banners.append(banner)
    return results, banners


def _search_result_sort_score(result: dict) -> float:
    return (
        _score_result(result)
        + (10000 if result.get("voice_preferred") else 0)
        + (5000 if result.get("audio_preferred") else 0)
        + (3000 if result.get("subs_preferred") else 0)
    )


def _cancel_didmean_prefetch(context) -> None:
    """Cancel an in-flight did-you-mean prefetch task and pop the slot.

    Safe to call even when no prefetch exists. Used at every search-lifecycle
    exit point (search_cancel, search_timeout, new _run_search start, etc.)
    to avoid zombie background tasks accumulating.
    """
    prefetch = context.user_data.pop("srch_didmean_prefetch", None)
    if not prefetch:
        return
    _query, task = prefetch
    if not task.done():
        task.cancel()
        logger.info("movie_discovery: didmean prefetch cancelled (query=%r)", _query)


def _remember_didmean_suggestions(context, suggestions: list[str] | None) -> list[str]:
    cleaned = [
        str(s).strip()
        for s in (suggestions or [])
        if str(s).strip()
    ][:3]
    if cleaned:
        context.user_data["srch_didmean_suggestions"] = cleaned
    else:
        context.user_data.pop("srch_didmean_suggestions", None)
    return cleaned


async def _didmean_prefetch_jackett(
    base_query: str,
    indexers: list[str],
) -> list | None:
    """Background helper: run the slow Jackett.search() call so the result
    is ready when the user taps the did-you-mean suggestion button.

    Returns the raw JackettResult list on success, None on any failure.
    Intentionally narrow scope — we only prefetch the SLOW network part
    (Jackett 2-5 sec network round-trip). The downstream processing
    (parsing, filtering, scoring) is fast and stays in _run_search on
    the click path. This minimises wasted work when the user picks a
    different suggestion or none.
    """
    if jackett_client is None or not base_query:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                jackett_client.search,
                base_query,
                indexers=indexers,
                fetch_limit=JACKETT_FETCH_LIMIT,
            ),
            timeout=JACKETT_SEARCH_TIMEOUT_SECONDS + 5.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("movie_discovery: didmean prefetch failed for %r: %s", base_query, exc)
        return None


_SEARCH_CLUSTER_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _search_cluster_year(title: str) -> int | None:
    matches = _SEARCH_CLUSTER_YEAR_RE.findall(title or "")
    return int(matches[-1]) if matches else _movie_extract_year(title)


def _normalize_search_cluster_title(title: str) -> str:
    normalized = _normalize_movie_title(title)
    normalized = _SEARCH_CLUSTER_YEAR_RE.sub(" ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _strip_search_cluster_series_markers(title: str) -> str:
    title = _normalize_season_in_query(title or "")
    title = re.sub(r"\bS0*\d{1,2}\s*[-‑–—]\s*S?0*\d{1,2}\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\bS0*\d{1,2}E\d{1,2}(?:-\d{1,2})?(?:\s+of\s+\d{1,2})?\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\bS0*\d{1,2}\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\bСезоны?[:\s]+0*\d{1,2}\s*[-‑–—]\s*0*\d{1,2}\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\bСезон[:\s]+0*\d{1,2}\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b0*\d{1,2}\s*(?:[-‑–—]?\s*(?:й|ый|ой))?\s+сезон\b", " ", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", title).strip()


def _search_cluster_series_meta(title: str) -> tuple[str, tuple[int, ...], str]:
    pack_range = season_pack_range_from_title(title)
    if pack_range is not None:
        start, end = pack_range
        seasons = tuple(range(start, end + 1))
        return "pack", seasons, f"S{start}-S{end}"

    season = _extract_season_from_query(title)
    if season is not None:
        return "season", (season,), f"S{season}"

    return "unknown", (), ""


def _search_cluster_display_title(title: str, kind: str) -> str:
    if kind != "series":
        return _normalize_search_cluster_title(title)

    base = _extract_series_base_query(title) or title
    stripped = _strip_search_cluster_series_markers(base)
    normalized = _normalize_search_cluster_title(stripped)
    return normalized or _normalize_search_cluster_title(title)


_SEARCH_CLUSTER_SERIES_MARKERS = (
    "сериал",
    "сериалы",
    "телесериал",
    "мультсериал",
    "мультсериалы",
    "tv",
    "series",
)


def _search_cluster_kind(result: dict) -> str:
    """Return ``series`` or ``movie`` for the compact cluster picker badge."""
    title = str(result.get("movie_title") or result.get("title") or "")
    if _plex_is_series(title) or _extract_series_base_query(title):
        return "series"

    category_text = " ".join(
        str(result.get(key) or "")
        for key in ("category", "tracker_name")
    ).lower()
    if any(marker in category_text for marker in _SEARCH_CLUSTER_SERIES_MARKERS):
        return "series"

    return "movie"


def _build_search_clusters(results_data: list[dict]) -> list[dict]:
    """Group search results by title/year, with seasons split for series.

    Returns a list of cluster dicts, each:
        {
          "key": "<title>|<year>",         # human-readable identity
          "title": "Дюна",                 # display title (best from cluster)
          "year": 2024,                    # or None if unparseable
          "kind": "movie",                 # "movie" or "series"
          "season_label": "S2",            # series only, empty when unknown
          "count": 12,                     # number of releases
          "indices": [0, 3, 7, ...],       # positions in results_data
        }

    Sort: by year descending, then season/count descending.
    """
    clusters: dict[tuple, dict] = {}
    for idx, r in enumerate(results_data):
        title = r.get("title") or ""
        year = _search_cluster_year(title)
        kind = _search_cluster_kind(r)
        normalized = _search_cluster_display_title(title, kind)
        season_kind, seasons, season_label = _search_cluster_series_meta(title) if kind == "series" else ("", (), "")
        season_key = (season_kind, seasons) if kind == "series" else None
        key = (kind, normalized.lower(), year, season_key)
        if key not in clusters:
            clusters[key] = {
                "key": f"{normalized}|{year if year else '?'}",
                "title": normalized or title,
                "year": year,
                "kind": kind,
                "season_label": season_label,
                "seasons": list(seasons),
                "count": 0,
                "indices": [],
            }
        clusters[key]["count"] += 1
        clusters[key]["indices"].append(idx)
    # Sort: newer first, then by season and release count.
    return sorted(
        clusters.values(),
        key=lambda c: (
            -(c["year"] or 0),
            -max(c.get("seasons") or [0]),
            -c["count"],
            c["title"],
        ),
    )


def _cluster_query_relevance(cluster: dict, query: str) -> int:
    """How directly a cluster title matches the cleaned search query.

    3 = exact title match, 2 = title starts with the query, 1 = query is only
    a token inside a longer title, 0 = unrelated/noisy.
    """
    q_norm = _normalize_search_cluster_title(query or "").lower()
    title_norm = str(cluster.get("title") or "").strip().lower()
    if not q_norm or not title_norm:
        return 0
    if title_norm == q_norm:
        return 3
    if title_norm.startswith(f"{q_norm} "):
        return 2
    tokens = set(title_norm.split())
    if q_norm in tokens:
        return 1
    return 0


def _clusters_for_query_picker(clusters: list[dict], query: str) -> list[dict]:
    """Pick and order clusters for the visible picker.

    If the query has direct title matches, show those even when they have only
    one release and suppress loose token matches like «Ледяной драйв» for a
    query «Драйв». The full result list remains available via «Показать все».
    """
    focused = [
        (relevance, cluster)
        for cluster in clusters
        if (relevance := _cluster_query_relevance(cluster, query)) >= 2
    ]
    if focused:
        focused.sort(
            key=lambda item: (
                -item[0],
                -(item[1].get("year") or 0),
                -max(item[1].get("seasons") or [0]),
                -item[1].get("count", 0),
                item[1].get("title") or "",
            )
        )
        return [cluster for _relevance, cluster in focused]

    return [c for c in clusters if c["count"] >= 2]


def _cluster_picker_text(search_query: str, banner: str = "") -> str:
    text = (
        f"По запросу {_format_search_query_label(search_query)} найдено несколько вариантов.\n"
        "Выберите один или покажите все раздачи."
    )
    if banner:
        return f"{banner}\n{text}"
    return text


def _cluster_plex_summary(clusters: list[dict]) -> str:
    series_clusters = [
        c for c in clusters
        if c.get("kind") == "series" and isinstance(c.get("plex_hint"), dict)
    ]
    if not series_clusters:
        return ""
    by_title: dict[str, list[int]] = {}
    for cluster in series_clusters:
        hint = cluster.get("plex_hint") or {}
        if hint.get("action") not in {"warn_same", "warn_better", "offer_upgrade"}:
            continue
        seasons = cluster.get("seasons") or []
        if len(seasons) != 1:
            continue
        title = str(cluster.get("title") or "").strip()
        if title:
            by_title.setdefault(title, []).append(int(seasons[0]))
    if not by_title:
        return ""
    title, seasons = max(by_title.items(), key=lambda item: len(item[1]))
    season_labels = ", ".join(f"S{s}" for s in sorted(set(seasons)))
    return f"Plex: по «{html_module.escape(title)}» уже есть {season_labels}."


def _cluster_picker_text_with_hints(search_query: str, clusters: list[dict], banner: str = "") -> str:
    summary = _cluster_plex_summary(clusters)
    prefix = "\n".join(p for p in (banner, summary) if p)
    return _cluster_picker_text(search_query, prefix)


def _should_show_cluster_picker(
    clusters: list[dict],
    *,
    total_clusters: int | None = None,
    filtered_for_query: bool = False,
) -> bool:
    """Show picker only when results genuinely span ≥2 distinct films with
    ≥2 releases each. Otherwise the picker is a useless extra tap (single
    film already, or one cluster dominates and the rest are noise)."""
    if filtered_for_query and clusters:
        if len(clusters) >= 2:
            return True
        if total_clusters is not None and len(clusters) < total_clusters:
            return True
    real_clusters = [c for c in clusters if c["count"] >= 2]
    return len(real_clusters) >= 2


def _no_results_flags(context: ContextTypes.DEFAULT_TYPE, search_query: str) -> tuple[bool, bool]:
    """Compute (has_quality, jackett_can_expand) for ``_no_results_keyboard``.

    - ``has_quality``: bare ``srch_query`` differs from the actual executed
      ``search_query`` (case-insensitive) — i.e. a quality suffix was appended.
    - ``jackett_can_expand``: Jackett is configured AND the user's selected
      indexers are a strict subset of the known ones — we can broaden.
    """
    base = (context.user_data.get("srch_query") or "").strip()
    has_quality = bool(base) and base.lower() != search_query.lower()
    indexers = context.user_data.get("srch_jackett_indexers") or []
    all_ids = {i["id"] for i in indexers}
    selected = set(context.user_data.get("srch_jackett_selected") or set())
    jackett_can_expand = bool(jackett_client) and bool(all_ids) and selected != all_ids
    return has_quality, jackett_can_expand


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
    # New search invalidates any stashed cluster picker state from a previous
    # query — we don't want stale «Показать все» buttons pointing at old
    # results_full from the prior search.
    context.user_data.pop("srch_results_full", None)
    context.user_data.pop("srch_clusters", None)
    context.user_data.pop("srch_picker_clusters", None)
    context.user_data.pop("srch_cluster_picker_return", None)
    # Check for in-flight didmean prefetch BEFORE Strategy-2 splitting — we
    # need base_query to compare. If the prefetched query matches the current
    # base, we'll use its raw Jackett results below; otherwise we cancel it
    # to free the asyncio.Task.
    prefetch = context.user_data.get("srch_didmean_prefetch")
    # Strategy 2: search WITHOUT user-preference tokens (quality / audio / subs),
    # classify + filter client-side. See _split_query_settings for rationale.
    # base_query is what actually goes to Jackett/Rutracker; the rest are
    # applied as post-filters on the raw results.
    base_query, preferred_quality, audio_required, subs_required = _split_query_settings(search_query)
    context.user_data["srch_preferred_quality"] = preferred_quality
    series_master = _search_is_series_master(context)

    # Loading message: show the user a clean «what we're looking for» rather
    # than the technical query with appended tokens. Reflects the structure
    # transparently — base on top, filters below as a sub-line.
    filter_parts: list[str] = []
    if preferred_quality:
        filter_parts.append(preferred_quality)
    if audio_required:
        filter_parts.append("оригинальная дорожка")
    if subs_required:
        filter_parts.append("субтитры")
    loading_kind = "эталонную раздачу" if series_master else "раздачи"
    loading_text = (
        f"🔎 Ищу {loading_kind}: «{base_query}»\n\n"
        "Это может занять до минуты.\n"
        "Проверяю выбранные трекеры."
    )
    if filter_parts:
        loading_text += f"\n⚙️ Фильтры: {' · '.join(filter_parts)}"

    context_chat_id = getattr(context, "_chat_id", None)
    fact_line = _pick_search_fact_for_chat(context_chat_id if isinstance(context_chat_id, int) else None, search_query)
    if fact_line:
        loading_text += fact_line

    loading_msg = await send_fn(loading_text)
    if loading_msg is not None:
        fact_line = "" if fact_line else _pick_search_fact_for_chat(getattr(loading_msg, "chat_id", None), search_query)
        if fact_line:
            loading_text += fact_line
            try:
                await loading_msg.edit_text(loading_text)
            except Exception:
                logger.debug("Search loading fact edit failed", exc_info=True)
        context.user_data["srch_ui_msg_id"] = loading_msg.message_id
        context.user_data["srch_ui_chat_id"] = loading_msg.chat_id

    # R.2-followup: progressive UI — schedule stage text edits at t+10s / t+25s
    # plus an animated MP4 sent as a sibling message. By the time the user
    # waits through a slow Jackett response (30-40s common), they've seen
    # the bot is still working. Cancelled & cleaned up when search completes.
    progressive: ProgressiveStatus | None = None
    if loading_msg is not None:
        progressive = ProgressiveStatus(
            bot=loading_msg.get_bot() if hasattr(loading_msg, "get_bot") else context.bot,
            chat_id=loading_msg.chat_id,
            initial_text=loading_text,  # already shown above; helper won't re-send
            stages=search_stages(),
            gif_path=SEARCH_ANIMATION_PATH,
        )
        # Override: we already sent the text via send_fn above, so attach the
        # existing message handle and only schedule the gif + stage updates.
        progressive.text_msg = loading_msg
        try:
            if progressive.gif_path.exists():
                with open(progressive.gif_path, "rb") as fh:
                    progressive.gif_msg = await context.bot.send_animation(
                        chat_id=loading_msg.chat_id, animation=fh,
                    )
        except Exception:
            logger.debug("Search progressive: gif send failed", exc_info=True)
            progressive.gif_msg = None
        if progressive.stages:
            try:
                progressive._task = asyncio.create_task(progressive._run_stages())
            except RuntimeError:
                progressive._task = None

    # After the first send we always edit-in-place regardless of origin.
    _raw_edit_fn = loading_msg.edit_text if loading_msg is not None else send_fn

    async def _finalize_progressive() -> None:
        """Cancel progressive updates + delete gif before showing final UI.

        Idempotent — safe to call multiple times.
        """
        if progressive is not None:
            try:
                await progressive.stop()
            except Exception:
                logger.debug("Search progressive: stop failed", exc_info=True)

    async def edit_fn(*args, **kwargs):
        """Wrapper around the original edit/send function that finalises
        progressive UI first. Every exit path in _run_search goes through
        edit_fn (it's how we render the final state), so attaching cleanup
        here covers all branches without per-branch duplication.
        """
        await _finalize_progressive()
        return await _raw_edit_fn(*args, **kwargs)

    # --- Search: Jackett first (preferred), Rutracker direct as fallback ON ERROR only ---
    #
    # Critical distinction (see CLAUDE.md → "Search fallback policy"):
    #   - Jackett ERRORED    → try Rutracker direct as alternative source
    #   - Jackett RETURNED [] → trust as authoritative «no matches», SKIP Rutracker
    #     fallback (it's currently broken at search/login pages anyway) and go
    #     straight to the no-results screen so the user sees did-you-mean.
    results_data = []
    banner = ""
    source = "rutracker"
    jackett_errored = False
    jackett_err_msg = ""
    both_sources_unavailable = False

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
                jackett_errored = True
                jackett_err_msg = str(e)
                if rutracker_client:
                    banner = "⚠️ Jackett недоступен, ищу напрямую в Rutracker"
                    # fall through to Rutracker path below
                else:
                    await edit_fn(
                        _search_source_error_text("jackett", str(e)),
                        reply_markup=_search_error_keyboard(),
                        parse_mode="HTML",
                    )
                    return ConversationHandler.END

        selected: set[str] = context.user_data.get("srch_jackett_selected", set())

        if selected and not jackett_errored:  # Jackett search
            try:
                # PR2 prefetch hit: if a previous did-you-mean prefetch fired
                # for THIS exact base_query, use its result instead of doing a
                # fresh network call. Massive UX win for the «typo → tap
                # suggestion → instant» flow.
                if (prefetch and prefetch[0] == base_query
                        and not prefetch[1].cancelled()):
                    prefetch_task = prefetch[1]
                    if prefetch_task.done():
                        cached = prefetch_task.result()
                        if cached is not None:
                            j_results_raw = list(cached)
                            logger.info(
                                "Search: didmean prefetch HIT for %r — %d results from cache",
                                base_query, len(j_results_raw),
                            )
                        else:
                            j_results_raw = None  # prefetch failed → fall back
                    else:
                        # Still running — wait briefly. If it completes in <5s
                        # we save the rest; if not, fall back to fresh call.
                        try:
                            cached = await asyncio.wait_for(
                                asyncio.shield(prefetch_task), timeout=5.0,
                            )
                            j_results_raw = list(cached) if cached is not None else None
                            if j_results_raw is not None:
                                logger.info(
                                    "Search: didmean prefetch awaited (%d results) for %r",
                                    len(j_results_raw), base_query,
                                )
                        except (asyncio.TimeoutError, Exception):
                            j_results_raw = None
                    # Consume the slot whether hit/miss — it served its purpose.
                    context.user_data.pop("srch_didmean_prefetch", None)
                else:
                    j_results_raw = None
                    # Different query — prefetch is stale, cancel it.
                    if prefetch:
                        _cancel_didmean_prefetch(context)

                if j_results_raw is None:
                    # Normal path: no prefetch or prefetch unusable.
                    j_results_raw = await asyncio.wait_for(
                        asyncio.to_thread(
                            jackett_client.search,
                            base_query,  # quality suffix stripped — we filter client-side
                            indexers=list(selected),
                            fetch_limit=JACKETT_FETCH_LIMIT,
                        ),
                        timeout=JACKETT_SEARCH_TIMEOUT_SECONDS + 5.0,
                    )
            except (JackettError, asyncio.TimeoutError) as e:
                raw_err = (
                    f"Jackett не ответил за {int(JACKETT_SEARCH_TIMEOUT_SECONDS)} сек — проверьте Global timeout в настройках Jackett"
                    if isinstance(e, asyncio.TimeoutError) else str(e)
                )
                logger.error("Jackett search failed in _run_search: %s", raw_err)
                jackett_errored = True
                jackett_err_msg = raw_err
                if rutracker_client:
                    banner = "⚠️ Jackett временно недоступен, ищу напрямую в Rutracker"
                    # fall through to Rutracker path
                else:
                    await edit_fn(
                        _search_source_error_text("jackett", raw_err),
                        reply_markup=_search_error_keyboard(),
                        parse_mode="HTML",
                    )
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
                        "series": _extract_series_base_query(r.title) is not None,
                        "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
                        "magnet_url": r.magnet_url,
                        "torrent_url": r.torrent_url,
                        "tracker_name": r.tracker,
                    })
                source = "jackett"
                logger.info(
                    "Search: Jackett returned %d results for %r",
                    len(j_results_raw), search_query,
                )

    # Rutracker direct path runs in two distinct contexts:
    #   A) Jackett client not configured at all → Rutracker is the ONLY source,
    #      so RutrackerError is fatal (existing behaviour).
    #   B) Jackett configured but errored → Rutracker is a fallback,
    #      so RutrackerError is NON-fatal — we fall through to the no-results
    #      screen with did-you-mean + a banner explaining both sources failed.
    # We do NOT enter Rutracker when Jackett succeeded with 0 results — that's
    # an authoritative «no match», and trying Rutracker direct (often broken)
    # only swaps the friendly no-results screen for a hard error dead-end.
    rutracker_is_only_source = (jackett_client is None) and (rutracker_client is not None)
    rutracker_is_fallback = jackett_errored and not results_data and (rutracker_client is not None)

    if rutracker_is_only_source or rutracker_is_fallback:
        try:
            # Same Strategy 2 reasoning as Jackett above — pass base_query
            # so the tracker doesn't text-filter on «1080p».
            rt_results = await asyncio.to_thread(rutracker_client.search, base_query)
        except RutrackerError as rt_err:
            if rutracker_is_only_source:
                # Context A — pure-Rutracker install. Nothing to fall back to.
                await edit_fn(
                    _search_source_error_text("rutracker", str(rt_err)),
                    reply_markup=_search_error_keyboard(), parse_mode="HTML",
                )
                return ConversationHandler.END
            # Context B — both sources down. Fall through to the dedicated
            # temporary-failure screen below; did-you-mean would be misleading.
            logger.warning("Rutracker fallback also failed for %r: %s", search_query, rt_err)
            both_sources_unavailable = True
            rt_results = []
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
                "series": _extract_series_base_query(r.title) is not None,
                "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
                "magnet_url": None,
                "torrent_url": None,
                "tracker_name": "rutracker",
            })
        if rt_results:
            source = "rutracker"

    # Diagnostic for the search-fallback investigation: log when we DIDN'T fall
    # back to Rutracker because Jackett successfully returned 0 results.
    if not results_data and not jackett_errored and jackett_client is not None:
        logger.info(
            "Search: skipping Rutracker fallback for %r — Jackett returned 0 "
            "(treating as authoritative no-match, will show did-you-mean)",
            search_query,
        )

    if not results_data and not rutracker_client and not jackett_client:
        await edit_fn("Поиск недоступен: не настроен ни Rutracker, ни Jackett.", reply_markup=_search_error_keyboard(), parse_mode="HTML")
        return ConversationHandler.END

    if not results_data:
        has_quality, jackett_can_expand = _no_results_flags(context, search_query)
        if both_sources_unavailable:
            _remember_didmean_suggestions(context, [])
            _cancel_didmean_prefetch(context)
            no_results_text = (
                "⚠️ Поиск временно не получился\n\n"
                f"Запрос {_format_search_query_label(search_query)} не удалось проверить: "
                "источники поиска сейчас не ответили.\n\n"
                "Это не значит, что раздач нет. Повторите поиск через минуту. "
                "Если проблема повторяется, проверьте диагностику в /admin."
            )
            await edit_fn(
                no_results_text,
                reply_markup=_no_results_keyboard(
                    has_quality=False,
                    jackett_can_expand=False,
                    suggestions=[],
                ),
            )
            return SEARCH_RESULTS

        # === Failure-vs-empty distinction (Problem A) =====================
        # If Jackett errored AND user's selected indexers covered trackers
        # BEYOND rutracker, the RT-direct fallback we just ran only saw one
        # source — the 0-result outcome doesn't mean «not anywhere», it
        # means «we lost coverage of the trackers Jackett would have hit».
        # Skip did-you-mean entirely (misleading) — show retry.
        is_rt_only = _is_rutracker_only_indexer_set(
            context.user_data.get("srch_jackett_selected"),
            context.user_data.get("srch_jackett_indexers"),
        )
        multi_tracker_coverage_lost = jackett_errored and not is_rt_only
        if multi_tracker_coverage_lost:
            logger.info(
                "Search: skipping did-you-mean for %r — Jackett errored and "
                "selection covered non-rutracker indexers (coverage lost, not "
                "definitive 0)", search_query,
            )
            suggestions: list[str] = []
            original_kp_match = False  # don't bother checking
        else:
            # === KP-verify original query (Problem B) =====================
            # If KP confirms the original query itself, we should NOT confuse
            # the user with did-you-mean variations — instead say «найден на
            # КП, но в трекерах сейчас нет». Run in parallel with the GPT
            # did-you-mean call to keep latency at ~1s total.
            kp_info = context.user_data.get("srch_kp_info")
            from_kp_link = bool(
                context.user_data.get("srch_from_kp_link")
                and isinstance(kp_info, dict)
                and kp_info.get("kp_id")
            )
            if from_kp_link:
                original_kp_match = True
                suggestions = await _gpt_get_did_you_mean(base_query)
            else:
                kp_task = asyncio.create_task(
                    asyncio.to_thread(
                        _kp_verify_title_sync, base_query,
                        default_on_unknown=False,  # don't suppress did-you-mean on KP outage
                    )
                )
                suggestions_task = asyncio.create_task(_gpt_get_did_you_mean(base_query))
                try:
                    original_kp_match, suggestions = await asyncio.gather(
                        kp_task, suggestions_task,
                    )
                except Exception:
                    logger.warning("KP/did-you-mean parallel fetch failed", exc_info=True)
                    original_kp_match = False
                    suggestions = []

            if not original_kp_match and not suggestions:
                suggestions = await asyncio.to_thread(_kp_loose_suggestions_sync, base_query)

            # If user's original query IS on KP, swap GPT suggestions for a
            # single «искать снова» hint — they didn't mistype.
            if original_kp_match:
                logger.info(
                    "Search: original query %r found on KP — suppressing "
                    "did-you-mean (no typo)", base_query,
                )
                suggestions = []

        suggestions = _remember_didmean_suggestions(context, suggestions)
        advice = None
        if not multi_tracker_coverage_lost and not original_kp_match:
            advice = await _gpt_get_search_failure_advice(
                search_query,
                base_query=base_query,
                preferred_quality=preferred_quality,
                audio_required=audio_required,
                subs_required=subs_required,
                has_quality=has_quality,
                jackett_can_expand=jackett_can_expand,
                season_requested=bool(_extract_season_from_query(search_query)),
                source_status=("tracker_failure" if jackett_errored else "empty"),
                suggestions=suggestions,
            )
            extra_suggestions = (advice or {}).get("suggested_queries") or []
            if extra_suggestions:
                suggestions = _remember_didmean_suggestions(
                    context, [*suggestions, *extra_suggestions],
                )

        # Prefetch (Proposal #2): fire a background Jackett search for the
        # TOP-1 suggestion while the user reads the buttons. Top-1 only —
        # the GPT prompt guarantees array index 0 is the most likely.
        if suggestions and jackett_client is not None:
            _cancel_didmean_prefetch(context)  # belt-and-suspenders
            top_suggestion = suggestions[0]
            top_base, _q, _a, _s = _split_query_settings(top_suggestion)
            indexer_list = list(context.user_data.get("srch_jackett_selected") or [])
            if top_base and indexer_list:
                prefetch_task = asyncio.create_task(
                    _didmean_prefetch_jackett(top_base, indexer_list)
                )
                context.user_data["srch_didmean_prefetch"] = (top_base, prefetch_task)
                logger.info(
                    "Search: didmean prefetch started for top suggestion %r (base=%r)",
                    top_suggestion, top_base,
                )
        # Compose text: optional banner (e.g. «both sources down») + framing
        # that emphasises did-you-mean buttons when we have any.
        no_results_text = _search_empty_text(
            search_query,
            preferred_quality=preferred_quality,
            audio_required=audio_required,
            subs_required=subs_required,
            banner=banner,
        )
        advice_text = _format_search_failure_advice(advice)
        if multi_tracker_coverage_lost:
            # New branch (A): Jackett errored + we lost non-rutracker coverage.
            # Don't suggest variants — push the user toward a retry instead.
            no_results_text = (
                f"{no_results_text}\n\n"
                "⚠️ Поисковики ответили не полностью — это похоже на временный сбой.\n"
                "Лучшее следующее действие: повторить поиск через минуту."
            )
        elif original_kp_match:
            # New branch (B): user's query is a real title on KP, just not on
            # trackers right now. Don't shove «возможно вы имели в виду» at
            # them — they typed correctly.
            no_results_text = (
                f"{no_results_text}\n\n"
                "🎬 Этот фильм/сериал найден на Кинопоиске, но в трекерах "
                "сейчас не доступен. Попробуйте позже или поищите по "
                "оригинальному названию."
            )
        elif advice_text:
            no_results_text = f"{no_results_text}\n\n{advice_text}"
        elif suggestions:
            no_results_text = (
                f"{no_results_text}\n\n"
                "🤖 Возможно вы имели в виду — попробуйте вариант ниже "
                "или измените запрос вручную."
            )
        else:
            no_results_text = (
                f"{no_results_text}\n\n"
                "Можно ослабить фильтры, расширить список трекеров или попробовать другой вариант названия."
            )
        await edit_fn(
            no_results_text,
            reply_markup=_no_results_keyboard(
                has_quality=has_quality,
                jackett_can_expand=jackett_can_expand,
                suggestions=suggestions,
            ),
        )
        return SEARCH_RESULTS

    # --- Strategy 2: client-side filters (quality + audio + subs) ---
    # Apply audio/subs FIRST (they're presence flags — narrows the pool but
    # doesn't change the quality landscape). Quality filter runs after so the
    # «found in other quality» banner reflects what's actually available
    # given the audio/subs preference.
    results_data, filter_banner_parts = _apply_audio_subs_preferences(
        results_data,
        context,
        audio_required=audio_required,
        subs_required=subs_required,
    )

    # Audio/subs preferences now keep alternatives when there is no exact
    # match. This branch remains as a defensive guard for edge cases where
    # another presence filter produced an empty set.
    if not results_data and filter_banner_parts:
        has_quality, jackett_can_expand = _no_results_flags(context, search_query)
        suggestions = await _gpt_get_did_you_mean(base_query)
        suggestions = _remember_didmean_suggestions(context, suggestions)
        advice = await _gpt_get_search_failure_advice(
            search_query,
            base_query=base_query,
            preferred_quality=preferred_quality,
            audio_required=audio_required,
            subs_required=subs_required,
            has_quality=has_quality,
            jackett_can_expand=jackett_can_expand,
            season_requested=bool(_extract_season_from_query(search_query)),
            source_status="filters_empty",
            suggestions=suggestions,
        )
        extra_suggestions = (advice or {}).get("suggested_queries") or []
        if extra_suggestions:
            suggestions = _remember_didmean_suggestions(
                context, [*suggestions, *extra_suggestions],
            )
        text = (
            _search_empty_text(
                search_query,
                preferred_quality=preferred_quality,
                audio_required=audio_required,
                subs_required=subs_required,
                body="\n".join(filter_banner_parts),
                action_hint=(
                    _format_search_failure_advice(advice)
                    or "Можно отключить Original/субтитры в настройках поиска и попробовать снова."
                ),
            )
        )
        await edit_fn(
            text,
            reply_markup=_no_results_keyboard(
                has_quality=has_quality,
                jackett_can_expand=jackett_can_expand,
                suggestions=suggestions,
            ),
        )
        return SEARCH_RESULTS

    media_intent = _detect_media_intent(base_query)
    before_media_intent = len(results_data)
    results_data, media_intent_banner = _apply_media_intent_filter(results_data, media_intent)
    if media_intent_banner:
        filter_banner_parts.append(media_intent_banner)
        logger.info(
            "Search: media intent %s kept %d/%d for %r",
            media_intent, len(results_data), before_media_intent, base_query,
        )

    # Classify all results by detected quality (1080p / 2160p / 720p / other).
    # If the user asked for a specific quality:
    #   - filter to that bucket if non-empty (standard case)
    #   - else show ALL with a banner «в <quality> ничего, есть в других»
    #     (avoids the «0 results» dead-end when the film exists in other quality)
    quality_banner = ""
    if preferred_quality:
        buckets = _classify_results_by_quality(results_data)
        preferred_bucket = buckets.get(preferred_quality) or []
        if preferred_bucket:
            other_stats = _format_quality_stats(buckets, exclude=preferred_quality)
            stats_suffix = f". Также есть: {other_stats}" if other_stats else ""
            quality_banner = (
                f"🎬 Найдено {len(results_data)} раздач, "
                f"показаны {len(preferred_bucket)} в {preferred_quality}{stats_suffix}."
            )
            results_data = preferred_bucket
            logger.info(
                "Search: quality filter %s kept %d/%d for %r",
                preferred_quality, len(preferred_bucket), len(results_data) + sum(
                    len(v) for k, v in buckets.items() if k != preferred_quality
                ), base_query,
            )
        else:
            # Preferred bucket empty but other qualities have content → show
            # everything with a banner. Better than hiding behind no-results.
            other_stats = _format_quality_stats(buckets)
            quality_banner = (
                f"⚠️ В {preferred_quality} ничего не найдено. "
                f"Показаны все качества: {other_stats}."
            )
            logger.info(
                "Search: %s bucket empty, falling back to all qualities (%d total) for %r",
                preferred_quality, len(results_data), base_query,
            )

    series_master_banner = ""
    if series_master:
        before = len(results_data)
        results_data = [
            r for r in results_data
            if r.get("series") or _extract_series_base_query(str(r.get("title") or ""))
        ]
        if not results_data:
            has_quality, jackett_can_expand = _no_results_flags(context, search_query)
            await edit_fn(
                "Не нашёл сериальных раздач.\n\n"
                "Попробуйте другое название или уберите часть фильтров.",
                reply_markup=_no_results_keyboard(
                    has_quality=has_quality,
                    jackett_can_expand=jackett_can_expand,
                ),
            )
            return SEARCH_RESULTS
        if before != len(results_data):
            filter_banner_parts.append(f"📚 Оставлены сериальные раздачи: {len(results_data)}/{before}.")
        series_master_banner = (
            "📚 Эталонная раздача для сериала\n"
            "Выберите раздачу, по которой я пойму качество, тип релиза и возможные озвучки. "
            "Скачивание начнётся только после плана и подтверждения."
        )

    results_data, voice_banner = _apply_voice_preferences(results_data, context)
    if voice_banner:
        filter_banner_parts.append(voice_banner)

    # --- Cluster picker (Proposal #1): when the same query returns ≥2
    # distinct (title, year) films with ≥2 releases each, show a picker
    # «Какую Дюну вы ищете?» so the user can narrow to one film in one tap.
    # Common single-film queries skip this entirely (conditional show).
    # Skip when a season filter is active — user asked for specific season,
    # showing «which series?» would conflict with their intent.
    _maybe_season = _extract_season_from_query(search_query)
    if _maybe_season is None:
        clusters = _build_search_clusters(results_data)
        picker_clusters = _clusters_for_query_picker(clusters, base_query)
        await _enrich_clusters_with_plex_hints(picker_clusters, preferred_quality)
        picker_is_focused = any(
            _cluster_query_relevance(cluster, base_query) >= 2
            for cluster in picker_clusters
        )
        if _should_show_cluster_picker(
            picker_clusters,
            total_clusters=len(clusters),
            filtered_for_query=picker_is_focused,
        ):
            # Stash full results + clusters for the picker callback. The user
            # may choose a single cluster (filter) or «show all» (use original).
            context.user_data["srch_results_full"] = list(results_data)
            context.user_data["srch_clusters"] = clusters
            context.user_data["srch_picker_clusters"] = picker_clusters
            context.user_data["srch_source"] = source
            # Banner: combine source-fallback + filter banners (quality/audio/subs).
            # combined_banner isn't computed until after season filter further
            # down, so we build it inline here for the picker case.
            picker_banner = "\n".join(
                b for b in (series_master_banner, banner, *filter_banner_parts, quality_banner) if b
            )
            context.user_data["srch_banner"] = picker_banner
            await edit_fn(
                _cluster_picker_text_with_hints(search_query, picker_clusters, picker_banner),
                reply_markup=_cluster_picker_keyboard(
                    picker_clusters,
                    total_count=len(results_data),
                ),
                parse_mode="HTML",
            )
            return SEARCH_RESULTS

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
            has_quality, jackett_can_expand = _no_results_flags(context, search_query)
            _remember_didmean_suggestions(context, [])
            if has_quality:
                await edit_fn(
                    _search_empty_text(
                        search_query,
                        preferred_quality=preferred_quality,
                        audio_required=audio_required,
                        subs_required=subs_required,
                        body="Раздачи есть, но с указанным качеством сезон не нашёл.",
                        action_hint="Лучшее следующее действие: искать без ограничения качества.",
                    ),
                    reply_markup=_no_results_keyboard(
                        has_quality=True,
                        jackett_can_expand=jackett_can_expand,
                    ),
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
                    _search_empty_text(
                        search_query,
                        preferred_quality=preferred_quality,
                        audio_required=audio_required,
                        subs_required=subs_required,
                        body=f"На трекерах найдены сезоны: {seasons_str}.",
                        action_hint="Можно вернуться к выбору сезона.",
                    ),
                    reply_markup=_season_back_to_picker_keyboard(),
                )
                return SEARCH_SEASON_SELECT

            # Generic dead-end after season filter wiped everything: offer to
            # broaden trackers (the requested season may exist elsewhere).
            advice = await _gpt_get_search_failure_advice(
                search_query,
                base_query=base_query,
                preferred_quality=preferred_quality,
                audio_required=audio_required,
                subs_required=subs_required,
                has_quality=False,
                jackett_can_expand=jackett_can_expand,
                season_requested=True,
                source_status="season_empty",
                suggestions=[],
            )
            suggestions = _remember_didmean_suggestions(
                context, (advice or {}).get("suggested_queries") or [],
            )
            await edit_fn(
                _search_empty_text(
                    search_query,
                    preferred_quality=preferred_quality,
                    audio_required=audio_required,
                    subs_required=subs_required,
                    action_hint=(
                        _format_search_failure_advice(advice)
                        or "Можно расширить поиск или попробовать другой вариант названия сезона."
                    ),
                ),
                reply_markup=_no_results_keyboard(
                    has_quality=False,
                    jackett_can_expand=jackett_can_expand,
                    suggestions=suggestions,
                ),
            )
            return SEARCH_RESULTS

    # --- Step 2: sort by score, best first ---
    results_data.sort(key=_search_result_sort_score, reverse=True)
    results_data[0]["recommended"] = True

    # PR3: enrich top-10 with GPT-parsed metadata (badges in card UI).
    # Runs after sort so the right results get the badges (top of list);
    # cache makes repeat searches instant. Silent no-op when GPT_ENABLED=false.
    await _enrich_top_results_with_metadata(results_data, max_n=10)
    await _enrich_results_with_plex_hints(results_data, preferred_quality, max_n=15)

    # Combine all banners (in display order): source-fallback message,
    # audio/subs filter stats, quality filter stats. Any may be empty.
    combined_banner = "\n".join(
        b for b in (series_master_banner, banner, *filter_banner_parts, quality_banner) if b
    )

    context.user_data["srch_results"] = results_data
    context.user_data["srch_results_page"] = 0
    # R.2 pre-warm: kick off background Plex season fetch for the first
    # partial-season result so the eventual confirm dialog is instant.
    _maybe_prewarm_plex_for_results(
        context, _chat_id_from_query(query) if "query" in locals() else None,
        results_data,
    )
    context.user_data["srch_banner"] = combined_banner
    context.user_data["srch_source"] = source

    await edit_fn(
        _build_results_text(results_data, search_query, 0, banner=combined_banner),
        reply_markup=_search_results_keyboard(
            results_data, page=0,
            show_switch_trackers=bool(jackett_client and source == "jackett"),
            show_retry_jackett=bool(jackett_client and source == "rutracker"),
            show_direct_rutracker=bool(rutracker_client and source == "jackett"),
            show_back_to_cluster_picker=bool(context.user_data.get("srch_cluster_picker_return")),
            series_master=series_master,
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
            show_back_to_cluster_picker=bool(context.user_data.get("srch_cluster_picker_return")),
            series_master=_search_is_series_master(context),
        ),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return SEARCH_RESULTS


async def search_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Quick search using the user's current settings (quality / audio / subs).

    Renamed from the legacy «Быстрый поиск с 1080p» — quality is no longer
    hardcoded; we honour whatever the user picked in /search settings.
    """
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("srch_query", "")
    settings = context.user_data.get("srch_settings", _search_settings_for_chat(_chat_id_from_query(query)))
    return await _execute_search(query, context, _build_current_mode_search_query(context, base, settings))


async def search_pick_cluster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle cluster picker selection from the «Какую Дюну?» screen.

    Callback format: «srch:cluster:<idx>» where idx is an integer (cluster
    index in srch_clusters) or the literal «all» to bypass filtering.

    Reads the stashed `srch_results_full` (set in _run_search when the
    picker was shown), filters it to the chosen cluster's indices, and
    re-renders the standard results screen. Stays in SEARCH_RESULTS state.
    """
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":", 2)
    if len(parts) < 3:
        await query.edit_message_text(
            "Не удалось разобрать выбор кластера.",
            reply_markup=_search_error_keyboard(),
        )
        return SEARCH_RESULTS
    pick = parts[2]

    full = context.user_data.get("srch_results_full") or []
    clusters = context.user_data.get("srch_clusters") or []
    picker_clusters = context.user_data.get("srch_picker_clusters") or []
    if not full or not clusters:
        await query.edit_message_text(
            "Данные кластера потеряны — начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return SEARCH_RESULTS

    # «all» → show every result (skip the cluster filter); otherwise pick by idx.
    if pick == "all":
        results_data = list(full)
    else:
        try:
            cluster_idx = int(pick)
        except ValueError:
            await query.edit_message_text(
                "Неверный индекс кластера.",
                reply_markup=_search_error_keyboard(),
            )
            return SEARCH_RESULTS
        # Use the exact visible cluster list rendered by _run_search. Fallback
        # to the old >=2 rule for stale sessions created before this state key.
        visible = picker_clusters or [c for c in clusters if c["count"] >= 2]
        if cluster_idx < 0 or cluster_idx >= len(visible):
            await query.edit_message_text(
                "Кластер не найден.",
                reply_markup=_search_error_keyboard(),
            )
            return SEARCH_RESULTS
        chosen = visible[cluster_idx]
        indices = set(chosen.get("indices") or [])
        results_data = [r for i, r in enumerate(full) if i in indices]

    # Standard post-cluster render: sort by score, mark top as recommended,
    # delegate to the existing results keyboard. Picker state stays in user_data
    # so «⬅️ К вариантам» can restore the original chooser.
    if results_data:
        results_data.sort(key=_search_result_sort_score, reverse=True)
        results_data[0]["recommended"] = True

    # PR3: enrich top-10 of the cluster slice. Cluster results are typically
    # a subset of what was fetched, so most are likely cache hits (we already
    # enriched the full set in _run_search above) — fast in practice.
    await _enrich_top_results_with_metadata(results_data, max_n=10)
    await _enrich_results_with_plex_hints(
        results_data,
        context.user_data.get("srch_preferred_quality"),
        max_n=15,
    )

    search_query = context.user_data.get("srch_search_query", "")
    source = context.user_data.get("srch_source", "")
    banner = context.user_data.get("srch_banner", "")

    context.user_data["srch_results"] = results_data
    context.user_data["srch_results_page"] = 0
    # R.2 pre-warm: kick off background Plex season fetch for the first
    # partial-season result so the eventual confirm dialog is instant.
    _maybe_prewarm_plex_for_results(
        context, _chat_id_from_query(query) if "query" in locals() else None,
        results_data,
    )
    # Keep picker state so the user can back out if they chose the wrong film.
    context.user_data["srch_cluster_picker_return"] = True

    await query.edit_message_text(
        _build_results_text(results_data, search_query, 0, banner=banner),
        reply_markup=_search_results_keyboard(
            results_data, page=0,
            show_switch_trackers=bool(jackett_client and source == "jackett"),
            show_retry_jackett=bool(jackett_client and source == "rutracker"),
            show_direct_rutracker=bool(rutracker_client and source == "jackett"),
            show_back_to_cluster_picker=True,
            series_master=_search_is_series_master(context),
        ),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return SEARCH_RESULTS


async def search_cluster_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from filtered cluster results to the original cluster picker."""
    query = update.callback_query
    await query.answer()

    full = context.user_data.get("srch_results_full") or []
    picker_clusters = context.user_data.get("srch_picker_clusters") or []
    search_query = context.user_data.get("srch_search_query", "")
    banner = context.user_data.get("srch_banner", "")

    if not full or not picker_clusters or not search_query:
        await query.edit_message_text(
            "Варианты потеряны — начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        context.user_data.pop("srch_cluster_picker_return", None)
        return SEARCH_RESULTS

    await query.edit_message_text(
        _cluster_picker_text_with_hints(search_query, picker_clusters, banner),
        reply_markup=_cluster_picker_keyboard(
            picker_clusters,
            total_count=len(full),
        ),
        parse_mode="HTML",
    )
    return SEARCH_RESULTS


async def search_didmean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Run search with a GPT-suggested alternative query, preserving the
    user's existing search settings (quality, audio, subs).

    Triggered by tapping one of the «🔍 <текст>» suggestion buttons on a
    no-results screen. The suggestion is a clean TITLE fix; we re-attach
    the user's preferences via _build_search_query so the new search keeps
    «1080p / Original / Sub» filtering intent — otherwise tapping
    «Дюна» on a «Дюра 1080p» miss would silently drop the quality
    preference and show unrelated qualities.
    """
    query = update.callback_query
    await query.answer()
    # Current callback shape is "srch:didmean:<index>" to stay under
    # Telegram's 64-byte callback_data limit.
    prefix = f"{SEARCH_CALLBACK_PREFIX}:didmean:"
    raw = query.data or ""
    token = raw[len(prefix):].strip() if raw.startswith(prefix) else ""
    suggestion = ""
    stored = context.user_data.get("srch_didmean_suggestions") or []
    if token.isdigit():
        idx = int(token)
        if 0 <= idx < len(stored):
            suggestion = str(stored[idx]).strip()
    if not suggestion:
        await query.edit_message_text(
            "Подсказка потеряна. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    settings = context.user_data.get("srch_settings", dict(_SRCH_DEFAULT_SETTINGS))
    full_query = _build_current_mode_search_query(context, suggestion, settings)
    context.user_data["srch_query"] = suggestion
    context.user_data["srch_search_query"] = full_query
    return await _execute_search(query, context, full_query)


async def search_no_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Повторить поиск без фильтра качества (фоллбэк при 0 результатов)."""
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("srch_query", "").strip()
    if not base:
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    return await _execute_search(query, context, _search_base_for_current_mode(context, base))


async def search_expand_all_trackers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Расширить выбор Jackett-индексеров до всех доступных и повторить запрос.

    Used as a fallback after a 0-results dead-end — broadens the search from the
    default (Rutracker-only) to every indexer Jackett knows about, keeping the
    same query (including any quality suffix).
    """
    query = update.callback_query
    await query.answer()
    indexers = context.user_data.get("srch_jackett_indexers") or []
    all_ids = {i["id"] for i in indexers}
    if not all_ids:
        await query.edit_message_text(
            "Jackett-индексеры неизвестны. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    context.user_data["srch_jackett_selected"] = all_ids
    sq = context.user_data.get("srch_search_query") or context.user_data.get("srch_query", "")
    if not sq:
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    return await _execute_search(query, context, sq)


async def search_no_quality_all_trackers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Combined fallback: drop quality filter AND broaden trackers, then retry."""
    query = update.callback_query
    await query.answer()
    indexers = context.user_data.get("srch_jackett_indexers") or []
    all_ids = {i["id"] for i in indexers}
    if all_ids:
        context.user_data["srch_jackett_selected"] = all_ids
    base = context.user_data.get("srch_query", "").strip()
    if not base:
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    return await _execute_search(query, context, _search_base_for_current_mode(context, base))


async def search_retry_dl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retry the same download that just failed, by index into ``srch_results``.

    Shown as «🔄 Повторить» on the error screen after a torrent download
    failure. Re-runs ``_download_and_add`` with the same index; if it fails
    again, the user sees the error screen again (and can keep retrying).
    """
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    # Restore subscribe-intent saved by the original _download_and_add call —
    # without this, retrying «⬇️📺 Серии» / «⬇️🎯 Сезон» after a failure would
    # silently become a plain one-shot download.
    return await _download_and_add(
        query, context, index,
        subscribe=bool(context.user_data.get("srch_last_subscribe", False)),
        notify_policy=context.user_data.get("srch_last_notify_policy"),
        download_policy=context.user_data.get("srch_last_download_policy"),
    )


async def search_queue_dl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Add the failed download to the pending queue for background retry.

    Shown as «⏳ Поставить в очередь» on the error screen, only when
    ``_pending_downloads_enabled()`` is True. The background loop tries the
    same Jackett → rutracker_client → magnet chain every
    ``PENDING_DOWNLOADS_INTERVAL_SECONDS`` and gives up after
    ``PENDING_DOWNLOADS_TTL_HOURS``.
    """
    query = update.callback_query
    await query.answer()
    if not _pending_downloads_enabled():
        await query.edit_message_text(
            "Очередь отложенных загрузок отключена.",
            reply_markup=_task_error_keyboard(),
        )
        return ConversationHandler.END
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Запрос потерян.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    result = results[index]
    chat_id = query.message.chat.id if query.message else None
    last_error = str(context.user_data.get("srch_last_dl_error") or "")

    entry_id, entry = _queue_pending_download_from_result(
        result,
        chat_id=chat_id,
        subscribe=bool(context.user_data.get("srch_last_subscribe", False)),
        notify_policy=context.user_data.get("srch_last_notify_policy"),
        download_policy=context.user_data.get("srch_last_download_policy"),
        error=last_error,
    )

    interval_min = max(1, PENDING_DOWNLOADS_INTERVAL_SECONDS // 60)
    title_text = entry["title"][:80]
    await query.edit_message_text(
        f"⏳ «{title_text}» поставлено в очередь.\n"
        f"Попробую скачать снова через ~{interval_min} мин.\n"
        f"Если за {PENDING_DOWNLOADS_TTL_HOURS:g}ч не получится — пришлю уведомление об отказе.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
        ]]),
    )
    return ConversationHandler.END


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
            _search_advanced_text(base, context),
            reply_markup=_search_advanced_keyboard(settings, tracker_label, _search_intent(context)),
        )
        return SEARCH_ADVANCED

    await query.edit_message_text(
        _search_options_text(base, context),
        reply_markup=_search_options_keyboard(tracker_label, _search_intent(context)),
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
    confirm_label = "💾 Применить" if return_to in ("options", "advanced") else "🔍 Искать"
    show_back = return_to in ("options", "advanced")
    await query.edit_message_text(
        f"Поиск: {_format_search_query_label(search_query)}\nВыберите трекеры для поиска:",
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
        confirm_label = "💾 Применить" if return_to_err in ("options", "advanced") else "🔍 Искать"
        show_back = return_to_err in ("options", "advanced")
        search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
        await query.edit_message_text(
            f"Поиск: {_format_search_query_label(search_query)}\nВыберите трекеры для поиска:",
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
                _search_advanced_text(base, context),
                reply_markup=_search_advanced_keyboard(settings, tracker_label, _search_intent(context)),
            )
            return SEARCH_ADVANCED
        else:
            await query.edit_message_text(
                _search_options_text(base, context),
                reply_markup=_search_options_keyboard(tracker_label, _search_intent(context)),
            )
            return SEARCH_OPTIONS

    # --- "results" mode: run the unified search path immediately ---
    await query.answer()
    search_query = context.user_data.get("srch_search_query", context.user_data.get("srch_query", ""))
    if not search_query:
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    return await _execute_search(query, context, search_query)


def _search_mode_return_to(value: str | None) -> str:
    return "advanced" if value == "advanced" else "options"


def _search_mode_state(return_to: str) -> int:
    return SEARCH_ADVANCED if return_to == "advanced" else SEARCH_OPTIONS


async def _render_search_settings(query, context: ContextTypes.DEFAULT_TYPE, return_to: str) -> int:
    return_to = _search_mode_return_to(return_to)
    base = context.user_data.get("srch_query", "")
    tracker_label = _tracker_label_from_context(context)
    if return_to == "advanced":
        settings = context.user_data.get("srch_settings", dict(_SRCH_DEFAULT_SETTINGS))
        await query.edit_message_text(
            _search_advanced_text(base, context),
            reply_markup=_search_advanced_keyboard(settings, tracker_label, _search_intent(context)),
        )
    else:
        await query.edit_message_text(
            _search_options_text(base, context),
            reply_markup=_search_options_keyboard(tracker_label, _search_intent(context)),
        )
    return _search_mode_state(return_to)


async def search_choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    return_to = _search_mode_return_to(parts[2] if len(parts) > 2 else "options")
    if _search_is_series_master(context):
        _clear_search_intent(context)
    else:
        context.user_data["srch_intent"] = SEARCH_INTENT_SERIES_MASTER
    return await _render_search_settings(query, context, return_to)


async def search_show_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показать расширенные настройки поиска."""
    query = update.callback_query
    await query.answer()
    settings = dict(context.user_data.get(
        "srch_settings",
        _search_settings_for_chat(_chat_id_from_query(query)),
    ))
    context.user_data["srch_settings"] = settings
    context.user_data.setdefault("srch_setting_sources", _default_search_setting_sources())
    base = context.user_data.get("srch_query", "")
    await query.edit_message_text(
        _search_advanced_text(base, context),
        reply_markup=_search_advanced_keyboard(
            settings,
            _tracker_label_from_context(context),
            _search_intent(context),
        ),
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
        _mark_search_setting_source(context, "quality", "explicit")
    elif action == "toggle" and value in ("audio", "subs"):
        settings[value] = not settings.get(value, False)
        _mark_search_setting_source(context, value, "explicit")

    base = context.user_data.get("srch_query", "")
    await query.edit_message_text(
        _search_advanced_text(base, context),
        reply_markup=_search_advanced_keyboard(
            settings,
            _tracker_label_from_context(context),
            _search_intent(context),
        ),
    )
    return SEARCH_ADVANCED


async def search_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запустить поиск с выбранными расширенными настройками."""
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("srch_query", "")
    settings = context.user_data.get("srch_settings", _search_settings_for_chat(_chat_id_from_query(query)))
    return await _execute_search(query, context, _build_current_mode_search_query(context, base, settings))


async def search_series_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'Другой сезон' button: offer season selection, then search."""
    query = update.callback_query
    await query.answer()

    series_query = context.user_data.pop("srch_series_query", "")
    if not series_query:
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["srch_base_title"] = series_query
    context.user_data["srch_query"] = series_query

    total_seasons: int | None = None

    # If KinoPoisk is available, look up the season count before offering a selector.
    if kinopoisk_client:
        await query.edit_message_text(f"🔍 Ищу информацию о «{series_query}»…")
        try:
            total_seasons = await asyncio.wait_for(
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


def _series_continue_close_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
    ]])


def _series_continue_progress_text() -> str:
    return (
        "📺 <b>Ищу сезоны для докачки</b>\n\n"
        "Сверяю неполные сезоны в Plex с историей загрузок.\n"
        "Покажу только варианты, которые можно уверенно продолжить."
    )


async def _series_continue_plex_shows_with_seasons() -> list["PlexShow"]:
    if not PLEX_ENABLED:
        return []
    if not _plex_shows_library and plex_client is not None:
        await _refresh_plex_library()

    shows: list[PlexShow] = []
    seen_ids: set[int] = set()
    for show in _plex_shows_library.values():
        marker = id(show)
        if marker in seen_ids:
            continue
        seen_ids.add(marker)
        await _plex_ensure_show_seasons_lite(show, focus_season=None)
        if show.seasons:
            shows.append(show)
    return shows


async def _series_continue_build_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
) -> dict:
    shows = await _series_continue_plex_shows_with_seasons()
    load_history = getattr(state_store, "load_download_history", None)
    history = load_history() if callable(load_history) else []
    state = {
        "mine": build_series_catch_up_candidates(
            shows,
            history,
            chat_id=chat_id,
            scope="mine",
        ),
        "all": build_series_catch_up_candidates(
            shows,
            history,
            chat_id=chat_id,
            scope="all",
        ),
        "scope": "mine",
        "page": 0,
    }
    context.user_data[CONTINUE_STATE_KEY] = state
    return state


def _series_continue_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    state = context.user_data.get(CONTINUE_STATE_KEY)
    return state if isinstance(state, dict) else {"mine": [], "all": [], "scope": "mine", "page": 0}


def _series_continue_candidates(state: dict, scope: str) -> list[SeriesCatchUpCandidate]:
    candidates = state.get(scope)
    return candidates if isinstance(candidates, list) else []


def _series_continue_candidate_title(candidate: SeriesCatchUpCandidate) -> str:
    return (
        candidate.identity.title
        or candidate.identity.original_title
        or candidate.identity.plex_guid
        or candidate.identity.plex_rating_key
        or "Без названия"
    )


def _series_continue_plex_line(candidate: SeriesCatchUpCandidate) -> str:
    if candidate.known_total > 0:
        return f"Plex: {candidate.present_count} из {candidate.known_total}"
    return f"Plex: {candidate.present_count} сер."


def _series_continue_profile_line(candidate: SeriesCatchUpCandidate) -> str:
    parts = [part for part in (candidate.quality, candidate.tracker) if part]
    if not parts:
        return ""
    return "Профиль: " + ", ".join(parts)


def _series_continue_candidate_line(index: int, candidate: SeriesCatchUpCandidate) -> str:
    title = html_module.escape(_series_continue_candidate_title(candidate))
    lines = [
        f"{index}. <b>{title}</b> — сезон {candidate.season_number}",
        f"   {_series_continue_plex_line(candidate)}",
    ]
    profile = _series_continue_profile_line(candidate)
    if profile:
        lines.append(f"   {html_module.escape(profile)}")
    if candidate.topic_id:
        lines.append("   Есть прошлая тема раздачи")
    elif candidate.source == "plex":
        lines.append("   Прошлая раздача неизвестна")
    return "\n".join(lines)


def _series_continue_list_text(state: dict, scope: str, page: int) -> str:
    candidates = _series_continue_candidates(state, scope)
    all_count = len(_series_continue_candidates(state, "all"))
    mode = "🙋 Моё" if scope == "mine" else "🌐 Всё"
    if not candidates:
        if scope == "mine" and all_count:
            return (
                "📺 <b>Докачать сезон</b>\n\n"
                "В ваших кандидатах пока пусто.\n\n"
                "Здесь появляются сезоны Plex, которые можно продолжить по вашим загрузкам.\n\n"
                "В общей медиатеке есть варианты. Переключитесь на «Всё», "
                "если хотите посмотреть их."
            )
        return (
            "📺 <b>Докачать сезон</b>\n\n"
            "Пока не нашёл сезоны, которые можно уверенно продолжить.\n\n"
            "Что проверяю: неполные сезоны Plex и актуальные раздачи, "
            "которые подходят для продолжения.\n\n"
            "Почему может быть пусто: бот показывает только уверенные варианты. "
            "Если данных недостаточно, сезон не попадёт в список.\n\n"
            "Что можно сделать: нажать «Обновить» позже или найти продолжение обычным поиском."
        )

    total_pages = max(1, (len(candidates) + CONTINUE_PAGE_SIZE - 1) // CONTINUE_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * CONTINUE_PAGE_SIZE
    visible = candidates[start:start + CONTINUE_PAGE_SIZE]
    lines = [
        "📺 <b>Докачать сезон</b>",
        "",
        "Нашёл сезоны, где есть сигнал неполноты или прошлой раздачи.",
        f"Режим: {mode}",
        "",
    ]
    for offset, candidate in enumerate(visible, start=1):
        lines.append(_series_continue_candidate_line(start + offset, candidate))
        lines.append("")
    if total_pages > 1:
        lines.append(f"Страница {page + 1}/{total_pages}")
    return "\n".join(lines).strip()


def _series_continue_list_keyboard(state: dict, scope: str, page: int) -> InlineKeyboardMarkup:
    candidates = _series_continue_candidates(state, scope)
    total_pages = max(1, (len(candidates) + CONTINUE_PAGE_SIZE - 1) // CONTINUE_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * CONTINUE_PAGE_SIZE
    visible = candidates[start:start + CONTINUE_PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for offset, _candidate in enumerate(visible, start=1):
        index = start + offset - 1
        rows.append([
            InlineKeyboardButton(
                f"📺 {index + 1}",
                callback_data=f"{CONTINUE_CALLBACK_PREFIX}:open:{scope}:{index}",
            )
        ])

    rows.append([
        InlineKeyboardButton("🙋 Моё", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:mine:0"),
        InlineKeyboardButton("🌐 Всё", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:all:0"),
    ])
    if total_pages > 1:
        prev_page = max(0, page - 1)
        next_page = min(total_pages - 1, page + 1)
        rows.append([
            InlineKeyboardButton("⬅️", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:{scope}:{prev_page}"),
            InlineKeyboardButton("➡️", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:{scope}:{next_page}"),
        ])
    rows.append([
        InlineKeyboardButton(BUTTON_REFRESH, callback_data=f"{CONTINUE_CALLBACK_PREFIX}:refresh:{scope}"),
    ])
    rows.append([
        InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
    ])
    return InlineKeyboardMarkup(rows)


def _series_continue_detail_text(candidate: SeriesCatchUpCandidate) -> str:
    title = html_module.escape(_series_continue_candidate_title(candidate))
    completeness = resolve_series_completeness(candidate)
    lines = [
        f"📺 <b>{title}</b>",
        f"Сезон: {candidate.season_number}",
        _series_continue_plex_line(candidate),
        "",
        html_module.escape(completeness.reason_for_user),
    ]
    profile = _series_continue_profile_line(candidate)
    if profile:
        lines.extend(["", html_module.escape(profile)])
    if candidate.topic_id:
        lines.append(f"Тема: {html_module.escape(candidate.topic_id)}")
        lines.extend([
            "",
            "Если тема обновилась, можно скачать новые серии из той же раздачи.",
        ])
    else:
        lines.append("Прошлая раздача неизвестна.")
        lines.extend([
            "",
            "Для такого сезона нужен подбор похожей раздачи.",
        ])
    return "\n".join(lines)


def _series_continue_detail_keyboard(
    candidate: SeriesCatchUpCandidate,
    scope: str,
    index: int,
    page: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if candidate.topic_id:
        rows.append([
            InlineKeyboardButton(
                "⬇️ Докачать и следить",
                callback_data=f"{CONTINUE_CALLBACK_PREFIX}:update_topic:{scope}:{index}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:{scope}:{page}")])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _series_continue_action_keyboard(
    scope: str,
    index: int,
    page: int,
    *,
    retry: bool = False,
    subscribe: bool = False,
    search_alt: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if retry:
        rows.append([
            InlineKeyboardButton(
                "🔄 Повторить",
                callback_data=f"{CONTINUE_CALLBACK_PREFIX}:update_topic:{scope}:{index}",
            )
        ])
    if subscribe:
        rows.append([
            InlineKeyboardButton(
                "🔔 Следить за темой",
                callback_data=f"{CONTINUE_CALLBACK_PREFIX}:subscribe_topic:{scope}:{index}",
            )
        ])
    if search_alt:
        rows.append([
            InlineKeyboardButton(
                "🔍 Искать похожие",
                callback_data=f"{CONTINUE_CALLBACK_PREFIX}:search_alt:{scope}:{index}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:{scope}:{page}")])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _series_continue_result_from_topic(
    candidate: SeriesCatchUpCandidate,
    topic_title: str,
) -> dict:
    topic_id = str(candidate.topic_id or "")
    result = {
        "title": topic_title or _series_continue_candidate_title(candidate),
        "source": "rutracker",
        "tracker": "Rutracker",
        "tracker_name": "Rutracker",
        "category": "Rutracker",
        "topic_id": topic_id,
        "url": _rutracker_topic_url(topic_id) if topic_id else "",
        "quality": candidate.quality,
        "year": candidate.identity.year,
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _series_continue_result_from_rutracker(result: RutrackerResult) -> dict:
    episode_info = _parse_episode_info(result.title)
    partial = bool(episode_info and episode_info[0] < episode_info[1])
    return {
        "source": "rutracker",
        "topic_id": result.topic_id,
        "title": result.title,
        "url": _rutracker_topic_url(result.topic_id),
        "category": result.category,
        "size": result.size,
        "seeders": result.seeders,
        "partial": partial,
        "series": _extract_series_base_query(result.title) is not None,
        "ep_str": f"{episode_info[0]}/{episode_info[1]} эп." if episode_info else "",
        "magnet_url": None,
        "torrent_url": None,
        "tracker_name": "rutracker",
        "quality": _plex_quality_from_title(result.title),
    }


def _series_continue_search_query(candidate: SeriesCatchUpCandidate) -> str:
    title = (
        candidate.identity.original_title
        or candidate.identity.title
        or _series_continue_candidate_title(candidate)
    )
    quality_suffix = _quality_to_query_suffix(candidate.quality)
    return _normalize_season_in_query(f"{title} Сезон {candidate.season_number}{quality_suffix}")


def _series_continue_alt_key(scope: str, index: int) -> str:
    return f"{CONTINUE_STATE_KEY}:alt:{scope}:{index}"


def _series_continue_alternatives_text(results: list[dict], search_query: str) -> str:
    lines = [
        "🔍 <b>Похожие раздачи</b>",
        "",
        f"Запрос: {html_module.escape(search_query)}",
        "Это уже другая тема, поэтому бот скачает её как обновлённую раздачу.",
        "",
    ]
    for offset, result in enumerate(results, start=1):
        title = html_module.escape(str(result.get("title") or "Без названия"))
        lines.append(f"{offset}. <b>{title}</b>")
        details = [str(result.get("size") or ""), f"{result.get('seeders', 0)} сидов"]
        quality = result.get("quality")
        if quality:
            details.append(str(quality))
        lines.append("   " + html_module.escape(" · ".join(part for part in details if part)))
        lines.append("")
    return "\n".join(lines).strip()


def _series_continue_alternatives_keyboard(
    scope: str,
    candidate_index: int,
    page: int,
    results: list[dict],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for offset, _result in enumerate(results, start=1):
        rows.append([
            InlineKeyboardButton(
                f"⬇️ {offset}",
                callback_data=f"{CONTINUE_CALLBACK_PREFIX}:alt_dl:{scope}:{candidate_index}:{offset - 1}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{CONTINUE_CALLBACK_PREFIX}:list:{scope}:{page}")])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _series_continue_task_matches_candidate(task: dict, candidate: SeriesCatchUpCandidate) -> bool:
    if str(task.get("status") or "").lower() not in _ACTIVE_STATUSES:
        return False
    title = str(task.get("title") or "")
    if not title:
        return False
    season = _extract_season_from_query(title)
    if season != candidate.season_number:
        return False
    task_series = _extract_series_base_query(title) or title
    task_norm = _normalize_movie_title(task_series).lower()
    names = [
        candidate.identity.title,
        candidate.identity.original_title,
    ]
    for name in names:
        target = _normalize_movie_title(name or "").lower()
        if target and target in task_norm:
            return True
    return False


async def _series_continue_active_task(candidate: SeriesCatchUpCandidate) -> dict | None:
    if ds_client is None:
        return None
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except Exception:
        logger.debug("Series continue: Download Station task lookup failed", exc_info=True)
        return None
    for task in tasks:
        if _series_continue_task_matches_candidate(task, candidate):
            return task
    return None


async def _series_continue_add_download_result(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
    *,
    subscribe: bool,
    scope: str,
    index: int,
    page: int,
    success_subscription_text: str,
) -> None:
    chat_id = _chat_id_from_query(query)
    notify_policy = NOTIFY_EACH_UPDATE
    download_policy = DOWNLOAD_AUTO_EACH_UPDATE
    try:
        entry = _pending_download_entry_from_result(
            result,
            chat_id=chat_id,
            subscribe=subscribe,
            notify_policy=notify_policy,
            download_policy=download_policy,
            error="",
        )
        task_id, method = await _attempt_pending_download(entry)
        if task_id:
            _remember_task_owner(task_id, chat_id)
            _remember_task_meta(task_id, _build_task_meta_from_result(result, source="continue"))
        _record_download_added_history(
            task_id,
            chat_id,
            result,
            method=method,
            meta_source="continue",
            subscribe=subscribe,
            notify_policy=notify_policy if subscribe else None,
            download_policy=download_policy if subscribe else None,
        )

        subscription_saved = False
        if subscribe:
            try:
                _save_subscription_for_result(
                    context,
                    result,
                    chat_id=chat_id,
                    notify_policy=notify_policy,
                    download_policy=download_policy,
                    seen_results=[result],
                )
                subscription_saved = True
            except Exception:
                logger.warning("Series continue: failed to save subscription", exc_info=True)

        success_text = _task_added_message(method, title=str(result.get("title") or ""), task_id=task_id)
        if subscribe and subscription_saved:
            success_text += f"\n\n🔔 {success_subscription_text}"
        elif subscribe:
            success_text += "\n\n⚠️ Загрузка добавлена, но подписку не удалось сохранить."
        await query.edit_message_text(success_text, reply_markup=_task_reply_markup(task_id))
        _register_task_card_from_query(query, task_id)
        if task_id:
            card_chat_id = _chat_id_from_query(query)
            card_msg_id = _message_id_from_message(query.message) if query.message else None
            if card_chat_id and card_msg_id:
                _start_task_card_refresh(context.application, card_chat_id, card_msg_id, task_id)
    except (RutrackerError, JackettError, DownloadStationError, RuntimeError) as exc:
        _record_download_history(
            "download_failed",
            chat_id=chat_id,
            result=result,
            meta=_build_task_meta_from_result(result, source="continue"),
            error=_format_download_error(exc),
        )
        await query.edit_message_text(
            _download_failure_text(exc, can_queue=False),
            reply_markup=_series_continue_action_keyboard(scope, index, page, retry=True),
        )


async def _series_continue_subscribe_same_topic(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    candidate: SeriesCatchUpCandidate,
    *,
    scope: str,
    index: int,
    page: int,
) -> None:
    title = html_module.escape(_series_continue_candidate_title(candidate))
    if not candidate.topic_id or rutracker_client is None:
        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nНет сохранённой темы Rutracker для подписки.",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page),
        )
        return
    try:
        topic_title = await asyncio.to_thread(rutracker_client.get_topic_title, candidate.topic_id)
        result = _series_continue_result_from_topic(candidate, topic_title)
        _save_subscription_for_result(
            context,
            result,
            chat_id=_chat_id_from_query(query),
            notify_policy=NOTIFY_EACH_UPDATE,
            download_policy=DOWNLOAD_AUTO_EACH_UPDATE,
            seen_results=[result],
        )
        await query.edit_message_text(
            f"🔔 <b>Подписка сохранена</b>\n\nБуду следить за темой «{html_module.escape(topic_title)}».",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page),
        )
    except (RutrackerError, RuntimeError) as exc:
        await query.edit_message_text(
            _friendly_error("rutracker", str(exc), include_detail=False),
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page, retry=True),
        )


async def _series_continue_search_alternatives(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    candidate: SeriesCatchUpCandidate,
    *,
    scope: str,
    index: int,
    page: int,
) -> None:
    title = html_module.escape(_series_continue_candidate_title(candidate))
    if rutracker_client is None:
        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nRutracker не настроен, похожие раздачи искать негде.",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page),
        )
        return
    search_query = _series_continue_search_query(candidate)
    await query.edit_message_text(
        f"🔍 <b>Ищу похожие раздачи</b>\n\n{html_module.escape(search_query)}",
        parse_mode="HTML",
        reply_markup=_series_continue_action_keyboard(scope, index, page),
    )
    try:
        raw_results = await asyncio.to_thread(rutracker_client.search, search_query)
    except RutrackerError as exc:
        await query.edit_message_text(
            _friendly_error("rutracker", str(exc), include_detail=False),
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page, retry=True),
        )
        return

    season_results = [
        _series_continue_result_from_rutracker(result)
        for result in raw_results
        if result.topic_id != candidate.topic_id
        and _extract_season_from_query(result.title) == candidate.season_number
    ]
    if candidate.quality:
        quality_results = [
            result for result in season_results
            if str(result.get("quality") or "") == candidate.quality
        ]
    else:
        quality_results = season_results
    results = quality_results or season_results
    results.sort(key=_score_result, reverse=True)
    results = results[:5]
    if not results:
        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nПохожих раздач для этого сезона не нашёл.",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page, retry=True, subscribe=True),
        )
        return

    state = _series_continue_state(context)
    state[_series_continue_alt_key(scope, index)] = results
    await query.edit_message_text(
        _series_continue_alternatives_text(results, search_query),
        parse_mode="HTML",
        reply_markup=_series_continue_alternatives_keyboard(scope, index, page, results),
    )


async def _series_continue_download_alternative(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    candidate: SeriesCatchUpCandidate,
    *,
    scope: str,
    index: int,
    page: int,
    alt_index: int,
) -> None:
    state = _series_continue_state(context)
    alternatives = state.get(_series_continue_alt_key(scope, index))
    if not isinstance(alternatives, list) or alt_index < 0 or alt_index >= len(alternatives):
        await query.edit_message_text(
            "Список похожих раздач устарел. Запустите поиск похожих ещё раз.",
            reply_markup=_series_continue_action_keyboard(scope, index, page, search_alt=True),
        )
        return
    active_task = await _series_continue_active_task(candidate)
    if active_task:
        task_id = str(active_task.get("id") or "")
        await query.edit_message_text(
            "По этому сезону уже есть активная задача Download Station.",
            reply_markup=_task_reply_markup(task_id),
        )
        return
    result = alternatives[alt_index]
    episode_info = _parse_episode_info(str(result.get("title") or ""))
    subscribe = bool(episode_info and episode_info[0] < episode_info[1])
    await _series_continue_add_download_result(
        query,
        context,
        result,
        subscribe=subscribe,
        scope=scope,
        index=index,
        page=page,
        success_subscription_text="Буду следить за новыми сериями в этой раздаче.",
    )


async def _series_continue_download_same_topic(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    candidate: SeriesCatchUpCandidate,
    *,
    scope: str,
    index: int,
    page: int,
) -> None:
    title = html_module.escape(_series_continue_candidate_title(candidate))
    running_key = (
        f"{CONTINUE_STATE_KEY}:running:"
        f"{candidate.identity.plex_rating_key}:{candidate.season_number}:{candidate.topic_id}"
    )
    if context.user_data.get(running_key):
        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nУже выполняю докачивание этого сезона.",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page),
        )
        return
    context.user_data[running_key] = True
    if not candidate.topic_id or rutracker_client is None:
        context.user_data.pop(running_key, None)
        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nНет сохранённой темы Rutracker для докачивания.",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page),
        )
        return
    try:
        active_task = await _series_continue_active_task(candidate)
        if active_task:
            task_id = str(active_task.get("id") or "")
            await query.edit_message_text(
                f"📺 <b>{title}</b>\n\nПо этому сезону уже есть активная задача Download Station.",
                parse_mode="HTML",
                reply_markup=_task_reply_markup(task_id),
            )
            return

        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nПроверяю тему Rutracker…",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page),
        )

        topic_title = await asyncio.to_thread(rutracker_client.get_topic_title, candidate.topic_id)
        update_check = resolve_same_topic_update(candidate, topic_title)
        if update_check.action != "same_topic_update":
            await query.edit_message_text(
                f"📺 <b>{title}</b>\n\n{html_module.escape(update_check.reason_for_user)}",
                parse_mode="HTML",
                reply_markup=_series_continue_action_keyboard(
                    scope,
                    index,
                    page,
                    retry=True,
                    subscribe=True,
                    search_alt=True,
                ),
            )
            return

        result = _series_continue_result_from_topic(candidate, topic_title)
        subscribe = not (
            update_check.topic_total > 0
            and update_check.topic_episode_end >= update_check.topic_total
        )
        await _series_continue_add_download_result(
            query,
            context,
            result,
            subscribe=subscribe,
            scope=scope,
            index=index,
            page=page,
            success_subscription_text="Буду следить за новыми сериями в этой теме.",
        )
    except RutrackerTopicUnavailable as exc:
        logger.info("Series continue topic unavailable: %s", exc)
        await query.edit_message_text(
            f"📺 <b>{title}</b>\n\nТема Rutracker больше недоступна или удалена.",
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page, retry=True),
        )
    except RutrackerError as exc:
        await query.edit_message_text(
            _friendly_error("rutracker", str(exc), include_detail=False),
            parse_mode="HTML",
            reply_markup=_series_continue_action_keyboard(scope, index, page, retry=True),
        )
    finally:
        context.user_data.pop(running_key, None)


async def series_continue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /continue from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return
    if not PLEX_ENABLED:
        await update.message.reply_text(
            "📺 <b>Докачать сезон</b>\n\nPlex не настроен, поэтому список собрать нельзя.",
            parse_mode="HTML",
            reply_markup=_series_continue_close_keyboard(),
        )
        await _delete_command_message_safely(update, context, "continue command")
        return

    progress = await update.message.reply_text(
        _series_continue_progress_text(),
        parse_mode="HTML",
        reply_markup=_series_continue_close_keyboard(),
    )
    chat_id = update.effective_chat.id if update.effective_chat else None
    state = await _series_continue_build_state(context, chat_id)
    scope = "mine"
    page = 0
    state["scope"] = scope
    state["page"] = page
    await progress.edit_text(
        _series_continue_list_text(state, scope, page),
        parse_mode="HTML",
        reply_markup=_series_continue_list_keyboard(state, scope, page),
    )
    await _delete_command_message_safely(update, context, "continue command")


async def series_continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_allowed(update):
        logger.warning("Rejected continue callback from chat_id=%s", chat_id)
        await query.edit_message_text("Доступ не разрешён.")
        return

    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    state = _series_continue_state(context)

    if action == "refresh":
        scope = parts[2] if len(parts) > 2 and parts[2] in {"mine", "all"} else "mine"
        state = await _series_continue_build_state(context, chat_id)
        page = 0
        state["scope"] = scope
        state["page"] = page
        await query.edit_message_text(
            _series_continue_list_text(state, scope, page),
            parse_mode="HTML",
            reply_markup=_series_continue_list_keyboard(state, scope, page),
        )
        return

    if action == "list":
        scope = parts[2] if len(parts) > 2 and parts[2] in {"mine", "all"} else "mine"
        try:
            page = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            page = 0
        state["scope"] = scope
        state["page"] = page
        await query.edit_message_text(
            _series_continue_list_text(state, scope, page),
            parse_mode="HTML",
            reply_markup=_series_continue_list_keyboard(state, scope, page),
        )
        return

    if action == "open":
        scope = parts[2] if len(parts) > 2 and parts[2] in {"mine", "all"} else "mine"
        try:
            index = int(parts[3]) if len(parts) > 3 else -1
        except ValueError:
            index = -1
        candidates = _series_continue_candidates(state, scope)
        if index < 0 or index >= len(candidates):
            await query.edit_message_text(
                "Кандидат устарел. Обновите список.",
                reply_markup=_series_continue_close_keyboard(),
            )
            return
        page = max(0, index // CONTINUE_PAGE_SIZE)
        state["scope"] = scope
        state["page"] = page
        await query.edit_message_text(
            _series_continue_detail_text(candidates[index]),
            parse_mode="HTML",
            reply_markup=_series_continue_detail_keyboard(candidates[index], scope, index, page),
        )
        return

    if action == "update_topic":
        scope = parts[2] if len(parts) > 2 and parts[2] in {"mine", "all"} else "mine"
        try:
            index = int(parts[3]) if len(parts) > 3 else -1
        except ValueError:
            index = -1
        candidates = _series_continue_candidates(state, scope)
        if index < 0 or index >= len(candidates):
            await query.edit_message_text(
                "Кандидат устарел. Обновите список.",
                reply_markup=_series_continue_close_keyboard(),
            )
            return
        page = max(0, index // CONTINUE_PAGE_SIZE)
        state["scope"] = scope
        state["page"] = page
        await _series_continue_download_same_topic(
            query,
            context,
            candidates[index],
            scope=scope,
            index=index,
            page=page,
        )
        return

    if action in {"subscribe_topic", "search_alt", "alt_dl"}:
        scope = parts[2] if len(parts) > 2 and parts[2] in {"mine", "all"} else "mine"
        try:
            index = int(parts[3]) if len(parts) > 3 else -1
        except ValueError:
            index = -1
        candidates = _series_continue_candidates(state, scope)
        if index < 0 or index >= len(candidates):
            await query.edit_message_text(
                "Кандидат устарел. Обновите список.",
                reply_markup=_series_continue_close_keyboard(),
            )
            return
        page = max(0, index // CONTINUE_PAGE_SIZE)
        state["scope"] = scope
        state["page"] = page
        if action == "subscribe_topic":
            await _series_continue_subscribe_same_topic(
                query,
                context,
                candidates[index],
                scope=scope,
                index=index,
                page=page,
            )
            return
        if action == "search_alt":
            await _series_continue_search_alternatives(
                query,
                context,
                candidates[index],
                scope=scope,
                index=index,
                page=page,
            )
            return
        try:
            alt_index = int(parts[4]) if len(parts) > 4 else -1
        except ValueError:
            alt_index = -1
        await _series_continue_download_alternative(
            query,
            context,
            candidates[index],
            scope=scope,
            index=index,
            page=page,
            alt_index=alt_index,
        )
        return

    await query.edit_message_text(
        "Неизвестное действие.",
        reply_markup=_series_continue_close_keyboard(),
    )


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
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
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
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
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
        await query.edit_message_text(
            "Запрос потерян. Начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
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
    if query.message:
        context.user_data["srch_season_input_msg_id"] = query.message.message_id
        context.user_data["srch_season_input_chat_id"] = query.message.chat.id
    await query.edit_message_text(
        f"Введите номер сезона для «{base}»:" if base else "Введите номер сезона:",
        reply_markup=_season_input_keyboard(),
    )
    return SEARCH_SEASON_SELECT


async def _delete_season_input_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _edit_or_send_season_input_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    chat_id = context.user_data.get("srch_season_input_chat_id")
    message_id = context.user_data.get("srch_season_input_msg_id")
    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=_season_input_keyboard(),
            )
            return
        except Exception:
            pass

    if update.message:
        prompt = await update.message.reply_text(text, reply_markup=_season_input_keyboard())
        if prompt:
            context.user_data["srch_season_input_msg_id"] = prompt.message_id
            context.user_data["srch_season_input_chat_id"] = prompt.chat_id


async def search_season_got_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a season number after the manual-input prompt."""
    text = (update.message.text or "").strip()
    base = context.user_data.get("srch_base_title", "")

    if not base:
        await update.message.reply_text("Запрос потерян. Начните поиск заново.")
        return ConversationHandler.END

    if not text.isdigit() or int(text) <= 0:
        chat_id = update.effective_chat.id if update.effective_chat else None
        message_id = update.message.message_id if update.message else None
        await _delete_season_input_message(context, chat_id, message_id)
        await _edit_or_send_season_input_prompt(
            update,
            context,
            f"Введите положительный номер сезона цифрой для «{base}\":",
        )
        return SEARCH_SEASON_SELECT

    season_num = int(text)
    chat_id = update.effective_chat.id if update.effective_chat else None
    message_id = update.message.message_id if update.message else None
    await _delete_season_input_message(context, chat_id, message_id)
    prompt_chat_id = context.user_data.pop("srch_season_input_chat_id", None)
    prompt_msg_id = context.user_data.pop("srch_season_input_msg_id", None)
    await _delete_season_input_message(context, prompt_chat_id, prompt_msg_id)

    quality_suffix = _quality_to_query_suffix(context.user_data.get("srch_picked_quality", ""))
    search_query = _normalize_season_in_query(f"{base} Сезон {season_num}{quality_suffix}")

    if chat_id is None:
        return await _run_search(update.message.reply_text, context, search_query)

    async def send_to_chat(*args, **kwargs):
        return await context.bot.send_message(*args, chat_id=chat_id, **kwargs)

    return await _run_search(send_to_chat, context, search_query)


def _pending_downloads_enabled() -> bool:
    """Whether the pending-download queue feature is active (env-gated)."""
    return PENDING_DOWNLOADS_ENABLED


def _load_pending_downloads() -> dict[str, dict]:
    return state_store.load_pending_downloads()


def _save_pending_downloads(entries: dict[str, dict]) -> None:
    state_store.save_pending_downloads(entries)


def _pending_download_entry_from_result(
    result: dict, *, chat_id: int | None, subscribe: bool,
    notify_policy: str | None = None,
    download_policy: str | None = None,
    error: str,
    series_bulk: dict | None = None,
) -> dict:
    """Build a pending-queue entry from a search result + last error message."""
    entry = {
        "chat_id": chat_id,
        "added_at": datetime.now(DISPLAY_TIMEZONE).isoformat(),
        "title": str(result.get("title") or ""),
        "topic_url": str(result.get("url") or ""),
        "torrent_url": str(result.get("torrent_url") or ""),
        "magnet_url": result.get("magnet_url") or None,
        "tracker": str(result.get("tracker_name") or result.get("category") or ""),
        "source": str(result.get("source") or ""),
        "subscribe": bool(subscribe),
        "attempts": 0,
        "last_attempt_at": None,
        "last_error": (error or "")[:200],
    }
    if subscribe:
        notify_policy, download_policy = _coerce_subscription_policies(
            notify_policy, download_policy
        )
        # Preserve subscription policy so the background pending-retry loop
        # can restore the exact user intent when it eventually succeeds.
        entry["notify_policy"] = notify_policy
        entry["download_policy"] = download_policy
    if isinstance(series_bulk, dict) and series_bulk:
        entry["series_bulk"] = _series_bulk_jsonable(series_bulk)
    for key in ("topic_id", "movie_title", "year", "quality"):
        value = result.get(key)
        if value not in (None, ""):
            entry[key] = value
    return entry


def _queue_pending_download_from_result(
    result: dict,
    *,
    chat_id: int | None,
    subscribe: bool,
    error: str,
    notify_policy: str | None = None,
    download_policy: str | None = None,
    series_bulk: dict | None = None,
) -> tuple[str, dict]:
    pending = _load_pending_downloads()
    entry_id = uuid.uuid4().hex[:12]
    entry = _pending_download_entry_from_result(
        result,
        chat_id=chat_id,
        subscribe=subscribe,
        notify_policy=notify_policy,
        download_policy=download_policy,
        error=error,
        series_bulk=series_bulk,
    )
    pending[entry_id] = entry
    _save_pending_downloads(pending)
    logger.info(
        "Pending download queued: id=%s title=%s chat_id=%s",
        entry_id, entry["title"], chat_id,
    )
    return entry_id, entry


def _pending_entry_to_search_result(entry: dict) -> dict:
    """Inverse: reconstruct a search-result-shaped dict for _build_task_meta_from_result."""
    result = {
        "title": entry.get("title") or "",
        "url": entry.get("topic_url") or "",
        "torrent_url": entry.get("torrent_url") or "",
        "magnet_url": entry.get("magnet_url"),
        "tracker_name": entry.get("tracker") or "",
        "source": entry.get("source") or "",
    }
    for key in ("topic_id", "movie_title", "year", "quality"):
        value = entry.get(key)
        if value not in (None, ""):
            result[key] = value
    if not result.get("topic_id"):
        topic_id = _extract_rutracker_topic_id(str(result.get("url") or ""))
        if topic_id:
            result["topic_id"] = topic_id
    return result


def _is_pending_retryable_download_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    transient_markers = (
        "http 5",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "connection",
        "подключ",
        "недоступ",
        "временно",
        "отложен",
        "пауз",
        "не json",
    )
    if isinstance(exc, JackettError):
        return "404" in msg or any(marker in msg for marker in transient_markers)
    if isinstance(exc, RutrackerError):
        return any(marker in msg for marker in transient_markers)
    if isinstance(exc, DownloadStationError):
        return (
            "dsm api недоступен" in msg
            or "достигнут лимит задач" in msg
            or any(marker in msg for marker in (
                "timeout",
                "timed out",
                "connection",
                "urlerror",
                "не json",
            ))
        )
    return False


def _format_download_error(exc: Exception) -> str:
    """Human-readable short description of a torrent download failure.

    Keeps raw exception details out of chat UI; logs still receive the original
    exception at the call site.
    """
    msg = str(exc)
    lower = msg.lower()
    if isinstance(exc, JackettError):
        if "404" in msg or "not found" in lower:
            return "❌ Jackett не отдал torrent-файл. Раздача могла временно пропасть с трекера."
        if any(marker in lower for marker in ("http 5", "500", "502", "503", "504")):
            return "❌ Jackett или трекер временно недоступен."
        if "timeout" in lower or "timed out" in lower:
            return "❌ Превышено время ожидания от Jackett."
        if "api" in lower or "ключ" in lower:
            return "❌ Jackett не принял API-ключ. Проверьте настройки."
        return "❌ Jackett сейчас не смог отдать torrent-файл."
    if isinstance(exc, RutrackerError):
        if "captcha" in lower or "капч" in lower:
            return "❌ Rutracker просит капчу. После её прохождения попробуйте ещё раз."
        if "авториза" in lower or "username" in lower or "password" in lower:
            return "❌ Rutracker не принял логин или пароль. Проверьте настройки."
        if "недоступ" in lower or "удален" in lower or "удалена" in lower:
            return "❌ Раздача на Rutracker больше недоступна."
        if "timeout" in lower or "timed out" in lower:
            return "❌ Rutracker не ответил вовремя."
        return "❌ Rutracker сейчас не смог отдать torrent-файл."
    if isinstance(exc, DownloadStationError):
        if "достигнут лимит задач" in lower:
            return "❌ В Download Station достигнут лимит задач."
        if "auth" in lower or "авториза" in lower or "логин" in lower or "парол" in lower:
            return "❌ Download Station не принял логин или пароль. Проверьте настройки."
        if "недоступ" in lower or "timeout" in lower or "timed out" in lower or "connection" in lower:
            return "❌ Download Station сейчас недоступен."
        return "❌ Download Station не принял задачу."
    return "❌ Не удалось добавить загрузку."


def _download_failure_text(exc: Exception, *, can_queue: bool) -> str:
    lines = [
        "⚠️ Не удалось добавить загрузку",
        "",
        "Что произошло:",
        "не получилось передать выбранную раздачу в очередь скачивания.",
        "",
    ]
    if can_queue:
        lines.extend([
            "Что можно сделать:",
            "попробовать снова сейчас или поставить в очередь, чтобы бот повторил попытку позже.",
        ])
    else:
        lines.extend([
            "Что можно сделать:",
            "попробовать снова сейчас. Если ошибка повторится, проверьте доступность сервиса загрузок.",
        ])
    return "\n".join(lines)


# Disk-space guard thresholds. <5% free → BLOCK download (DSM would likely
# fail anyway, but better to fail-fast with a clear message). <15% free →
# warn in logs and surface in /admin diagnostics, but don't block (user
# may consciously want this last 30 GB rip).
_DISK_SPACE_BLOCK_PCT = 5.0
_DISK_SPACE_WARN_PCT = 15.0


def _check_disk_space_for_download() -> tuple[str, str] | None:
    """Return (severity, message) if disk-space concern, else None.

    severity: "block" → caller MUST abort download with this message.
              "warn"  → caller logs + can optionally show in UI.

    Uses the unified disk-info helper (mount-first, DSM-fallback). None
    means either disk space is fine, OR neither source could answer
    (treat as fine — graceful degrade, never block on missing data).
    """
    try:
        info = get_unified_disk_info(ds_client)
    except Exception:  # noqa: BLE001 — disk check must never crash download flow
        logger.warning("Disk-space check raised unexpectedly", exc_info=True)
        return None
    if info is None or info.total_bytes <= 0:
        return None

    free = info.free_bytes
    total = info.total_bytes
    free_pct = 100.0 * free / total
    if free_pct < _DISK_SPACE_BLOCK_PCT:
        msg = (
            f"🚨 Недостаточно места на NAS\n\n"
            f"Свободно: <b>{_format_size(free)}</b> из {_format_size(total)} "
            f"(<b>{free_pct:.1f}%</b>).\n"
            f"Порог блокировки: {_DISK_SPACE_BLOCK_PCT:.0f}%.\n\n"
            f"Освободите место и попробуйте снова."
        )
        return ("block", msg)
    if free_pct < _DISK_SPACE_WARN_PCT:
        return ("warn",
                f"⚠️ На NAS осталось {_format_size(free)} ({free_pct:.1f}%) — "
                f"download продолжается, но место заканчивается.")
    return None


async def _download_and_add(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    index: int,
    *,
    subscribe: bool = False,
    notify_policy: str | None = None,
    download_policy: str | None = None,
    _skip_plex_check: bool = False,
    _movie_handled_cards: list[dict] | None = None,
) -> int:
    """Shared implementation for direct-download and direct-subscribe from the results list.

    Downloads the torrent at *index*, adds it to Download Station, optionally
    creates a subscription, then shows a success (or error) message.
    Returns the next ConversationHandler state.
    """
    results = context.user_data.get("srch_results", [])
    if index < 0 or index >= len(results):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    notify_policy, download_policy = _coerce_subscription_policies(
        notify_policy, download_policy
    )
    result = results[index]
    context.user_data["srch_picked"] = index
    # Stash subscribe/policy intent so retry/queue handlers
    # can restore them after a download failure. Without this, tapping retry
    # silently downgrades «⬇️📺 Серии» to a plain one-shot download.
    context.user_data["srch_last_subscribe"] = subscribe
    context.user_data["srch_last_notify_policy"] = notify_policy
    context.user_data["srch_last_download_policy"] = download_policy
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
                "notify_policy": notify_policy,
                "download_policy": download_policy,
                "movie_handled_cards": _movie_handled_cards,
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
                    "notify_policy": notify_policy,
                    "download_policy": download_policy,
                    "movie_handled_cards": _movie_handled_cards,
                    # R.2: stash the existing season's rating_key so the
                    # «🔼 Заменить» button can mark it for future removal
                    # after Plex indexes the new download.
                    "plex_old_season_key": series_check.season.rating_key,
                    "plex_action": series_check.action,
                }
                await query.edit_message_text(
                    _plex_series_confirm_text(series_check, display_title, req_quality),
                    reply_markup=_plex_confirm_keyboard(
                        show_upgrade=(series_check.action == "offer_upgrade")
                    ),
                    parse_mode="HTML",
                )
                return SEARCH_PLEX_CONFIRM

    # Disk-space guard — runs after Plex check (Plex confirm has its own
    # path that re-enters this function with _skip_plex_check=True, so the
    # check still fires before any actual DS task creation).
    disk_check = await asyncio.to_thread(_check_disk_space_for_download)
    if disk_check is not None:
        severity, msg = disk_check
        if severity == "block":
            logger.warning("Download blocked: disk space critical (%s)", msg)
            await query.edit_message_text(
                msg,
                reply_markup=_search_error_keyboard(),
                parse_mode="HTML",
            )
            return ConversationHandler.END
        # severity == "warn" — keep going, but surface to the user later
        # in the success message. Stash for renderer to pick up.
        context.user_data["srch_disk_warn"] = msg
        logger.info("Disk-space warning before download: %s", msg)

    await query.edit_message_text(
        "⏳ Добавляю загрузку\n\n"
        "Сейчас получаю torrent-файл и передаю задачу в очередь скачивания."
    )

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
                # Step 0 (new): if this is a Rutracker result and we have a direct
                # rutracker_client, try fetching the .torrent through it. Jackett's
                # proxy is the most failure-prone link (session refresh, 404 from
                # /dl/<indexer>/?path=...); rutracker_client uses its own session
                # and may succeed. Magnet fallback below physically can't work for
                # Rutracker (private tracker, magnet needs a passkey-bearing
                # announce URL that's not in the .torrent metadata).
                tracker_name = (result.get("tracker_name") or result.get("category") or "").lower()
                topic_id_from_url = _extract_rutracker_topic_id(result.get("url") or "")
                direct_rt_ok = False
                if rutracker_client and "rutracker" in tracker_name and topic_id_from_url:
                    try:
                        logger.info(
                            "Jackett download failed (%s), trying rutracker_client direct: topic_id=%s",
                            torrent_err, topic_id_from_url,
                        )
                        await query.edit_message_text(
                            "⏳ Пробую запасной путь\n\n"
                            "Jackett не отдал torrent-файл. Получаю раздачу напрямую с Rutracker."
                        )
                        torrent_bytes = await asyncio.to_thread(
                            rutracker_client.download_torrent, topic_id_from_url
                        )
                        temp_path.write_bytes(torrent_bytes)
                        task_id = await asyncio.to_thread(
                            ds_client.create_torrent_file, temp_path, safe_name
                        )
                        direct_rt_ok = True
                    except RutrackerError as rt_err:
                        logger.info("rutracker_client direct also failed: %s — falling back", rt_err)
                        # fall through to existing re-search / magnet chain

                if direct_rt_ok:
                    pass  # task_id is set; skip the re-search/magnet block
                else:
                    logger.warning("torrent_url download failed (%s), refreshing via re-search", torrent_err)
                    await query.edit_message_text(
                        "⏳ Повторяю попытку\n\n"
                        "Обновляю данные раздачи и пробую передать её в очередь скачивания."
                    )
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
                        except JackettMagnetRedirect as retry_redir:
                            magnet = retry_redir.magnet_url or result.get("magnet_url", "")
                            if not magnet:
                                raise JackettError(
                                    "Torrent-файл недоступен и magnet-ссылка отсутствует."
                                ) from retry_redir
                            task_id = await asyncio.to_thread(
                                ds_client.create_magnet, magnet
                            )
                            if not task_id:
                                task_id = await _wait_for_magnet_task_id(
                                    magnet, known_task_ids, query.message
                                )
                            download_method = "magnet"
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

        if not task_id and download_method != "magnet":
            raise _missing_task_id_error(f"для {download_method}")

        if task_id:
            _remember_task_owner(task_id, chat_id)
            _remember_task_meta(task_id, _build_task_meta_from_result(result, source="search"))
        _record_download_added_history(
            task_id,
            chat_id,
            result,
            method=download_method,
            meta_source="search",
            subscribe=subscribe,
            notify_policy=notify_policy if subscribe else None,
            download_policy=download_policy if subscribe else None,
        )
        if chat_id and _movie_handled_cards:
            _mark_user_handled_in_new(chat_id, _movie_handled_cards)

        if task_id and temp_path.exists():
            if _torrent_file_is_private(temp_path):
                tracker_result = TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")
                _mark_tracker_processed_if_final(task_id, tracker_result)
            else:
                await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
                tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
                _mark_tracker_processed_if_final(task_id, tracker_result)
        elif task_id:
            # magnet path — no torrent file to check
            await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
            tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
            _mark_tracker_processed_if_final(task_id, tracker_result)
        else:
            tracker_result = TrackerApplyResult(skipped_reason="ID задачи пока не найден, трекеры не добавляю")

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
                    notify_policy=notify_policy,
                    download_policy=download_policy,
                )
                state_store.save_topic_subscriptions(subs)
                logger.info(
                    "Jackett subscription added: key=%s query=%s policy=%s/%s",
                    sub_key, subs[sub_key]["query"], notify_policy, download_policy,
                )
            else:
                # Rutracker topic subscription (existing logic)
                episode_info = _parse_episode_info(title)
                if episode_info and chat_id:
                    subs = state_store.load_topic_subscriptions()
                    new_sub = {
                        "chat_id": chat_id,
                        "title": title,
                        "last_episode_end": episode_info[0],
                        "total_episodes": episode_info[1],
                        "added_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                        "notify_policy": notify_policy,
                        "download_policy": download_policy,
                    }
                    subs[topic_id] = new_sub
                    state_store.save_topic_subscriptions(subs)
                    logger.info(
                        "Subscription added: topic=%s chat=%s episodes=%s/%s policy=%s/%s",
                        topic_id, chat_id, episode_info[0], episode_info[1],
                        notify_policy, download_policy,
                    )

        added_msg = _task_added_message(
            download_method,
            title=title,
            task_id=task_id,
            tracker_result=tracker_result,
            accepted_without_task_id=(download_method == "magnet" and not task_id),
        )
        if download_method == "magnet" and not task_id:
            added_msg += f"\n\n{_magnet_without_task_id_note()}"
        suffix = "\n\n🔔 Буду следить за новыми сериями." if subscribe else ""
        success_text = f"{added_msg}{suffix}"

        series_query = _extract_series_base_query(title)
        _card_chat_id = _chat_id_from_query(query)
        _card_msg_id = _message_id_from_message(query.message) if query.message else None
        if series_query and task_id:
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

        await query.edit_message_text(
            success_text,
            reply_markup=_task_reply_markup(task_id),
        )
        _register_task_card_from_query(query, task_id)
        if task_id and _card_chat_id and _card_msg_id:
            _start_task_card_refresh(context.application, _card_chat_id, _card_msg_id, task_id)
    except (RutrackerError, JackettError, DownloadStationError) as e:
        logger.warning("Download failed for index=%s: %s", index, e, exc_info=True)
        _record_download_history(
            "download_failed",
            chat_id=chat_id,
            result=result,
            meta=_build_task_meta_from_result(result, source="search") if isinstance(result, dict) else None,
            error=_format_download_error(e),
        )
        can_queue = _pending_downloads_enabled()
        error_text = _download_failure_text(e, can_queue=can_queue)
        # Remember the error so the pending-queue handler (if user clicks
        # «⏳ Поставить в очередь») can record it on the queued entry.
        context.user_data["srch_last_dl_error"] = _format_download_error(e)
        await query.edit_message_text(
            error_text,
            reply_markup=_download_error_keyboard(
                index=index,
                can_queue=can_queue,
                can_retry=True,
            ),
        )
        # Return SEARCH_RESULTS (not END) so the Retry/Queue callbacks
        # dispatch within the active ConversationHandler.
        return SEARCH_RESULTS
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
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    # User committed to a result — any in-flight did-you-mean prefetch is now
    # irrelevant. Cancel to free the asyncio.Task.
    _cancel_didmean_prefetch(context)
    return await _download_and_add(query, context, index, subscribe=False)


# ─── Partial-series action pickers ─────────────────────────────────────────
#
# Partial results split the user's intent into two clear branches:
#   srch:dl_pick:N            → download branch for result N
#   srch:sub_pick:N           → notification-only branch for result N
#   srch:sub_preset:N:CODE    → commit one of the branch options
#   srch:sub_advanced:N       → legacy/manual 2-step menu (notify → download)
#   srch:sub_set_notify:N:V   → step 1 of manual flow
#   srch:sub_set_download:N:V → step 2 of manual flow
#   srch:sub_back_results:0   → back to results from the picker (rerender)
from subscription_policy import (
    NOTIFY_EACH_UPDATE, NOTIFY_FINAL_ONLY, NOTIFY_SILENT,
    VALID_DOWNLOAD_POLICIES, VALID_NOTIFY_POLICIES,
    DOWNLOAD_AUTO_EACH_UPDATE, DOWNLOAD_ONLY_WHEN_COMPLETE,
    DOWNLOAD_NOTIFY_ONLY, DOWNLOAD_ASK,
    download_policy_label_ru, notify_policy_label_ru,
    policies_summary_ru,
)

_SUB_PRESETS = {
    # code → (notify_policy, download_policy, download_current_now)
    "each":   (NOTIFY_EACH_UPDATE, DOWNLOAD_AUTO_EACH_UPDATE, True),
    "after":  (NOTIFY_FINAL_ONLY,  DOWNLOAD_ONLY_WHEN_COMPLETE, False),
    "notify": (NOTIFY_EACH_UPDATE, DOWNLOAD_NOTIFY_ONLY, False),
    "final":  (NOTIFY_FINAL_ONLY,  DOWNLOAD_NOTIFY_ONLY, False),
}


def _coerce_subscription_policies(
    notify_policy: str | None,
    download_policy: str | None,
) -> tuple[str, str]:
    notify = notify_policy if notify_policy in VALID_NOTIFY_POLICIES else NOTIFY_EACH_UPDATE
    download = (
        download_policy
        if download_policy in VALID_DOWNLOAD_POLICIES
        else DOWNLOAD_AUTO_EACH_UPDATE
    )
    return notify, download


def _subscription_policy_pair_does_nothing(notify_policy: str, download_policy: str) -> bool:
    return notify_policy == NOTIFY_SILENT and download_policy == DOWNLOAD_NOTIFY_ONLY


def _subscription_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
    ]])


def _save_subscription_for_result(
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
    *,
    chat_id: int | None,
    notify_policy: str,
    download_policy: str,
    seen_results: list[dict] | None = None,
) -> tuple[str, dict]:
    title = str(result.get("title") or "")
    notify_policy, download_policy = _coerce_subscription_policies(
        notify_policy, download_policy
    )
    source = str(result.get("source") or "rutracker")
    now_text = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    subs = state_store.load_topic_subscriptions()

    if source == "jackett":
        sub_key = f"jackett:{uuid.uuid4().hex[:8]}"
        search_query = str(
            context.user_data.get("srch_search_query")
            or context.user_data.get("srch_query")
            or title
        )
        subs[sub_key] = build_jackett_subscription(
            chat_id=chat_id,
            query=search_query,
            result=result,
            seen_results=seen_results if seen_results is not None else context.user_data.get("srch_results", []),
            added_at=now_text,
            notify_policy=notify_policy,
            download_policy=download_policy,
        )
        saved_key = sub_key
    else:
        topic_id = str(result.get("topic_id") or "")
        if not topic_id:
            topic_id = _extract_rutracker_topic_id(str(result.get("url") or ""))
        episode_info = _parse_episode_info(title)
        if not topic_id or not episode_info or chat_id is None:
            raise RuntimeError("Не удалось создать подписку для этого результата.")
        subs[topic_id] = {
            "chat_id": chat_id,
            "title": title,
            "last_episode_end": episode_info[0],
            "total_episodes": episode_info[1],
            "added_at": now_text,
            "notify_policy": notify_policy,
            "download_policy": download_policy,
        }
        saved_key = topic_id

    state_store.save_topic_subscriptions(subs)
    return saved_key, subs[saved_key]


async def _create_subscription_only(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    index: int,
    *,
    notify_policy: str,
    download_policy: str,
) -> int:
    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    result = results[index]
    title = str(result.get("title") or "")
    chat_id = _chat_id_from_query(query)
    if not isinstance(chat_id, int):
        chat_id = None
    try:
        saved_key, saved_sub = _save_subscription_for_result(
            context,
            result,
            chat_id=chat_id,
            notify_policy=notify_policy,
            download_policy=download_policy,
        )
    except RuntimeError as exc:
        logger.info("Subscription save failed: %s", exc)
        await query.edit_message_text(
            f"⚠️ {_subscription_save_user_error_text(downloaded=False)}.",
            reply_markup=_subscription_done_keyboard(),
        )
        return ConversationHandler.END

    logger.info(
        "Subscription added without initial download: key=%s policy=%s/%s",
        saved_key, notify_policy, download_policy,
    )
    title_html = html_module.escape(title[:160] or "раздача")
    policy_html = html_module.escape(policies_summary_ru(saved_sub))
    await query.edit_message_text(
        "✅ Настроил обновления.\n\n"
        f"🎬 <b>{title_html}</b>\n"
        f"Режим: {policy_html}\n\n"
        "Текущую неполную раздачу не скачиваю.",
        reply_markup=_subscription_done_keyboard(),
        parse_mode="HTML",
    )
    return ConversationHandler.END


def _admin_subscription_toggle_label(sub: dict) -> str:
    notify_policy = sub.get("notify_policy")
    if notify_policy not in VALID_NOTIFY_POLICIES:
        notify_policy = NOTIFY_EACH_UPDATE
    if notify_policy == NOTIFY_FINAL_ONLY:
        return "🎯→📺"
    if notify_policy == NOTIFY_SILENT:
        return "🔇→📺"
    return "📺→🎯"


def _toggle_subscription_notify_policy(sub: dict) -> tuple[str, str]:
    """Toggle only the notification axis, preserving the download policy."""
    current = str(sub.get("notify_policy") or NOTIFY_EACH_UPDATE)
    if current == NOTIFY_FINAL_ONLY:
        new_policy = NOTIFY_EACH_UPDATE
    elif current == NOTIFY_SILENT:
        new_policy = NOTIFY_EACH_UPDATE
    else:
        new_policy = NOTIFY_FINAL_ONLY

    sub["notify_policy"] = new_policy
    if sub.get("download_policy") not in VALID_DOWNLOAD_POLICIES:
        sub["download_policy"] = DOWNLOAD_AUTO_EACH_UPDATE
    return current, new_policy


def _download_picker_text(result: dict) -> str:
    title = html_module.escape(str(result.get("title") or "")[:120])
    ep_str = html_module.escape(str(result.get("ep_str") or ""))
    progress = f"\n\nСейчас доступно: <b>{ep_str}</b>" if ep_str else ""
    return f"🎬 {title}{progress}\n\nЧто скачать?"


def _download_picker_keyboard(
    index: int,
    *,
    partial: bool = True,
    show_bulk_plan: bool = False,
) -> InlineKeyboardMarkup:
    prefix = SEARCH_CALLBACK_PREFIX
    rows: list[list[InlineKeyboardButton]] = []
    if partial:
        rows.extend([
            [InlineKeyboardButton("⬇️ Скачать сейчас + новые серии по мере выхода",
                                  callback_data=f"{prefix}:sub_preset:{index}:each")],
            [InlineKeyboardButton("⬇️ Скачать только доступные",
                                  callback_data=f"{prefix}:dl:{index}")],
            [InlineKeyboardButton("📦 Скачать, когда сезон завершится",
                                  callback_data=f"{prefix}:sub_preset:{index}:after")],
        ])
    else:
        rows.append([InlineKeyboardButton("⬇️ Скачать сейчас",
                                          callback_data=f"{prefix}:dl:{index}")])
    if show_bulk_plan:
        rows.append([InlineKeyboardButton("📚 Скачать недостающие сезоны",
                                          callback_data=f"{prefix}:bulk_plan:{index}")])
    rows.extend([
        [InlineKeyboardButton("⬅️ К результатам",
                              callback_data=f"{prefix}:sub_back_results:0")],
        [InlineKeyboardButton("❌ Отмена",
                              callback_data=f"{prefix}:cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _series_bulk_wait_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel"),
    ]])


def _series_bulk_start_build(context: ContextTypes.DEFAULT_TYPE) -> str:
    token = uuid.uuid4().hex
    context.user_data["srch_series_bulk_build_token"] = token
    context.user_data.pop("srch_series_bulk_cancelled_token", None)
    return token


def _series_bulk_mark_build_cancelled(context: ContextTypes.DEFAULT_TYPE) -> None:
    token = context.user_data.get("srch_series_bulk_build_token")
    if token:
        context.user_data["srch_series_bulk_cancelled_token"] = token


def _series_bulk_build_cancelled(
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
) -> bool:
    return bool(token and context.user_data.get("srch_series_bulk_cancelled_token") == token)


def _series_bulk_finish_build(context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    if context.user_data.get("srch_series_bulk_build_token") == token:
        context.user_data.pop("srch_series_bulk_build_token", None)
    if context.user_data.get("srch_series_bulk_cancelled_token") == token:
        context.user_data.pop("srch_series_bulk_cancelled_token", None)


def _series_bulk_start_action(context: ContextTypes.DEFAULT_TYPE, action: str) -> bool:
    if context.user_data.get("srch_series_bulk_action_running"):
        return False
    context.user_data["srch_series_bulk_action_running"] = action
    return True


def _series_bulk_finish_action(context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if context.user_data.get("srch_series_bulk_action_running") == action:
        context.user_data.pop("srch_series_bulk_action_running", None)


def _series_bulk_resolved(context: ContextTypes.DEFAULT_TYPE) -> dict[int, str]:
    raw = context.user_data.setdefault("srch_series_bulk_resolved", {})
    if not isinstance(raw, dict):
        raw = {}
        context.user_data["srch_series_bulk_resolved"] = raw
    resolved: dict[int, str] = {}
    for key, value in raw.items():
        try:
            resolved[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return resolved


def _series_bulk_mark_resolved(
    context: ContextTypes.DEFAULT_TYPE,
    season: int,
    summary: str,
) -> None:
    raw = context.user_data.setdefault("srch_series_bulk_resolved", {})
    if not isinstance(raw, dict):
        raw = {}
        context.user_data["srch_series_bulk_resolved"] = raw
    raw[str(season)] = summary


def _series_bulk_failed(context: ContextTypes.DEFAULT_TYPE) -> dict[int, str]:
    raw = context.user_data.setdefault("srch_series_bulk_failed", {})
    if not isinstance(raw, dict):
        raw = {}
        context.user_data["srch_series_bulk_failed"] = raw
    failed: dict[int, str] = {}
    for key, value in raw.items():
        try:
            failed[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return failed


def _series_bulk_mark_failed(
    context: ContextTypes.DEFAULT_TYPE,
    season: int,
    error: str,
) -> None:
    raw = context.user_data.setdefault("srch_series_bulk_failed", {})
    if not isinstance(raw, dict):
        raw = {}
        context.user_data["srch_series_bulk_failed"] = raw
    raw[str(season)] = error


def _series_bulk_clear_failed(
    context: ContextTypes.DEFAULT_TYPE,
    season: int,
) -> None:
    raw = context.user_data.setdefault("srch_series_bulk_failed", {})
    if isinstance(raw, dict):
        raw.pop(str(season), None)
    index_raw = context.user_data.setdefault("srch_series_bulk_failed_candidates", {})
    if isinstance(index_raw, dict):
        index_raw.pop(str(season), None)


def _series_bulk_mark_failed_candidate(
    context: ContextTypes.DEFAULT_TYPE,
    season: int,
    candidate_index: int,
) -> None:
    raw = context.user_data.setdefault("srch_series_bulk_failed_candidates", {})
    if not isinstance(raw, dict):
        raw = {}
        context.user_data["srch_series_bulk_failed_candidates"] = raw
    raw[str(season)] = int(candidate_index)


def _series_bulk_failed_candidate_index(
    context: ContextTypes.DEFAULT_TYPE,
    season: int,
) -> int | None:
    raw = context.user_data.setdefault("srch_series_bulk_failed_candidates", {})
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw.get(str(season)))
    except (TypeError, ValueError):
        return None


def _series_bulk_candidate_index(season_plan: SeasonPlan, candidate) -> int:
    target_key = _series_bulk_result_key(candidate.result)
    for index, item in enumerate(season_plan.candidates):
        if _series_bulk_result_key(item.result) == target_key:
            return index
    return 0


def _series_bulk_failed_candidate(
    context: ContextTypes.DEFAULT_TYPE,
    season_plan: SeasonPlan,
):
    failed_index = _series_bulk_failed_candidate_index(context, season_plan.season)
    if failed_index is not None and 0 <= failed_index < len(season_plan.candidates):
        return season_plan.candidates[failed_index], failed_index
    if season_plan.selected is not None:
        return season_plan.selected, _series_bulk_candidate_index(season_plan, season_plan.selected)
    if season_plan.candidates:
        return season_plan.candidates[0], 0
    return None, None


def _series_bulk_job_now() -> str:
    return datetime.now(DISPLAY_TIMEZONE).isoformat(timespec="seconds")


def _series_bulk_jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _series_bulk_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_series_bulk_jsonable(v) for v in value]
    return str(value)


def _series_bulk_result_snapshot(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    return _series_bulk_jsonable(result)


def _series_bulk_profile_snapshot(profile: SeriesBulkProfile | None) -> dict:
    if profile is None:
        return {}
    return {
        "quality": profile.quality,
        "require_original": profile.require_original,
        "require_subs": profile.require_subs,
        "voice_policy": profile.voice_policy,
        "voices": list(profile.voices),
        "preferred_voices": list(profile.preferred_voices),
        "release_type": profile.release_type,
        "release_group": profile.release_group,
        "tracker": profile.tracker,
        "source": profile.source,
    }


def _series_bulk_release_snapshot(release) -> dict:
    return {
        "quality": release.quality,
        "release_type": release.release_type,
        "voices": list(release.voices),
        "has_original": release.has_original,
        "has_subs": release.has_subs,
        "has_russian_audio": release.has_russian_audio,
        "release_group": release.release_group,
        "size_gb": release.size_gb,
    }


def _series_bulk_candidate_snapshot(candidate) -> dict:
    return {
        "season": candidate.season,
        "score": candidate.score,
        "confidence": candidate.confidence,
        "reasons": list(candidate.reasons),
        "warnings": list(candidate.warnings),
        "hard_failures": list(candidate.hard_failures),
        "episode_progress": list(candidate.episode_progress) if candidate.episode_progress else None,
        "gpt_hint": str(getattr(candidate, "gpt_hint", "") or ""),
        "release": _series_bulk_release_snapshot(candidate.release),
        "result": _series_bulk_result_snapshot(candidate.result),
    }


def _series_bulk_initial_runtime_status(plan_status: str) -> str:
    if plan_status in {
        STATUS_ALREADY_IN_PLEX,
        STATUS_ALREADY_DOWNLOADING,
        STATUS_MISSING,
    }:
        return plan_status
    return "pending"


def _series_bulk_season_job_entry(season_plan: SeasonPlan) -> dict:
    return {
        "season": season_plan.season,
        "plan_status": season_plan.status,
        "runtime_status": _series_bulk_initial_runtime_status(season_plan.status),
        "reasons": list(season_plan.reasons),
        "selected": (
            _series_bulk_candidate_snapshot(season_plan.selected)
            if season_plan.selected is not None else None
        ),
        "candidates": [
            _series_bulk_candidate_snapshot(candidate)
            for candidate in season_plan.candidates[:5]
        ],
    }


def _series_bulk_create_job(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    plan,
    profile: SeriesBulkProfile,
    results: list[dict],
    warnings: tuple[str, ...],
    source_result: dict,
    chat_id: int | None,
) -> str:
    now = _series_bulk_job_now()
    job_id = f"bulk_{datetime.now(DISPLAY_TIMEZONE).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    job = {
        "id": job_id,
        "chat_id": chat_id if isinstance(chat_id, int) else None,
        "series_title": str(getattr(plan, "series_title", "")),
        "created_at": now,
        "updated_at": now,
        "status": "planned",
        "profile": _series_bulk_profile_snapshot(profile),
        "result_count": len(results),
        "warnings": list(dict.fromkeys(warnings)),
        "verified_season_range": bool(getattr(plan, "verified_season_range", False)),
        "source_result": _series_bulk_result_snapshot(source_result),
        "seasons": {
            str(season.season): _series_bulk_season_job_entry(season)
            for season in getattr(plan, "seasons", ())
        },
        "pack_candidates": [
            _series_bulk_result_snapshot(result)
            for result in getattr(plan, "pack_candidates", ())[:5]
        ],
    }
    try:
        jobs = state_store.load_series_bulk_jobs()
        jobs[job_id] = job
        state_store.save_series_bulk_jobs(jobs)
    except Exception:
        logger.warning("Series bulk job save failed: id=%s", job_id, exc_info=True)
        return ""
    context.user_data["srch_series_bulk_job_id"] = job_id
    return job_id


def _series_bulk_update_job(
    context: ContextTypes.DEFAULT_TYPE,
    updater,
) -> None:
    job_id = str(context.user_data.get("srch_series_bulk_job_id") or "")
    if not job_id:
        return
    try:
        jobs = state_store.load_series_bulk_jobs()
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            return
        updater(job)
        job["updated_at"] = _series_bulk_job_now()
        jobs[job_id] = job
        state_store.save_series_bulk_jobs(jobs)
    except Exception:
        logger.warning("Series bulk job update failed: id=%s", job_id, exc_info=True)


def _series_bulk_set_job_status(
    context: ContextTypes.DEFAULT_TYPE,
    status: str,
) -> None:
    def _set_status(job: dict) -> None:
        job["status"] = status

    _series_bulk_update_job(context, _set_status)


def _series_bulk_record_job_season(
    context: ContextTypes.DEFAULT_TYPE,
    season: int,
    runtime_status: str,
    *,
    task_id: str | None = None,
    method: str | None = None,
    error: str | None = None,
    summary: str | None = None,
    result: dict | None = None,
    subscription: dict | None = None,
    pending_entry_id: str | None = None,
) -> None:
    def _record(job: dict) -> None:
        seasons = job.setdefault("seasons", {})
        entry = seasons.setdefault(str(season), {"season": season})
        entry["runtime_status"] = runtime_status
        if task_id is not None:
            entry["task_id"] = str(task_id)
        if method is not None:
            entry["method"] = str(method)
        if error is not None:
            entry["error"] = str(error)
        elif runtime_status not in {"failed", "partial_downloaded_subscription_failed"}:
            entry.pop("error", None)
        if summary is not None:
            entry["summary"] = str(summary)
        if result is not None:
            entry["result"] = _series_bulk_result_snapshot(result)
        if subscription is not None:
            entry["subscription"] = _series_bulk_jsonable(subscription)
        if pending_entry_id is not None:
            entry["pending_entry_id"] = str(pending_entry_id)

    _series_bulk_update_job(context, _record)


def _series_bulk_record_job_pack(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    result: dict,
    task_id: str | None,
    method: str | None,
    season_range: tuple[int, int] | None,
) -> None:
    def _record(job: dict) -> None:
        packs = job.setdefault("pack_downloads", [])
        if not isinstance(packs, list):
            packs = []
            job["pack_downloads"] = packs
        packs.append({
            "added_at": _series_bulk_job_now(),
            "task_id": str(task_id or ""),
            "method": str(method or ""),
            "season_range": list(season_range) if season_range else None,
            "result": _series_bulk_result_snapshot(result),
        })

    _series_bulk_update_job(context, _record)


def _series_bulk_ready_seasons(
    plan,
    resolved: dict[int, str] | None = None,
    failed: dict[int, str] | None = None,
) -> list[SeasonPlan]:
    resolved = resolved or {}
    failed = failed or {}
    return [
        season
        for season in getattr(plan, "seasons", ())
        if season.status in {STATUS_EXACT, STATUS_GOOD} and season.selected is not None
        and season.season not in resolved
        and season.season not in failed
    ]


def _series_bulk_decision_seasons(
    plan,
    resolved: dict[int, str] | None = None,
    failed: dict[int, str] | None = None,
) -> list[SeasonPlan]:
    resolved = resolved or {}
    failed = failed or {}
    seasons: list[SeasonPlan] = []
    for season in getattr(plan, "seasons", ()):
        if season.season in resolved:
            continue
        if season.season in failed:
            seasons.append(season)
            continue
        if season.status in {STATUS_MISSING, STATUS_NEEDS_DECISION, STATUS_PARTIAL}:
            seasons.append(season)
    return seasons


def _series_bulk_terminal_no_action_plan(
    plan,
    resolved: dict[int, str] | None = None,
    failed: dict[int, str] | None = None,
) -> bool:
    seasons = tuple(getattr(plan, "seasons", ()) or ())
    if not seasons:
        return False
    if _series_bulk_ready_seasons(plan, resolved, failed):
        return False
    if _series_bulk_decision_seasons(plan, resolved, failed):
        return False
    return all(
        season.status in {STATUS_ALREADY_IN_PLEX, STATUS_ALREADY_DOWNLOADING}
        for season in seasons
    )


def _series_bulk_plan_keyboard(
    plan=None,
    resolved: dict[int, str] | None = None,
    failed: dict[int, str] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if plan is not None and _series_bulk_terminal_no_action_plan(plan, resolved, failed):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
        ]])
    ready_count = len(_series_bulk_ready_seasons(plan, resolved, failed)) if plan is not None else 0
    decision_count = len(_series_bulk_decision_seasons(plan, resolved, failed)) if plan is not None else 0
    pack_count = len(_series_bulk_pack_candidates(plan)) if plan is not None else 0
    if ready_count:
        rows.append([InlineKeyboardButton(
            f"⬇️ Скачать уверенные ({ready_count})",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_confirm",
        )])
    if decision_count:
        rows.append([InlineKeyboardButton(
            f"⚙️ Разобрать спорные ({decision_count})",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_review",
        )])
    if pack_count:
        rows.append([InlineKeyboardButton(
            f"📦 Показать паки сезонов ({pack_count})",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_packs",
        )])
    rows.extend([
        [InlineKeyboardButton("🔄 Пересобрать план", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_rebuild")],
        [InlineKeyboardButton("⬅️ К результатам", callback_data=f"{SEARCH_CALLBACK_PREFIX}:sub_back_results:0")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _series_bulk_confirm_keyboard(ready_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Скачать {ready_count}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_run",
        )],
        [InlineKeyboardButton("⬅️ К плану", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_back_plan")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])


def _series_bulk_pack_list_text(plan) -> str:
    packs = _series_bulk_pack_candidates(plan)
    if not packs:
        return "Паки сезонов не найдены."
    lines = [
        f"📦 Паки сезонов: {plan.series_title}",
        "",
        "Пак — это одна большая раздача на несколько сезонов. Я не выбираю её автоматически: проверьте перевод, качество, размер и сиды.",
        "",
    ]
    for index, result in enumerate(packs[:5], start=1):
        lines.extend([
            f"{index}. {_series_bulk_pack_label(result)}",
            _short_title(result, limit=110),
        ])
    return "\n".join(lines)


def _series_bulk_pack_list_keyboard(plan) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, _result in enumerate(_series_bulk_pack_candidates(plan)[:5], start=1):
        rows.append([InlineKeyboardButton(
            f"📦 Выбрать пак {index}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_pack_confirm:{index - 1}",
        )])
    rows.extend([
        [InlineKeyboardButton("⬅️ К плану", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_back_plan")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _series_bulk_pack_confirm_text(plan, result: dict) -> str:
    season_range = _series_bulk_pack_range(result)
    lines = [
        f"📦 Скачать пак сезонов: {plan.series_title}",
        "",
        _series_bulk_pack_label(result),
        _short_title(result, limit=130),
        "",
        "Скачаю эту раздачу одним торрентом. Это ручной выбор: я не проверяю состав файлов внутри пака.",
    ]
    if season_range:
        lines.append(
            f"После добавления отмечу сезоны {season_range[0]}-{season_range[1]} в этом плане как скачанные паком."
        )
    else:
        lines.append("Диапазон сезонов не распознан, поэтому сам план по сезонам не буду помечать решённым.")
    return "\n".join(lines)


def _series_bulk_pack_confirm_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ Скачать пак",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_pack_run:{index}",
        )],
        [InlineKeyboardButton("⬅️ К пакам", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_packs")],
        [InlineKeyboardButton("⬅️ К плану", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_back_plan")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])


def _series_bulk_done_keyboard(
    has_downloads: bool,
    has_failures: bool = False,
    remaining_decisions: int = 0,
) -> InlineKeyboardMarkup:
    rows = []
    if remaining_decisions:
        label = "⚙️ Разобрать ошибки" if has_failures and remaining_decisions == 1 else "⚙️ Разобрать оставшиеся"
        rows.append([InlineKeyboardButton(
            label,
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_review",
        )])
        rows.append([InlineKeyboardButton("⬅️ К плану", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_back_plan")])
    if has_downloads:
        rows.append([InlineKeyboardButton(
            BUTTON_DOWNLOAD_LIST,
            callback_data=_task_callback("list", TASK_LIST_SCOPE_MY),
        )])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _series_bulk_review_keyboard(
    season_plan: SeasonPlan,
    failed_error: str | None = None,
    failed_candidate_index: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if failed_error:
        if season_plan.selected is not None or season_plan.candidates:
            rows.append([InlineKeyboardButton(
                BUTTON_RETRY,
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_retry",
            )])
        failed_candidate = None
        if failed_candidate_index is not None and 0 <= failed_candidate_index < len(season_plan.candidates):
            failed_candidate = season_plan.candidates[failed_candidate_index]
        elif season_plan.selected is not None:
            failed_candidate = season_plan.selected
        failed_key = _series_bulk_result_key(failed_candidate.result) if failed_candidate is not None else None
        for index, candidate in enumerate(season_plan.candidates[:3]):
            if failed_key is not None and _series_bulk_result_key(candidate.result) == failed_key:
                continue
            rows.append([InlineKeyboardButton(
                f"⬇️ Скачать другой вариант {index + 1}",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_cand_dl:{index}",
            )])
    elif season_plan.status == STATUS_NEEDS_DECISION:
        for index, _candidate in enumerate(season_plan.candidates[:3], start=1):
            rows.append([InlineKeyboardButton(
                f"⬇️ Скачать {index}",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_cand_dl:{index - 1}",
            )])
        rows.append([InlineKeyboardButton(
            "🔄 Искать мягче",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_soft",
        )])
    elif season_plan.status == STATUS_MISSING:
        rows.append([InlineKeyboardButton(
            "🔄 Искать мягче",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_soft",
        )])
    elif season_plan.status == STATUS_PARTIAL:
        rows.extend([
            [InlineKeyboardButton(
                "⬇️ Скачать доступные серии",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_partial:download",
            )],
            [InlineKeyboardButton(
                "⬇️ Скачать доступные + новые по мере выхода",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_partial:each",
            )],
            [InlineKeyboardButton(
                "📦 Скачать, когда сезон завершится",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_partial:after",
            )],
            [InlineKeyboardButton(
                "🔔 Только уведомлять",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_partial:notify",
            )],
            [InlineKeyboardButton(
                "🎯 Сообщить о финале",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_partial:final",
            )],
        ])
    rows.extend([
        [InlineKeyboardButton("⏭ Пропустить сезон", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_skip")],
        [InlineKeyboardButton("⬅️ К плану", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_back_plan")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _series_bulk_error_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BUTTON_RETRY, callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_plan:{index}")],
        [InlineKeyboardButton("⬅️ К результатам", callback_data=f"{SEARCH_CALLBACK_PREFIX}:sub_back_results:0")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])


_SERIES_BULK_LONG_NOTICE_SECONDS = 75.0
_SERIES_BULK_LONG_NOTICE_INTERVAL_SECONDS = 75.0


def _series_bulk_wait_text(
    series_query: str,
    active_stage: str = "seasons",
    *,
    long_running: bool = False,
) -> str:
    stages = [
        ("seasons", "Определяю список сезонов"),
        ("plex", "Проверяю Plex"),
        ("downloads", "Проверяю текущие загрузки"),
        ("search", "Ищу раздачи на трекерах"),
        ("targeted", "Уточняю проблемные сезоны"),
        ("plan", "Оцениваю кандидатов"),
    ]
    seen_active = False
    lines = [
        f"📚 Собираю план: {series_query}",
        "",
        "Это может занять несколько минут: проверю сезоны, Plex, текущие загрузки и раздачи.",
        "",
    ]
    for key, label in stages:
        if key == active_stage:
            lines.append(f"⏳ {label}")
            seen_active = True
        elif seen_active:
            lines.append(f"• {label}")
        else:
            lines.append(f"✅ {label}")
    if long_running:
        lines.extend([
            "",
            "⏳ Всё ещё собираю план.",
            "Некоторые сезоны ищутся отдельно, поэтому это может занять ещё пару минут.",
        ])
    return "\n".join(lines)


async def _series_bulk_edit_wait(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    series_query: str,
    active_stage: str,
    *,
    long_running: bool = False,
) -> None:
    if long_running:
        context.user_data["srch_series_bulk_long_notice"] = True
    context.user_data["srch_series_bulk_wait_stage"] = active_stage
    await query.edit_message_text(
        _series_bulk_wait_text(
            series_query,
            active_stage,
            long_running=bool(context.user_data.get("srch_series_bulk_long_notice")),
        ),
        reply_markup=_series_bulk_wait_keyboard(),
    )


async def _series_bulk_long_notice_loop(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    series_query: str,
    build_token: str,
) -> None:
    try:
        await asyncio.sleep(_SERIES_BULK_LONG_NOTICE_SECONDS)
        while not _series_bulk_build_cancelled(context, build_token):
            if context.user_data.get("srch_series_bulk_build_token") != build_token:
                return
            active_stage = str(context.user_data.get("srch_series_bulk_wait_stage") or "search")
            try:
                await _series_bulk_edit_wait(
                    query,
                    context,
                    series_query,
                    active_stage,
                    long_running=True,
                )
            except Exception:
                logger.debug("Series bulk plan: long notice update failed", exc_info=True)
            await asyncio.sleep(_SERIES_BULK_LONG_NOTICE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise


async def _series_bulk_stop_long_notice(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _series_bulk_send_animation(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None):
    if chat_id is None or not SEARCH_ANIMATION_PATH.exists():
        return None
    try:
        with open(SEARCH_ANIMATION_PATH, "rb") as fh:
            return await context.bot.send_animation(chat_id=chat_id, animation=fh)
    except Exception:
        logger.debug("Series bulk plan: animation send failed", exc_info=True)
        return None


async def _series_bulk_delete_animation(animation_msg) -> None:
    if animation_msg is None:
        return
    try:
        await animation_msg.delete()
    except Exception:
        logger.debug("Series bulk plan: animation cleanup failed", exc_info=True)


def _series_bulk_result_from_jackett(result) -> dict:
    ep = _parse_episode_info(result.title)
    partial = ep is not None and ep[0] < ep[1]
    return {
        "source": "jackett",
        "topic_id": "",
        "title": result.title,
        "url": result.topic_url or "",
        "category": result.tracker,
        "size": result.size,
        "seeders": result.seeders,
        "partial": partial,
        "series": _extract_series_base_query(result.title) is not None,
        "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
        "magnet_url": result.magnet_url,
        "torrent_url": result.torrent_url,
        "tracker_name": result.tracker,
    }


def _series_bulk_result_from_rutracker(result) -> dict:
    ep = _parse_episode_info(result.title)
    partial = ep is not None and ep[0] < ep[1]
    return {
        "source": "rutracker",
        "topic_id": result.topic_id,
        "title": result.title,
        "url": f"https://rutracker.org/forum/viewtopic.php?t={result.topic_id}",
        "category": result.category,
        "size": result.size,
        "seeders": result.seeders,
        "partial": partial,
        "series": _extract_series_base_query(result.title) is not None,
        "ep_str": f"{ep[0]}/{ep[1]} эп." if ep else "",
        "magnet_url": None,
        "torrent_url": None,
        "tracker_name": "rutracker",
    }


def _series_bulk_result_key(result: dict) -> tuple[str, str]:
    source = str(result.get("source") or "")
    stable = (
        str(result.get("topic_id") or "")
        or str(result.get("url") or "")
        or str(result.get("torrent_url") or "")
        or str(result.get("magnet_url") or "")
        or str(result.get("title") or "").lower()
    )
    return source, stable


def _series_bulk_merge_results(*groups: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for group in groups:
        for result in group:
            if not isinstance(result, dict):
                continue
            key = _series_bulk_result_key(result)
            if key in seen:
                continue
            seen.add(key)
            merged.append(result)
    return merged


def _series_bulk_profile_from_snapshot(snapshot: dict | None) -> SeriesBulkProfile:
    data = snapshot if isinstance(snapshot, dict) else {}
    voices_raw = data.get("voices") or ()
    preferred_raw = data.get("preferred_voices") or ()
    if isinstance(voices_raw, str):
        voices = (voices_raw,)
    elif isinstance(voices_raw, (list, tuple, set)):
        voices = tuple(str(value) for value in voices_raw if value)
    else:
        voices = ()
    if isinstance(preferred_raw, str):
        preferred_voices = (preferred_raw,)
    elif isinstance(preferred_raw, (list, tuple, set)):
        preferred_voices = tuple(str(value) for value in preferred_raw if value)
    else:
        preferred_voices = ()
    return SeriesBulkProfile(
        quality=str(data.get("quality") or "any"),
        require_original=bool(data.get("require_original")),
        require_subs=bool(data.get("require_subs")),
        voice_policy=str(data.get("voice_policy") or VOICE_ANY_FROM_REFERENCE),
        voices=voices,
        preferred_voices=preferred_voices,
        release_type=str(data.get("release_type") or ""),
        release_group=str(data.get("release_group") or ""),
        tracker=str(data.get("tracker") or ""),
        source=str(data.get("source") or ""),
    )


def _series_bulk_release_from_snapshot(snapshot: dict | None) -> ReleaseProfile:
    data = snapshot if isinstance(snapshot, dict) else {}
    voices_raw = data.get("voices") or ()
    if isinstance(voices_raw, str):
        voices = (voices_raw,)
    elif isinstance(voices_raw, (list, tuple, set)):
        voices = tuple(str(value) for value in voices_raw if value)
    else:
        voices = ()
    try:
        size_gb = float(data.get("size_gb") or 0.0)
    except (TypeError, ValueError):
        size_gb = 0.0
    return ReleaseProfile(
        quality=str(data.get("quality") or ""),
        release_type=str(data.get("release_type") or ""),
        voices=voices,
        has_original=bool(data.get("has_original")),
        has_subs=bool(data.get("has_subs")),
        has_russian_audio=bool(data.get("has_russian_audio")),
        release_group=str(data.get("release_group") or ""),
        size_gb=size_gb,
    )


def _series_bulk_candidate_from_snapshot(
    snapshot: dict | None,
    *,
    default_season: int,
) -> CandidateEvaluation | None:
    data = snapshot if isinstance(snapshot, dict) else {}
    result = data.get("result")
    if not isinstance(result, dict) or not result:
        return None
    try:
        season = int(data.get("season") or default_season)
    except (TypeError, ValueError):
        season = default_season
    try:
        score = float(data.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0

    episode_progress = None
    raw_progress = data.get("episode_progress")
    if isinstance(raw_progress, (list, tuple)) and len(raw_progress) == 2:
        try:
            episode_progress = (int(raw_progress[0]), int(raw_progress[1]))
        except (TypeError, ValueError):
            episode_progress = None

    return CandidateEvaluation(
        result=_series_bulk_result_snapshot(result),
        season=season,
        release=_series_bulk_release_from_snapshot(data.get("release")),
        score=score,
        confidence=str(data.get("confidence") or STATUS_NEEDS_DECISION),
        reasons=tuple(str(value) for value in data.get("reasons") or ()),
        warnings=tuple(str(value) for value in data.get("warnings") or ()),
        hard_failures=tuple(str(value) for value in data.get("hard_failures") or ()),
        episode_progress=episode_progress,
        gpt_hint=str(data.get("gpt_hint") or ""),
    )


def _series_bulk_plan_from_job(job: dict) -> SeriesBulkPlan | None:
    seasons_raw = job.get("seasons")
    if not isinstance(seasons_raw, dict):
        return None

    seasons: list[SeasonPlan] = []
    for key, entry_raw in sorted(seasons_raw.items(), key=lambda item: str(item[0])):
        entry = entry_raw if isinstance(entry_raw, dict) else {}
        try:
            season_num = int(entry.get("season") or key)
        except (TypeError, ValueError):
            continue
        selected = _series_bulk_candidate_from_snapshot(
            entry.get("selected"),
            default_season=season_num,
        )
        candidates = [
            candidate
            for candidate in (
                _series_bulk_candidate_from_snapshot(item, default_season=season_num)
                for item in entry.get("candidates") or ()
            )
            if candidate is not None
        ]
        if selected is not None:
            selected_key = _series_bulk_result_key(selected.result)
            if all(_series_bulk_result_key(candidate.result) != selected_key for candidate in candidates):
                candidates.insert(0, selected)
        status = str(entry.get("plan_status") or entry.get("status") or STATUS_MISSING)
        seasons.append(SeasonPlan(
            season=season_num,
            status=status,
            selected=selected,
            candidates=tuple(candidates),
            reasons=tuple(str(value) for value in entry.get("reasons") or ()),
        ))

    if not seasons:
        return None

    return SeriesBulkPlan(
        series_title=str(job.get("series_title") or "Сериал"),
        seasons=tuple(sorted(seasons, key=lambda item: item.season)),
        pack_candidates=tuple(
            item for item in (job.get("pack_candidates") or ())
            if isinstance(item, dict)
        ),
        verified_season_range=bool(job.get("verified_season_range", True)),
    )


def _series_bulk_results_from_job(job: dict) -> list[dict]:
    groups: list[list[dict]] = []
    source_result = job.get("source_result")
    if isinstance(source_result, dict) and source_result:
        groups.append([source_result])
    pack_candidates = [item for item in job.get("pack_candidates") or () if isinstance(item, dict)]
    if pack_candidates:
        groups.append(pack_candidates)

    season_results: list[dict] = []
    seasons = job.get("seasons")
    if isinstance(seasons, dict):
        for entry in seasons.values():
            if not isinstance(entry, dict):
                continue
            result = entry.get("result")
            if isinstance(result, dict) and result:
                season_results.append(result)
            selected = entry.get("selected")
            if isinstance(selected, dict) and isinstance(selected.get("result"), dict):
                season_results.append(selected["result"])
            for candidate in entry.get("candidates") or ():
                if isinstance(candidate, dict) and isinstance(candidate.get("result"), dict):
                    season_results.append(candidate["result"])
    if season_results:
        groups.append(season_results)
    return _series_bulk_merge_results(*groups)


_SERIES_BULK_FAILED_RUNTIME_STATUSES = {
    "failed",
    "pending_failed",
    "partial_downloaded_subscription_failed",
}
_SERIES_BULK_RESOLVED_RUNTIME_STATUSES = {
    "downloaded",
    "pack_downloaded",
    "pending_retry",
    "partial_downloaded",
    "downloaded_and_subscribed",
    "subscribed",
    "skipped",
}


def _series_bulk_context_maps_from_job(job: dict) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    resolved: dict[str, str] = {}
    failed: dict[str, str] = {}
    failed_candidates: dict[str, int] = {}
    seasons = job.get("seasons")
    if not isinstance(seasons, dict):
        return resolved, failed, failed_candidates

    for key, entry in seasons.items():
        if not isinstance(entry, dict):
            continue
        try:
            season = int(entry.get("season") or key)
        except (TypeError, ValueError):
            continue
        season_key = str(season)
        runtime_status = str(entry.get("runtime_status") or "")
        summary = str(entry.get("summary") or "")
        if runtime_status in _SERIES_BULK_FAILED_RUNTIME_STATUSES:
            failed[season_key] = str(entry.get("error") or summary or "ошибка")
            failed_candidates[season_key] = 0
        elif runtime_status in _SERIES_BULK_RESOLVED_RUNTIME_STATUSES:
            if runtime_status == "pending_retry":
                summary = summary or "в очереди на повтор"
            elif runtime_status == "downloaded":
                summary = summary or f"скачан: {entry.get('task_id') or entry.get('method') or 'задача создана'}"
            elif runtime_status == "skipped":
                summary = summary or "пропущен"
            else:
                summary = summary or "решено"
            resolved[season_key] = summary
    return resolved, failed, failed_candidates


def _series_bulk_restore_context_from_job(
    context: ContextTypes.DEFAULT_TYPE,
    job_id: str,
    job: dict,
) -> bool:
    plan = _series_bulk_plan_from_job(job)
    if plan is None:
        return False
    profile = _series_bulk_profile_from_snapshot(job.get("profile"))
    results = _series_bulk_results_from_job(job)
    resolved, failed, failed_candidates = _series_bulk_context_maps_from_job(job)
    try:
        result_count = int(job.get("result_count") or len(results))
    except (TypeError, ValueError):
        result_count = len(results)
    context.user_data["srch_series_bulk_plan"] = plan
    context.user_data["srch_series_bulk_profile"] = profile
    context.user_data["srch_series_bulk_results"] = results
    context.user_data["srch_series_bulk_result_count"] = result_count
    context.user_data["srch_series_bulk_warnings"] = tuple(str(value) for value in job.get("warnings") or ())
    context.user_data["srch_series_bulk_resolved"] = resolved
    context.user_data["srch_series_bulk_failed"] = failed
    context.user_data["srch_series_bulk_failed_candidates"] = failed_candidates
    context.user_data["srch_series_bulk_job_id"] = job_id
    context.user_data["srch_results"] = results
    context.user_data["srch_query"] = plan.series_title
    context.user_data["srch_search_query"] = plan.series_title
    context.user_data.pop("srch_series_bulk_review_season", None)
    return True


_SERIES_BULK_JOB_STATUS_LABELS = {
    "planned": "план собран",
    "batch_running": "скачивание идёт",
    "batch_completed": "часть задач добавлена",
    "batch_completed_with_decisions": "есть спорные сезоны",
    "batch_completed_with_pending": "есть очередь повтора",
    "batch_completed_with_errors": "есть ошибки",
    "batch_failed": "добавление не удалось",
    "pack_downloaded": "пак добавлен",
    "cancelled": "отменён",
    "replaced": "пересобран",
}

_SERIES_BULK_HIDDEN_JOB_STATUSES = {"cancelled", "replaced"}


def _series_bulk_int_map(values: dict) -> dict[int, str]:
    normalized: dict[int, str] = {}
    for key, value in values.items():
        try:
            normalized[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return normalized


def _series_bulk_job_is_actionable(job: dict) -> bool:
    if str(job.get("status") or "") in _SERIES_BULK_HIDDEN_JOB_STATUSES:
        return False
    plan = _series_bulk_plan_from_job(job)
    if plan is None:
        return False
    resolved_raw, failed_raw, _failed_candidates = _series_bulk_context_maps_from_job(job)
    resolved = _series_bulk_int_map(resolved_raw)
    failed = _series_bulk_int_map(failed_raw)
    return bool(
        _series_bulk_ready_seasons(plan, resolved, failed)
        or _series_bulk_decision_seasons(plan, resolved, failed)
    )


def _series_bulk_job_matches_chat(job: dict, chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    try:
        return int(job.get("chat_id")) == chat_id
    except (TypeError, ValueError):
        return False


def _series_bulk_jobs_for_chat(chat_id: int | None, *, limit: int = 10) -> list[tuple[str, dict]]:
    try:
        jobs = state_store.load_series_bulk_jobs()
    except Exception:
        logger.warning("Series bulk jobs list failed", exc_info=True)
        return []
    if not isinstance(jobs, dict):
        return []

    items: list[tuple[str, dict]] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if not _series_bulk_job_matches_chat(job, chat_id):
            continue
        if not isinstance(job.get("seasons"), dict):
            continue
        if not _series_bulk_job_is_actionable(job):
            continue
        items.append((str(job_id), job))

    items.sort(
        key=lambda item: str(item[1].get("updated_at") or item[1].get("created_at") or ""),
        reverse=True,
    )
    return items[:limit]


def _series_bulk_short_text(value: object, *, limit: int = 42) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "Без названия"
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _series_bulk_short_datetime(value: object) -> str:
    text = str(value or "")
    try:
        return datetime.fromisoformat(text).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return text or "неизвестно"


def _series_bulk_jobs_text(jobs: list[tuple[str, dict]]) -> str:
    if not jobs:
        return (
            "📚 Сохранённых планов сезонов нет.\n\n"
            "План появится после кнопки «📚 Скачать недостающие сезоны» в найденном сериале."
        )

    lines = [
        "📚 Сохранённые планы сезонов",
        "",
        "Откройте план, чтобы продолжить скачивание или разбор спорных сезонов.",
        "",
    ]
    for index, (_job_id, job) in enumerate(jobs, start=1):
        status = _SERIES_BULK_JOB_STATUS_LABELS.get(
            str(job.get("status") or ""),
            str(job.get("status") or "неизвестно"),
        )
        seasons = job.get("seasons") if isinstance(job.get("seasons"), dict) else {}
        lines.extend([
            f"{index}. {_series_bulk_short_text(job.get('series_title'))}",
            f"   Статус: {status}",
            f"   Сезонов в плане: {len(seasons)}",
            f"   Обновлено: {_series_bulk_short_datetime(job.get('updated_at') or job.get('created_at'))}",
            "",
        ])
    return "\n".join(lines).rstrip()


def _series_bulk_jobs_keyboard(jobs: list[tuple[str, dict]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, (job_id, job) in enumerate(jobs, start=1):
        rows.append([InlineKeyboardButton(
            f"📚 {index}. {_series_bulk_short_text(job.get('series_title'), limit=30)}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_open:{job_id}",
        )])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _series_bulk_soft_search_queries(series_title: str, season: int) -> list[str]:
    raw_queries = [
        _normalize_season_in_query(f"{series_title} Сезон {season}"),
        _normalize_season_in_query(f"{series_title} {season} сезон"),
        f"{series_title} S{season:02d}",
        f"{series_title} Season {season}",
    ]
    seen: set[str] = set()
    queries: list[str] = []
    for query in raw_queries:
        normalized = " ".join(str(query).split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            queries.append(normalized)
    return queries


async def _series_bulk_selected_indexers(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    selected = context.user_data.get("srch_jackett_selected")
    if isinstance(selected, set):
        return selected
    if isinstance(selected, (list, tuple)):
        return {str(item) for item in selected if item}
    if not jackett_client:
        return set()
    try:
        indexers = await asyncio.to_thread(jackett_client.get_indexers)
    except JackettError:
        logger.debug("Series bulk plan: Jackett indexer lookup failed", exc_info=True)
        return set()
    rutracker_ids = {i["id"] for i in indexers if "rutracker" in i["id"].lower()}
    selected = rutracker_ids if rutracker_ids else {i["id"] for i in indexers}
    context.user_data["srch_jackett_indexers"] = indexers
    context.user_data["srch_jackett_selected"] = selected
    return selected


_SERIES_BULK_FETCH_LIMIT_WARNING_PREFIX = "Jackett: выдача достигла лимита"


def _series_bulk_source_warning(source: str, *, timeout: bool = False) -> str:
    if timeout:
        return f"{source}: не ответил вовремя, часть раздач могла не попасть в план."
    return f"{source}: временно недоступен, часть раздач могла не попасть в план."


async def _series_bulk_search_once(
    context: ContextTypes.DEFAULT_TYPE,
    search_query: str,
) -> tuple[list[dict], list[str]]:
    base_query, _quality, _audio, _subs = _split_query_settings(search_query)
    results: list[dict] = []
    warnings: list[str] = []

    selected = await _series_bulk_selected_indexers(context)
    if jackett_client and selected:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(
                    jackett_client.search,
                    base_query,
                    indexers=list(selected),
                    fetch_limit=JACKETT_FETCH_LIMIT,
                ),
                timeout=JACKETT_SEARCH_TIMEOUT_SECONDS + 5.0,
            )
            if JACKETT_FETCH_LIMIT and len(raw) >= JACKETT_FETCH_LIMIT:
                warnings.append(
                    f"{_SERIES_BULK_FETCH_LIMIT_WARNING_PREFIX} {JACKETT_FETCH_LIMIT}, "
                    "часть раздач могла не попасть в план."
                )
            return [_series_bulk_result_from_jackett(item) for item in raw], warnings
        except (JackettError, asyncio.TimeoutError) as exc:
            warnings.append(_series_bulk_source_warning("Jackett", timeout=isinstance(exc, asyncio.TimeoutError)))
            logger.info("Series bulk plan: Jackett search failed for %r: %s", base_query, exc)

    if rutracker_client:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(rutracker_client.search, base_query),
                timeout=45.0,
            )
            results.extend(_series_bulk_result_from_rutracker(item) for item in raw)
        except asyncio.TimeoutError:
            warnings.append(_series_bulk_source_warning("Rutracker", timeout=True))
            logger.info("Series bulk plan: Rutracker search timed out for %r", base_query)
        except RutrackerError as exc:
            warnings.append(_series_bulk_source_warning("Rutracker"))
            logger.info("Series bulk plan: Rutracker search failed for %r: %s", base_query, exc)

    return results, warnings


def _series_bulk_is_fetch_limit_warning(warning: str) -> bool:
    return str(warning).startswith(_SERIES_BULK_FETCH_LIMIT_WARNING_PREFIX)


def _series_bulk_has_fetch_limit_warning(warnings) -> bool:
    return any(_series_bulk_is_fetch_limit_warning(warning) for warning in warnings)


def _series_bulk_without_fetch_limit_warnings(warnings: list[str]) -> list[str]:
    return [
        warning
        for warning in warnings
        if not _series_bulk_is_fetch_limit_warning(warning)
    ]


def _series_bulk_seasons_for_targeted_search(
    plan,
    *,
    fetch_limit_supplement: bool = False,
) -> list[int]:
    if fetch_limit_supplement:
        return [
            season.season
            for season in plan.seasons
            if season.status not in {STATUS_ALREADY_IN_PLEX, STATUS_ALREADY_DOWNLOADING}
        ]
    return [
        season.season
        for season in plan.seasons
        if season.status in {STATUS_MISSING, STATUS_NEEDS_DECISION, STATUS_PARTIAL}
    ]


def _series_bulk_profile_from_result(
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
) -> SeriesBulkProfile:
    title = str(result.get("title") or "")
    release = release_profile_from_title(title, size=str(result.get("size") or ""))
    preferred_voices = tuple(
        voice
        for voice in _normalise_preferred_voices(context.user_data.get("srch_voice_hints"))
        if voice in release.voices
    )
    search_query = str(
        context.user_data.get("srch_search_query")
        or context.user_data.get("srch_query")
        or title
    )
    _base_query, preferred_quality, audio_required, subs_required = _split_query_settings(search_query)
    settings = context.user_data.get("srch_settings") or {}
    audio_source = _search_setting_source(context, "audio")
    subs_source = _search_setting_source(context, "subs")
    quality = (
        context.user_data.get("srch_preferred_quality")
        or preferred_quality
        or settings.get("quality")
        or "any"
    )
    if quality == "any":
        quality = "any"
    audio_required = bool((audio_required or settings.get("audio")) and audio_source == "explicit")
    subs_required = bool((subs_required or settings.get("subs")) and subs_source == "explicit")
    return SeriesBulkProfile(
        quality=quality,
        require_original=audio_required,
        require_subs=subs_required,
        voice_policy=VOICE_ANY_FROM_REFERENCE,
        voices=release.voices,
        preferred_voices=preferred_voices,
        release_type=release.release_type,
        release_group=release.release_group,
        tracker=str(result.get("tracker_name") or result.get("category") or ""),
        source=str(result.get("source") or ""),
    )


def _series_bulk_profile_copy(
    profile: SeriesBulkProfile,
    **updates,
) -> SeriesBulkProfile:
    data = {
        "quality": profile.quality,
        "require_original": profile.require_original,
        "require_subs": profile.require_subs,
        "voice_policy": profile.voice_policy,
        "voices": profile.voices,
        "preferred_voices": profile.preferred_voices,
        "release_type": profile.release_type,
        "release_group": profile.release_group,
        "tracker": profile.tracker,
        "source": profile.source,
    }
    data.update(updates)
    return SeriesBulkProfile(**data)


def _series_bulk_soft_profile(profile: SeriesBulkProfile | None) -> SeriesBulkProfile:
    base = profile if isinstance(profile, SeriesBulkProfile) else SeriesBulkProfile()
    return _series_bulk_profile_copy(
        base,
        quality="any",
        require_original=False,
        require_subs=False,
        voice_policy=VOICE_ANY_FROM_REFERENCE,
        voices=(),
        preferred_voices=(),
        release_type="",
        release_group="",
    )


def _series_bulk_profile_voice_label(profile: SeriesBulkProfile) -> str:
    voices = " / ".join(profile.voices[:3])
    preferred = " / ".join(profile.preferred_voices[:2])
    if profile.voice_policy == VOICE_SINGLE_FROM_REFERENCE:
        return "одна на все сезоны"
    if profile.voice_policy == VOICE_ANY_RUSSIAN:
        return "любая русская"
    if profile.voice_policy == VOICE_REQUIRE_SELECTED:
        return f"выбрано: {voices}" if voices else "выбранные студии"
    if profile.voice_policy == VOICE_ORIGINAL_ONLY:
        return "только Original"
    if preferred:
        base = f"любая из эталона — {voices}" if voices else "любая из эталона"
        return f"{base}; предпочитаю {preferred}"
    return f"любая из эталона — {voices}" if voices else "любая из эталона"


def _series_bulk_profile_voice_button_label(profile: SeriesBulkProfile) -> str:
    if profile.voice_policy == VOICE_SINGLE_FROM_REFERENCE:
        return "одна на все сезоны"
    if profile.voice_policy == VOICE_REQUIRE_SELECTED:
        voices = " / ".join(profile.voices[:2])
        return voices or "вручную"
    if profile.voice_policy == VOICE_ANY_RUSSIAN:
        return "любая русская"
    if profile.voice_policy == VOICE_ORIGINAL_ONLY:
        return "только Original"
    return "любая из эталона"


def _series_bulk_reference_voices(result: dict) -> tuple[str, ...]:
    release = release_profile_from_title(
        str(result.get("title") or ""),
        size=str(result.get("size") or ""),
    )
    return release.voices


def _series_bulk_profile_text(result: dict, profile: SeriesBulkProfile) -> str:
    title = str(result.get("title") or "")[:120]
    quality = profile.quality if profile.quality and profile.quality != "any" else "любое"
    return "\n".join([
        "📚 Скачать недостающие сезоны",
        "",
        "Эталон:",
        title,
        "",
        "Что сохраню при подборе:",
        "",
        f"• Качество: {quality}",
        f"• Original: {'нужен' if profile.require_original else 'не обязательно'}",
        f"• Субтитры: {'нужны' if profile.require_subs else 'не обязательно'}",
        f"• Озвучка: {_series_bulk_profile_voice_label(profile)}",
    ])


def _series_bulk_profile_keyboard(
    index: int,
    profile: SeriesBulkProfile,
    *,
    voice_expanded: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            "✅ Собрать план",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_build",
        )],
        [InlineKeyboardButton(
            f"🎙 Озвучка: {_series_bulk_profile_voice_button_label(profile)}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:voice_toggle",
        )],
    ]
    if voice_expanded:
        voice_options = [
            ("Любая из эталона", "voice_ref", VOICE_ANY_FROM_REFERENCE),
            ("Одна на все сезоны", "voice_single", VOICE_SINGLE_FROM_REFERENCE),
        ]
        for label, action, policy in voice_options:
            prefix = "☑️" if profile.voice_policy == policy else "⬜"
            rows.append([InlineKeyboardButton(
                f"{prefix} {label}",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:{action}",
            )])
        selected_prefix = "☑️" if profile.voice_policy == VOICE_REQUIRE_SELECTED else "⬜"
        rows.append([InlineKeyboardButton(
            f"{selected_prefix} Выбрать вручную",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:voice_manual",
        )])
    rows.extend([
        [InlineKeyboardButton("⚙️ Остальные настройки", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:settings")],
        [InlineKeyboardButton("⬅️ К выбору", callback_data=f"{SEARCH_CALLBACK_PREFIX}:dl_pick:{index}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _series_bulk_profile_settings_text(result: dict, profile: SeriesBulkProfile) -> str:
    title = str(result.get("title") or "")[:120]
    quality = profile.quality if profile.quality and profile.quality != "any" else "любое"
    return "\n".join([
        "⚙️ Настроить подбор",
        "",
        "Эталон:",
        title,
        "",
        f"• Качество: {quality}",
        f"• Original: {'нужен' if profile.require_original else 'не обязательно'}",
        f"• Субтитры: {'нужны' if profile.require_subs else 'не обязательно'}",
    ])


def _series_bulk_profile_settings_keyboard(
    index: int,
    profile: SeriesBulkProfile,
) -> InlineKeyboardMarkup:
    quality = profile.quality if profile.quality and profile.quality != "any" else "любое"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🎞 Качество: {quality}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:quality",
        )],
        [InlineKeyboardButton(
            f"🎧 Original: {'нужен' if profile.require_original else 'не обязательно'}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:original",
        )],
        [InlineKeyboardButton(
            f"💬 Субтитры: {'нужны' if profile.require_subs else 'не обязательно'}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:subs",
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:settings_back")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])


def _series_bulk_voice_manual_text(result: dict, selected: set[str]) -> str:
    voices = _series_bulk_reference_voices(result)
    lines = [
        "🎙 Озвучка",
        "",
        "Выберите одну или две озвучки из эталонной раздачи.",
        "",
    ]
    if voices:
        for voice in voices:
            mark = "☑️" if voice in selected else "⬜"
            lines.append(f"{mark} {voice}")
    else:
        lines.append("В эталоне не распознал конкретные студии озвучки.")
    return "\n".join(lines)


def _series_bulk_voice_manual_keyboard(result: dict, selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, voice in enumerate(_series_bulk_reference_voices(result)):
        mark = "☑️" if voice in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {voice}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:voice_pick_{index}",
        )])
    if selected:
        rows.append([InlineKeyboardButton(
            "💾 Сохранить выбор",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:voice_done",
        )])
    rows.extend([
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"{SEARCH_CALLBACK_PREFIX}:bulk_prof:voice_back")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _series_bulk_profile_from_context(
    context: ContextTypes.DEFAULT_TYPE,
) -> SeriesBulkProfile | None:
    profile = context.user_data.get("srch_series_bulk_profile_draft")
    return profile if isinstance(profile, SeriesBulkProfile) else None


def _series_bulk_current_index_and_result(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int, dict] | None:
    try:
        index = int(context.user_data.get("srch_series_bulk_index"))
    except (TypeError, ValueError):
        return None

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        return None
    result = results[index]
    return index, result if isinstance(result, dict) else {}


async def _series_bulk_show_profile(
    query,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    current = _series_bulk_current_index_and_result(context)
    if current is None:
        await query.edit_message_text("План потерян. Вернитесь к результатам и откройте его заново.")
        return SEARCH_RESULTS
    index, result = current

    profile = _series_bulk_profile_from_context(context)
    if profile is None:
        profile = _series_bulk_profile_from_result(context, result)
        context.user_data["srch_series_bulk_profile_draft"] = profile
    if context.user_data.get("srch_series_bulk_profile_screen") == "settings":
        await query.edit_message_text(
            _series_bulk_profile_settings_text(result, profile),
            reply_markup=_series_bulk_profile_settings_keyboard(index, profile),
        )
        return SEARCH_RESULTS
    await query.edit_message_text(
        _series_bulk_profile_text(result, profile),
        reply_markup=_series_bulk_profile_keyboard(
            index,
            profile,
            voice_expanded=bool(context.user_data.get("srch_series_bulk_voice_expanded")),
        ),
    )
    return SEARCH_RESULTS


async def _series_bulk_show_voice_manual(
    query,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    current = _series_bulk_current_index_and_result(context)
    if current is None:
        await query.edit_message_text("План потерян. Вернитесь к результатам и откройте его заново.")
        return SEARCH_RESULTS
    _index, result = current
    selected = context.user_data.get("srch_series_bulk_voice_manual")
    selected_set = set(selected) if isinstance(selected, (set, list, tuple)) else set()
    await query.edit_message_text(
        _series_bulk_voice_manual_text(result, selected_set),
        reply_markup=_series_bulk_voice_manual_keyboard(result, selected_set),
    )
    return SEARCH_RESULTS


async def search_series_bulk_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    profile = _series_bulk_profile_from_context(context)
    if profile is None:
        await query.answer()
        return await _series_bulk_show_profile(query, context)

    action = query.data.rsplit(":", 1)[-1]
    answer_text: str | None = None
    if action == "quality":
        base_quality = str(context.user_data.get("srch_series_bulk_base_quality") or "1080p")
        profile = _series_bulk_profile_copy(
            profile,
            quality=base_quality if profile.quality == "any" else "any",
        )
    elif action == "original":
        profile = _series_bulk_profile_copy(profile, require_original=not profile.require_original)
    elif action == "subs":
        profile = _series_bulk_profile_copy(profile, require_subs=not profile.require_subs)
    elif action == "settings":
        context.user_data["srch_series_bulk_profile_screen"] = "settings"
    elif action == "settings_back":
        context.user_data.pop("srch_series_bulk_profile_screen", None)
    elif action == "voice_toggle":
        context.user_data.pop("srch_series_bulk_profile_screen", None)
        expanded = bool(context.user_data.get("srch_series_bulk_voice_expanded"))
        context.user_data["srch_series_bulk_voice_expanded"] = not expanded
    elif action == "voice_ref":
        current = _series_bulk_current_index_and_result(context)
        voices = _series_bulk_reference_voices(current[1]) if current else profile.voices
        profile = _series_bulk_profile_copy(
            profile,
            voice_policy=VOICE_ANY_FROM_REFERENCE,
            voices=voices,
        )
        context.user_data["srch_series_bulk_voice_expanded"] = False
    elif action == "voice_single":
        current = _series_bulk_current_index_and_result(context)
        voices = _series_bulk_reference_voices(current[1]) if current else profile.voices
        profile = _series_bulk_profile_copy(
            profile,
            voice_policy=VOICE_SINGLE_FROM_REFERENCE,
            voices=voices,
        )
        context.user_data["srch_series_bulk_voice_expanded"] = False
    elif action == "voice_any_ru":
        profile = _series_bulk_profile_copy(profile, voice_policy=VOICE_ANY_RUSSIAN)
    elif action == "voice_selected":
        profile = _series_bulk_profile_copy(profile, voice_policy=VOICE_REQUIRE_SELECTED)
    elif action == "voice_original":
        profile = _series_bulk_profile_copy(
            profile,
            voice_policy=VOICE_ORIGINAL_ONLY,
            require_original=True,
        )
    elif action == "voice_manual":
        selected = profile.voices if profile.voice_policy == VOICE_REQUIRE_SELECTED else ()
        context.user_data["srch_series_bulk_voice_manual"] = set(selected)
        await query.answer()
        return await _series_bulk_show_voice_manual(query, context)
    elif action.startswith("voice_pick_"):
        current = _series_bulk_current_index_and_result(context)
        voices = _series_bulk_reference_voices(current[1]) if current else ()
        try:
            voice_index = int(action.removeprefix("voice_pick_"))
        except ValueError:
            voice_index = -1
        if not (0 <= voice_index < len(voices)):
            answer_text = "Озвучка недоступна"
        else:
            selected = context.user_data.get("srch_series_bulk_voice_manual")
            selected_set = set(selected) if isinstance(selected, (set, list, tuple)) else set()
            voice = voices[voice_index]
            if voice in selected_set:
                selected_set.remove(voice)
            elif len(selected_set) >= 2:
                answer_text = "Можно выбрать до двух озвучек"
            else:
                selected_set.add(voice)
            context.user_data["srch_series_bulk_voice_manual"] = selected_set
        await query.answer(answer_text)
        return await _series_bulk_show_voice_manual(query, context)
    elif action == "voice_done":
        selected = context.user_data.get("srch_series_bulk_voice_manual")
        selected_set = set(selected) if isinstance(selected, (set, list, tuple)) else set()
        current = _series_bulk_current_index_and_result(context)
        reference_voices = _series_bulk_reference_voices(current[1]) if current else ()
        selected_tuple = tuple(voice for voice in reference_voices if voice in selected_set)
        if not selected_tuple:
            await query.answer("Выберите одну или две озвучки")
            return await _series_bulk_show_voice_manual(query, context)
        profile = _series_bulk_profile_copy(
            profile,
            voice_policy=VOICE_REQUIRE_SELECTED,
            voices=selected_tuple,
        )
        context.user_data.pop("srch_series_bulk_voice_manual", None)
        context.user_data["srch_series_bulk_voice_expanded"] = False
    elif action == "voice_back":
        context.user_data.pop("srch_series_bulk_voice_manual", None)
        context.user_data["srch_series_bulk_voice_expanded"] = True
    context.user_data["srch_series_bulk_profile_draft"] = profile
    await query.answer(answer_text)
    return await _series_bulk_show_profile(query, context)


async def _series_bulk_known_seasons(series_query: str, results: list[dict]) -> tuple[list[int], bool]:
    if kinopoisk_client:
        try:
            total = await asyncio.wait_for(
                asyncio.to_thread(kinopoisk_client.search_series_seasons, series_query),
                timeout=8,
            )
            if total and int(total) > 0:
                return list(range(1, int(total) + 1)), True
        except Exception:
            logger.debug("Series bulk plan: season count lookup failed", exc_info=True)

    seasons = set(_seasons_available_in_results(results))
    return sorted(seasons), False


async def _series_bulk_downloading_seasons(series_query: str) -> set[int]:
    if ds_client is None:
        return set()
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except Exception:
        logger.debug("Series bulk plan: Download Station task lookup failed", exc_info=True)
        return set()

    target = _normalize_movie_title(series_query).lower()
    found: set[int] = set()
    for task in tasks:
        if str(task.get("status") or "").lower() not in _ACTIVE_STATUSES:
            continue
        title = str(task.get("title") or task.get("id") or "")
        season = _extract_season_from_query(title)
        if not season:
            continue
        task_series = _extract_series_base_query(title) or title
        if target and target in _normalize_movie_title(task_series).lower():
            found.add(season)
    return found


def _series_bulk_candidate_label(candidate) -> str:
    release = candidate.release
    parts = [
        release.release_type,
        release.quality,
        "/".join(release.voices[:2]),
    ]
    if release.has_original:
        parts.append("Original")
    if release.has_subs:
        parts.append("Sub")
    label = " · ".join(part for part in parts if part)
    if label:
        return label
    return _short_title(candidate.result, limit=72)


def _series_bulk_pack_candidates(plan) -> tuple[dict, ...]:
    return tuple(
        result
        for result in getattr(plan, "pack_candidates", ()) or ()
        if isinstance(result, dict)
    )


def _series_bulk_pack_range(result: dict) -> tuple[int, int] | None:
    return season_pack_range_from_title(str(result.get("title") or ""))


def _series_bulk_pack_label(result: dict) -> str:
    title = str(result.get("title") or "")
    release = release_profile_from_title(title, size=str(result.get("size") or ""))
    season_range = _series_bulk_pack_range(result)
    parts: list[str] = []
    if season_range:
        parts.append(f"сезоны {season_range[0]}-{season_range[1]}")
    if release.release_type:
        parts.append(release.release_type)
    if release.quality:
        parts.append(release.quality)
    if release.voices:
        parts.append(" / ".join(release.voices[:2]))
    if release.has_original:
        parts.append("Original")
    if release.has_subs:
        parts.append("Sub")
    if result.get("size"):
        parts.append(str(result.get("size")))
    if result.get("seeders") is not None:
        parts.append(f"сиды: {result.get('seeders')}")
    return " · ".join(parts) or _short_title(result, limit=72)


def _series_bulk_profile_line(profile: SeriesBulkProfile | None) -> str:
    if profile is None:
        return ""
    parts: list[str] = []
    if profile.quality and profile.quality != "any":
        parts.append(profile.quality)
    if profile.require_original:
        parts.append("Original")
    if profile.require_subs:
        parts.append("Sub")
    if profile.voices:
        parts.append(" / ".join(profile.voices[:3]))
    return " · ".join(parts)


def _series_bulk_reason_for_user(reason: str) -> str:
    reason = str(reason or "").strip()
    if not reason:
        return ""
    if reason.startswith("quality matches "):
        return f"качество совпало: {reason.removeprefix('quality matches ')}"
    if reason.startswith("voice matched: "):
        return f"озвучка совпала: {reason.removeprefix('voice matched: ')}"
    if reason.startswith("voice from reference matched: "):
        return f"озвучка из эталона совпала: {reason.removeprefix('voice from reference matched: ')}"
    if reason.startswith("release type matches "):
        return f"тип релиза совпал: {reason.removeprefix('release type matches ')}"
    if reason.startswith("release type differs: "):
        return "тип релиза отличается"
    mapping = {
        "season matches": "сезон совпал",
        "original audio found": "Original-дорожка найдена",
        "original-only policy matched": "Original-дорожка найдена",
        "subtitles found": "субтитры найдены",
        "release group matches": "релиз-группа совпала",
        "release group differs": "релиз-группа отличается",
        "russian audio looks present": "русская дорожка найдена",
        "voice preference is not constrained": "озвучка не ограничена",
        "no selected voice to require": "озвучка не выбрана",
        "no seeders reported": "сидов не видно",
        "season is partial": "сезон неполный",
        "quality does not match search preference": "качество не совпало с профилем",
        "original audio not found": "не найдена Original-дорожка",
        "subtitles not found": "не найдены субтитры",
        "selected voice not found": "не найдена выбранная озвучка",
        "russian audio not found": "не найдена русская дорожка",
        "no voice from reference found": "не найдена озвучка из эталона",
        "no candidate passed all hard filters": "нет варианта, который прошёл все требования",
        "multiple candidates are too close to auto-select": "несколько вариантов слишком близки",
        "soft search candidates": "варианты найдены мягким поиском",
        "season is not complete yet": "сезон ещё не полный",
    }
    return mapping.get(reason, reason)


def _series_bulk_first_reason_for_user(reasons: tuple[str, ...] | list[str] | None) -> str:
    for reason in reasons or ():
        text = _series_bulk_reason_for_user(reason)
        if text:
            return text
    return ""


def _series_bulk_candidate_confidence_label(candidate) -> str:
    confidence = getattr(candidate, "confidence", "")
    if confidence == STATUS_EXACT:
        return "✅ Уверенно"
    if confidence == STATUS_GOOD:
        return "🟡 Похоже"
    if confidence == STATUS_PARTIAL:
        return "⏳ Неполный сезон"
    return "⚠️ Нужно проверить"


def _series_bulk_candidate_explanation(candidate) -> str:
    if candidate is None:
        return ""
    if getattr(candidate, "episode_progress", None):
        cur, total = candidate.episode_progress
        if cur < total:
            return f"доступно {cur}/{total} серий"
    if getattr(candidate, "hard_failures", None):
        return _series_bulk_first_reason_for_user(candidate.hard_failures)
    if getattr(candidate, "warnings", None):
        return _series_bulk_first_reason_for_user(candidate.warnings)
    if getattr(candidate, "confidence", "") == STATUS_EXACT:
        return "совпали ключевые параметры"
    return _series_bulk_first_reason_for_user(getattr(candidate, "reasons", ()))


def _series_bulk_candidate_gpt_payload(candidate) -> dict:
    result = getattr(candidate, "result", {}) or {}
    release = getattr(candidate, "release", None)
    return {
        "title": str(result.get("title") or ""),
        "tracker": str(result.get("tracker_name") or result.get("source") or ""),
        "seeders": result.get("seeders"),
        "size": str(result.get("size") or ""),
        "score": getattr(candidate, "score", 0.0),
        "confidence": getattr(candidate, "confidence", ""),
        "reasons": list(getattr(candidate, "reasons", ()) or ()),
        "warnings": list(getattr(candidate, "warnings", ()) or ()),
        "hard_failures": list(getattr(candidate, "hard_failures", ()) or ()),
        "episode_progress": list(candidate.episode_progress) if getattr(candidate, "episode_progress", None) else None,
        "quality": getattr(release, "quality", ""),
        "release_type": getattr(release, "release_type", ""),
        "voices": list(getattr(release, "voices", ()) or ()),
        "has_original": bool(getattr(release, "has_original", False)),
        "has_subs": bool(getattr(release, "has_subs", False)),
        "release_group": getattr(release, "release_group", ""),
    }


def _series_bulk_apply_gpt_hints(season_plan: SeasonPlan, hints: dict[int, str]) -> SeasonPlan:
    if not hints:
        return season_plan
    candidates = []
    for index, candidate in enumerate(season_plan.candidates):
        hint = str(hints.get(index) or "").strip()
        candidates.append(replace(candidate, gpt_hint=hint) if hint else candidate)
    selected = season_plan.selected
    if selected is not None:
        selected_key = _series_bulk_result_key(selected.result)
        for candidate in candidates:
            if _series_bulk_result_key(candidate.result) == selected_key:
                selected = candidate
                break
    return replace(season_plan, selected=selected, candidates=tuple(candidates))


async def _gpt_enrich_series_bulk_plan(
    plan,
    profile: SeriesBulkProfile,
    *,
    max_seasons: int = 3,
) -> object:
    if not GPT_ENABLED or plan is None:
        return plan
    targets = [
        season
        for season in getattr(plan, "seasons", ())
        if season.status == STATUS_NEEDS_DECISION and season.candidates
    ][:max_seasons]
    if not targets:
        return plan

    profile_payload = _series_bulk_profile_snapshot(profile)
    sinks: list[list] = [[] for _ in targets]

    def _call(season_plan: SeasonPlan, sink: list):
        return gpt_explain_series_bulk_candidates(
            series_title=str(getattr(plan, "series_title", "")),
            season=season_plan.season,
            profile=profile_payload,
            candidates=[
                _series_bulk_candidate_gpt_payload(candidate)
                for candidate in season_plan.candidates[:3]
            ],
            api_key=OPENAI_API_KEY,
            model=GPT_MODEL,
            usage_sink=sink,
        )

    outcomes = await asyncio.gather(
        *[
            asyncio.to_thread(_call, season_plan, sink)
            for season_plan, sink in zip(targets, sinks)
        ],
        return_exceptions=True,
    )

    hints_by_season: dict[int, dict[int, str]] = {}
    for season_plan, outcome, sink in zip(targets, outcomes, sinks):
        if isinstance(outcome, Exception):
            logger.warning(
                "series_bulk GPT review failed: season=%s error=%s",
                season_plan.season,
                outcome,
            )
            continue
        hints, error = outcome
        _gpt_record_usage(
            feature="series_bulk_review",
            input_tokens=450,
            output_tokens=160,
            error_label=error,
            usage=(sink[0] if sink else None),
        )
        if hints:
            hints_by_season[season_plan.season] = hints
    if not hints_by_season:
        return plan

    seasons = tuple(
        _series_bulk_apply_gpt_hints(season, hints_by_season.get(season.season, {}))
        for season in getattr(plan, "seasons", ())
    )
    return replace(plan, seasons=seasons)


def _series_bulk_plan_reason(season_plan: SeasonPlan) -> str:
    reason = _series_bulk_first_reason_for_user(getattr(season_plan, "reasons", ()))
    if reason:
        return reason
    if season_plan.candidates:
        return _series_bulk_candidate_explanation(season_plan.candidates[0])
    return ""


def _series_bulk_status_line(
    season_plan: SeasonPlan,
    failed_error: str | None = None,
    resolved_summary: str | None = None,
) -> str:
    season = season_plan.season
    if failed_error:
        return f"⚠️ Сезон {season} - не удалось добавить, нужен разбор"
    if resolved_summary:
        if "очеред" in resolved_summary:
            return f"⏳ Сезон {season} - {resolved_summary}"
        if "пропущ" in resolved_summary:
            return f"⏭ Сезон {season} - {resolved_summary}"
        return f"✅ Сезон {season} - {resolved_summary}"
    if season_plan.status == STATUS_ALREADY_IN_PLEX:
        return f"✅ Сезон {season} - уже есть в Plex"
    if season_plan.status == STATUS_ALREADY_DOWNLOADING:
        return f"⏬ Сезон {season} - уже качается"
    if season_plan.status == STATUS_EXACT and season_plan.selected:
        return f"✅ Сезон {season} - уверенно: {_series_bulk_candidate_label(season_plan.selected)}"
    if season_plan.status == STATUS_GOOD and season_plan.selected:
        hint = _series_bulk_candidate_explanation(season_plan.selected)
        suffix = f" ({hint})" if hint else ""
        return f"🟡 Сезон {season} - похоже: {_series_bulk_candidate_label(season_plan.selected)}{suffix}"
    if season_plan.status == STATUS_NEEDS_DECISION:
        count = len(season_plan.candidates)
        reason = _series_bulk_plan_reason(season_plan)
        suffix = f" ({reason})" if reason else ""
        return f"⚠️ Сезон {season} - нужно проверить: кандидатов {count}{suffix}"
    if season_plan.status == STATUS_PARTIAL:
        candidate = season_plan.candidates[0] if season_plan.candidates else None
        if candidate and candidate.episode_progress:
            cur, total = candidate.episode_progress
            return f"⏳ Сезон {season} - неполный сезон: доступно {cur}/{total} серий"
        return f"⏳ Сезон {season} - неполный сезон, нужно решить"
    if season_plan.status == STATUS_MISSING:
        return f"❌ Сезон {season} - не найдено"
    return f"• Сезон {season} - {season_plan.status}"


def _series_bulk_plan_text(
    plan,
    profile: SeriesBulkProfile,
    *,
    result_count: int,
    warnings: tuple[str, ...] = (),
    resolved: dict[int, str] | None = None,
    failed: dict[int, str] | None = None,
) -> str:
    resolved = resolved or {}
    failed = failed or {}
    quality = profile.quality if profile.quality and profile.quality != "any" else "любое"
    voices = " / ".join(profile.voices) if profile.voices else "не ограничиваю"
    preferred_voices = " / ".join(profile.preferred_voices)
    lines = [
        f"📚 Скачать недостающие сезоны: {plan.series_title}",
        "",
        "Перед скачиванием показываю план, чтобы не добавить лишнее.",
        "",
        "Профиль:",
        f"• Качество: {quality}",
        f"• Оригинальная дорожка: {'нужна' if profile.require_original else 'не требовалась'}",
        f"• Субтитры: {'нужны' if profile.require_subs else 'не требовались'}",
        f"• Озвучка: {voices}",
    ]
    if preferred_voices and profile.voice_policy in {VOICE_ANY_FROM_REFERENCE, VOICE_ANY_RUSSIAN}:
        lines.append(f"• Предпочитаю перевод: {preferred_voices}")
    if profile.release_type:
        lines.append(f"• Тип релиза: {profile.release_type}")
    if not plan.verified_season_range:
        lines.extend([
            "",
            "⚠️ Полный диапазон сезонов не подтверждён.",
            "Показываю сезоны, которые удалось найти на трекерах.",
        ])
    if warnings:
        lines.extend([
            "",
            "⚠️ План собран не полностью:",
            *[f"• {warning}" for warning in dict.fromkeys(warnings)],
        ])
    lines.extend(["", f"Проверено раздач: {result_count}", ""])
    lines.extend(
        _series_bulk_status_line(
            season,
            failed.get(season.season),
            resolved.get(season.season),
        )
        for season in plan.seasons
    )

    ready = len(_series_bulk_ready_seasons(plan, resolved, failed))
    skipped = sum(1 for season in plan.seasons if season.status in {
        STATUS_ALREADY_IN_PLEX,
        STATUS_ALREADY_DOWNLOADING,
    })
    decisions = len(_series_bulk_decision_seasons(plan, resolved, failed))
    lines.extend([
        "",
        f"Можно скачать после подтверждения: {ready}",
        f"Пропущу: {skipped}",
        f"Нужно решение: {decisions}",
    ])
    if ready == 0:
        if decisions:
            lines.extend([
                "",
                "Автоматически скачивать нечего: оставшиеся сезоны требуют ручного выбора.",
                "Следующее действие: нажмите «⚙️ Разобрать спорные».",
            ])
        elif skipped:
            lines.extend([
                "",
                "Автоматически скачивать нечего: сезоны уже есть в Plex или уже качаются.",
            ])
        elif plan.pack_candidates:
            lines.extend([
                "",
                "Автоматически скачивать нечего, но есть паки сезонов для ручной проверки.",
            ])
    if resolved:
        lines.extend(["", "Решено вручную:"])
        for season, summary in sorted(resolved.items()):
            lines.append(f"• Сезон {season} - {summary}")
    if plan.pack_candidates:
        lines.append(f"Найдены паки сезонов: {len(plan.pack_candidates)} (не выбираю автоматически)")
    return "\n".join(lines)


def _series_bulk_no_action_text(
    plan,
    *,
    result_count: int,
    warnings: tuple[str, ...] = (),
) -> str:
    has_plex = any(season.status == STATUS_ALREADY_IN_PLEX for season in plan.seasons)
    has_downloading = any(season.status == STATUS_ALREADY_DOWNLOADING for season in plan.seasons)
    if has_plex and has_downloading:
        summary = "Все сезоны уже есть в Plex или уже стоят в загрузке."
    elif has_downloading:
        summary = "Все сезоны уже стоят в загрузке."
    else:
        summary = "Все сезоны уже есть в Plex."

    lines = [
        f"📚 Скачать недостающие сезоны: {plan.series_title}",
        "",
        summary,
        "Скачивать или разбирать нечего, поэтому план не сохраняю.",
        "",
        f"Проверено раздач: {result_count}",
        "",
    ]
    lines.extend(_series_bulk_status_line(season, None, None) for season in plan.seasons)
    if warnings:
        lines.extend([
            "",
            "⚠️ Во время проверки были предупреждения:",
            *[f"• {warning}" for warning in dict.fromkeys(warnings)],
        ])
    return "\n".join(lines)


_SERIES_BULK_LARGE_TASK_COUNT = 20


def _series_bulk_confirm_text(
    plan,
    resolved: dict[int, str] | None = None,
    failed: dict[int, str] | None = None,
) -> str:
    ready = _series_bulk_ready_seasons(plan, resolved, failed)
    if not ready:
        return (
            "📚 Уверенных сезонов для автоскачивания нет.\n\n"
            "Я не буду добавлять раздачи наугад. Спорные, неполные или ненайденные сезоны нужно разобрать вручную."
        )

    lines = [
        f"📚 Скачать уверенные сезоны: {plan.series_title}",
        "",
        f"Будет создано задач: {len(ready)}",
    ]
    if len(ready) > _SERIES_BULK_LARGE_TASK_COUNT:
        lines.extend([
            "",
            f"⚠️ Это много задач: {len(ready)}.",
            "Проверьте место на NAS и список сезонов перед подтверждением.",
        ])
    lines.append("")
    lines.extend(_series_bulk_status_line(season) for season in ready)
    lines.extend([
        "",
        "Скачаю только эти сезоны. Спорные, неполные, уже имеющиеся в Plex и уже качающиеся пропущу.",
    ])
    return "\n".join(lines)


def _series_bulk_done_text(
    successes: list[dict],
    failures: list[dict],
    pending_retries: list[dict] | None = None,
    remaining_decisions: int = 0,
) -> str:
    pending_retries = pending_retries or []
    lines = ["✅ План обработан", ""]
    if successes:
        lines.append(f"Добавлено задач: {len(successes)}")
        for item in successes:
            lines.append(
                f"✅ Сезон {item['season']} - {item['task_id']} ({item['method']})"
            )
    else:
        lines.append("Добавлено задач: 0")
    if pending_retries:
        interval_min = max(1, PENDING_DOWNLOADS_INTERVAL_SECONDS // 60)
        lines.extend(["", f"В очереди на повтор: {len(pending_retries)}"])
        for item in pending_retries:
            lines.append(f"⏳ Сезон {item['season']} - попробую снова через ~{interval_min} мин")
    if failures:
        lines.extend(["", f"Требуют решения: {len(failures)}"])
        for item in failures:
            lines.append(f"❌ Сезон {item['season']} - {item['error']}")
    if failures:
        lines.extend(["", "Можно открыть загрузки или разобрать ошибки в плане."])
    elif remaining_decisions:
        lines.extend([
            "",
            f"Осталось разобрать сезонов: {remaining_decisions}.",
            "Уверенные задачи добавлены, а спорные или ненайденные сезоны я не буду выбирать наугад.",
        ])
    elif successes or pending_retries:
        lines.extend(["", "Можно открыть список загрузок."])
    return "\n".join(lines)


def _series_bulk_plan_from_context(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get("srch_series_bulk_plan")


def _series_bulk_pack_from_query(context: ContextTypes.DEFAULT_TYPE, data: str | None) -> tuple[int, dict] | None:
    plan = _series_bulk_plan_from_context(context)
    if plan is None:
        return None
    try:
        index = int(str(data or "").rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return None
    packs = _series_bulk_pack_candidates(plan)
    if not (0 <= index < len(packs)):
        return None
    return index, packs[index]


def _series_bulk_pack_covered_seasons(plan, season_range: tuple[int, int] | None) -> list[int]:
    if not season_range:
        return []
    start, end = season_range
    seasons: list[int] = []
    for season in getattr(plan, "seasons", ()):
        if season.status in {STATUS_ALREADY_IN_PLEX, STATUS_ALREADY_DOWNLOADING}:
            continue
        if start <= season.season <= end:
            seasons.append(season.season)
    return seasons


def _series_bulk_seasons_label(seasons: list[int]) -> str:
    if not seasons:
        return "Сезоны"
    ordered = sorted(seasons)
    if len(ordered) > 1 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"Сезоны {ordered[0]}-{ordered[-1]}"
    if len(ordered) == 1:
        return f"Сезон {ordered[0]}"
    return "Сезоны " + ", ".join(str(season) for season in ordered)


def _series_bulk_current_review(
    context: ContextTypes.DEFAULT_TYPE,
) -> SeasonPlan | None:
    plan = _series_bulk_plan_from_context(context)
    if plan is None:
        return None

    resolved = _series_bulk_resolved(context)
    failed = _series_bulk_failed(context)
    decision_seasons = _series_bulk_decision_seasons(plan, resolved, failed)
    if not decision_seasons:
        context.user_data.pop("srch_series_bulk_review_season", None)
        return None

    current = context.user_data.get("srch_series_bulk_review_season")
    try:
        current_season = int(current)
    except (TypeError, ValueError):
        current_season = None

    if current_season is not None:
        for season in decision_seasons:
            if season.season == current_season:
                return season

    season = decision_seasons[0]
    context.user_data["srch_series_bulk_review_season"] = season.season
    return season


def _series_bulk_replace_season(plan, updated: SeasonPlan):
    seasons = [
        updated if season.season == updated.season else season
        for season in getattr(plan, "seasons", ())
    ]
    if all(season.season != updated.season for season in seasons):
        seasons.append(updated)
    seasons.sort(key=lambda item: item.season)
    return type(plan)(
        series_title=plan.series_title,
        seasons=tuple(seasons),
        pack_candidates=plan.pack_candidates,
        verified_season_range=plan.verified_season_range,
    )


def _series_bulk_candidate_tuple_for_manual_choice(season_plan: SeasonPlan) -> tuple:
    candidates = []
    if season_plan.selected is not None:
        candidates.append(season_plan.selected)
    candidates.extend(season_plan.candidates)
    seen: set[tuple[str, str]] = set()
    unique = []
    for candidate in candidates:
        key = _series_bulk_result_key(candidate.result)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique[:5])


def _series_bulk_manual_season_from_soft_result(
    current: SeasonPlan,
    soft_result: SeasonPlan,
) -> SeasonPlan:
    if soft_result.status == STATUS_MISSING:
        return current if current.candidates else soft_result
    if soft_result.status == STATUS_PARTIAL:
        return soft_result
    candidates = _series_bulk_candidate_tuple_for_manual_choice(soft_result)
    if not candidates:
        return current
    return SeasonPlan(
        season=current.season,
        status=STATUS_NEEDS_DECISION,
        candidates=candidates,
        reasons=("soft search candidates",),
    )


def _series_bulk_review_text(
    season_plan: SeasonPlan,
    profile: SeriesBulkProfile | None,
    *,
    notice: str = "",
    failed_error: str | None = None,
    failed_candidate_index: int | None = None,
) -> str:
    lines: list[str] = []
    if notice:
        lines.extend([notice, ""])

    season = season_plan.season
    if failed_error:
        lines.extend([
            f"⚠️ Сезон {season} - не удалось добавить",
            f"Ошибка: {failed_error}",
            "",
            "Можно повторить тот же вариант или выбрать другую найденную раздачу.",
        ])
        candidate = None
        if failed_candidate_index is not None and 0 <= failed_candidate_index < len(season_plan.candidates):
            candidate = season_plan.candidates[failed_candidate_index]
        elif season_plan.selected is not None:
            candidate = season_plan.selected
        elif season_plan.candidates:
            candidate = season_plan.candidates[0]
        if candidate:
            lines.extend([
                "",
                f"Текущий вариант: {_series_bulk_candidate_confidence_label(candidate)}: {_series_bulk_candidate_label(candidate)}",
                _short_title(candidate.result, limit=110),
            ])
        failed_key = _series_bulk_result_key(candidate.result) if candidate is not None else None
        alternatives = [
            (index, candidate)
            for index, candidate in enumerate(season_plan.candidates[:3], start=1)
            if failed_key is None or _series_bulk_result_key(candidate.result) != failed_key
        ]
        if alternatives:
            lines.extend(["", "Другие варианты:"])
            for index, candidate in alternatives:
                lines.extend([
                    f"{index}. {_series_bulk_candidate_confidence_label(candidate)}: {_series_bulk_candidate_label(candidate)}",
                    _short_title(candidate.result, limit=110),
                ])
    elif season_plan.status == STATUS_PARTIAL:
        lines.append(f"⏳ Сезон {season} - найден неполный сезон")
        candidate = season_plan.candidates[0] if season_plan.candidates else None
        if candidate and candidate.episode_progress:
            cur, total = candidate.episode_progress
            lines.append(f"Сейчас доступно: {cur}/{total} серий.")
        lines.extend([
            "",
            "Автоматически не скачиваю: нужно выбрать, что делать с неполным сезоном.",
        ])
        if candidate:
            lines.extend([
                "",
                f"Кандидат: {_series_bulk_candidate_confidence_label(candidate)}: {_series_bulk_candidate_label(candidate)}",
                _short_title(candidate.result, limit=110),
            ])
    elif season_plan.status == STATUS_MISSING:
        lines.append(f"❌ Сезон {season} - не найдено")
        lines.extend([
            "",
            "По обычным правилам подходящей раздачи нет.",
            "Можно попробовать мягкий поиск: без жёстких требований к качеству, Original, субтитрам и озвучке.",
        ])
    else:
        reason = _series_bulk_plan_reason(season_plan)
        title = f"⚠️ Сезон {season} - нужно проверить"
        if reason:
            title += f": {reason}"
        lines.append(title)
        lines.extend([
            "",
            "Нашёл несколько близких вариантов и не выбираю автоматически.",
        ])

    profile_line = _series_bulk_profile_line(profile)
    if profile_line:
        lines.extend(["", f"Профиль поиска: {profile_line}"])

    if season_plan.status == STATUS_NEEDS_DECISION:
        lines.append("")
        for index, candidate in enumerate(season_plan.candidates[:3], start=1):
            result = candidate.result
            details = []
            explanation = _series_bulk_candidate_explanation(candidate)
            if explanation:
                details.append(f"причина: {explanation}")
            if result.get("seeders") is not None:
                details.append(f"сиды: {result.get('seeders')}")
            if result.get("size"):
                details.append(f"размер: {result.get('size')}")
            lines.extend([
                f"{index}. {_series_bulk_candidate_confidence_label(candidate)}: {_series_bulk_candidate_label(candidate)}",
                _short_title(result, limit=110),
                " · ".join(details),
            ])
            gpt_hint = str(getattr(candidate, "gpt_hint", "") or "").strip()
            if gpt_hint:
                lines.append(f"🤖 Подсказка: {gpt_hint}")

    return "\n".join(line for line in lines if line is not None)


async def _series_bulk_show_plan(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str = "",
) -> int:
    plan = _series_bulk_plan_from_context(context)
    profile = context.user_data.get("srch_series_bulk_profile")
    results = context.user_data.get("srch_series_bulk_results") or context.user_data.get("srch_results") or []
    warnings = tuple(context.user_data.get("srch_series_bulk_warnings") or ())
    try:
        result_count = int(context.user_data.get("srch_series_bulk_result_count") or len(results))
    except (TypeError, ValueError):
        result_count = len(results)
    if plan is None or profile is None:
        await query.edit_message_text("План потерян. Соберите его заново.")
        return ConversationHandler.END

    resolved = _series_bulk_resolved(context)
    failed = _series_bulk_failed(context)
    text = _series_bulk_plan_text(
        plan,
        profile,
        result_count=result_count,
        warnings=warnings,
        resolved=resolved,
        failed=failed,
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await query.edit_message_text(
        text,
        reply_markup=_series_bulk_plan_keyboard(plan, resolved, failed),
    )
    return SEARCH_RESULTS


async def series_bulk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /bulk from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return
    if update.message is None:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    jobs = _series_bulk_jobs_for_chat(chat_id)
    await update.message.reply_text(
        _series_bulk_jobs_text(jobs),
        reply_markup=_series_bulk_jobs_keyboard(jobs),
    )
    await _delete_command_message_safely(update, context, "bulk command")


async def search_series_bulk_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()

    chat_id = _chat_id_from_query(query)
    if chat_id is None and update.effective_chat:
        chat_id = update.effective_chat.id
    if not _is_allowed(update):
        await query.edit_message_text(
            "Нет доступа к этому плану.",
            reply_markup=_series_bulk_jobs_keyboard([]),
        )
        return ConversationHandler.END

    job_id = str(query.data or "").rsplit(":", 1)[-1]
    try:
        jobs = state_store.load_series_bulk_jobs()
    except Exception:
        logger.warning("Series bulk job open failed: id=%s", job_id, exc_info=True)
        jobs = {}
    job = jobs.get(job_id) if isinstance(jobs, dict) else None
    if not isinstance(job, dict) or not _series_bulk_job_matches_chat(job, chat_id):
        await query.edit_message_text(
            "План не найден или уже недоступен.",
            reply_markup=_series_bulk_jobs_keyboard([]),
        )
        return ConversationHandler.END
    if not _series_bulk_restore_context_from_job(context, job_id, job):
        await query.edit_message_text(
            "Не удалось восстановить этот план. Соберите его заново.",
            reply_markup=_series_bulk_jobs_keyboard([]),
        )
        return ConversationHandler.END

    return await _series_bulk_show_plan(
        query,
        context,
        notice="📚 Открыл сохранённый план.",
    )


async def _series_bulk_show_review_or_plan(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str = "",
) -> int:
    season_plan = _series_bulk_current_review(context)
    profile = context.user_data.get("srch_series_bulk_profile")
    if season_plan is None:
        return await _series_bulk_show_plan(query, context, notice=notice)
    failed_error = _series_bulk_failed(context).get(season_plan.season)
    failed_candidate_index = _series_bulk_failed_candidate_index(context, season_plan.season)
    await query.edit_message_text(
        _series_bulk_review_text(
            season_plan,
            profile,
            notice=notice,
            failed_error=failed_error,
            failed_candidate_index=failed_candidate_index,
        ),
        reply_markup=_series_bulk_review_keyboard(
            season_plan,
            failed_error,
            failed_candidate_index,
        ),
    )
    return SEARCH_RESULTS


async def _series_bulk_add_download(
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
    *,
    chat_id: int | None,
    meta_source: str,
) -> tuple[str, str]:
    entry = _pending_download_entry_from_result(
        result,
        chat_id=chat_id,
        subscribe=False,
        error="",
    )
    task_id, method = await _attempt_pending_download(entry)
    if task_id:
        _remember_task_owner(task_id, chat_id)
        _remember_task_meta(task_id, _build_task_meta_from_result(result, source=meta_source))
        _record_download_added_history(
            task_id,
            chat_id,
            result,
            method=method,
            meta_source=meta_source,
        )
    return task_id, method


def _series_bulk_partial_summary(action: str, saved_sub: dict | None = None) -> str:
    if action == "each":
        return "доступные серии добавлены, новые будут докачиваться"
    if action == "after":
        return "подписка: скачать сезон после финала"
    if action == "notify":
        return "подписка: уведомлять о новых сериях"
    if action == "final":
        return "подписка: сообщить о финале"
    if saved_sub:
        return f"подписка: {policies_summary_ru(saved_sub)}"
    return "решено"


async def search_series_bulk_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _series_bulk_show_review_or_plan(query, context)


async def search_series_bulk_pack_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    plan = _series_bulk_plan_from_context(context)
    if plan is None:
        return await _series_bulk_show_plan(query, context)
    packs = _series_bulk_pack_candidates(plan)
    if not packs:
        return await _series_bulk_show_plan(
            query,
            context,
            notice="Паки сезонов в этом плане не найдены.",
        )
    await query.edit_message_text(
        _series_bulk_pack_list_text(plan),
        reply_markup=_series_bulk_pack_list_keyboard(plan),
    )
    return SEARCH_RESULTS


async def search_series_bulk_pack_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    plan = _series_bulk_plan_from_context(context)
    selected = _series_bulk_pack_from_query(context, query.data)
    if plan is None or selected is None:
        return await _series_bulk_show_plan(
            query,
            context,
            notice="Не нашёл выбранный пак. Откройте список паков ещё раз.",
        )
    index, result = selected
    await query.edit_message_text(
        _series_bulk_pack_confirm_text(plan, result),
        reply_markup=_series_bulk_pack_confirm_keyboard(index),
    )
    return SEARCH_RESULTS


async def search_series_bulk_pack_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    plan = _series_bulk_plan_from_context(context)
    selected = _series_bulk_pack_from_query(context, query.data)
    if plan is None or selected is None:
        return await _series_bulk_show_plan(
            query,
            context,
            notice="Не нашёл выбранный пак. Откройте список паков ещё раз.",
        )
    _index, result = selected
    if not _series_bulk_start_action(context, "pack"):
        return await _series_bulk_show_plan(
            query,
            context,
            notice="⏳ Уже выполняю скачивание по этому плану. Дождитесь завершения текущего действия.",
        )

    try:
        disk_check = await asyncio.to_thread(_check_disk_space_for_download)
        if disk_check is not None and disk_check[0] == "block":
            await query.edit_message_text(
                disk_check[1],
                reply_markup=_series_bulk_pack_list_keyboard(plan),
                parse_mode="HTML",
            )
            return SEARCH_RESULTS

        await query.edit_message_text(
            f"⏳ Добавляю пак сезонов: {_series_bulk_pack_label(result)}",
            reply_markup=_series_bulk_wait_keyboard(),
        )
        chat_id = _chat_id_from_query(query)
        try:
            task_id, method = await _series_bulk_add_download(
                context,
                result,
                chat_id=chat_id,
                meta_source="series_bulk_pack",
            )
        except Exception as exc:
            logger.warning(
                "Series bulk pack download failed: title=%s error=%s",
                result.get("title"),
                exc,
                exc_info=True,
            )
            return await _series_bulk_show_plan(
                query,
                context,
                notice=f"❌ Пак не удалось добавить: {_format_download_error(exc)}",
            )

        season_range = _series_bulk_pack_range(result)
        covered_seasons = _series_bulk_pack_covered_seasons(plan, season_range)
        summary = f"скачан паком: {task_id or method}"
        for season in covered_seasons:
            _series_bulk_mark_resolved(context, season, summary)
            _series_bulk_clear_failed(context, season)
            _series_bulk_record_job_season(
                context,
                season,
                "pack_downloaded",
                task_id=task_id or "",
                method=method,
                summary=summary,
                result=result,
            )
        _series_bulk_record_job_pack(
            context,
            result=result,
            task_id=task_id,
            method=method,
            season_range=season_range,
        )
        _series_bulk_set_job_status(context, "pack_downloaded")

        if covered_seasons:
            season_text = f"{_series_bulk_seasons_label(covered_seasons)} пометил как скачанные паком."
        else:
            season_text = "План по сезонам не помечал: диапазон пака не распознан."
        return await _series_bulk_show_plan(
            query,
            context,
            notice=f"✅ Пак добавлен: {task_id or method}.\n{season_text}",
        )
    finally:
        _series_bulk_finish_action(context, "pack")


async def search_series_bulk_candidate_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    season_plan = _series_bulk_current_review(context)
    if season_plan is None:
        return await _series_bulk_show_plan(query, context)
    try:
        candidate_index = int(query.data.rsplit(":", 1)[1])
        candidate = season_plan.candidates[candidate_index]
    except (ValueError, IndexError):
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice="Не нашёл выбранный вариант. Откройте разбор ещё раз.",
        )

    disk_check = await asyncio.to_thread(_check_disk_space_for_download)
    if disk_check is not None and disk_check[0] == "block":
        await query.edit_message_text(
            disk_check[1],
            reply_markup=_series_bulk_review_keyboard(
                season_plan,
                _series_bulk_failed(context).get(season_plan.season),
                _series_bulk_failed_candidate_index(context, season_plan.season),
            ),
            parse_mode="HTML",
        )
        return SEARCH_RESULTS

    await query.edit_message_text(
        f"⏳ Добавляю сезон {season_plan.season}: {_series_bulk_candidate_label(candidate)}",
        reply_markup=_series_bulk_wait_keyboard(),
    )
    try:
        chat_id = _chat_id_from_query(query)
        task_id, method = await _series_bulk_add_download(
            context,
            candidate.result,
            chat_id=chat_id,
            meta_source="series_bulk_manual",
        )
    except Exception as exc:
        logger.warning(
            "Series bulk manual download failed: season=%s title=%s error=%s",
            season_plan.season,
            candidate.result.get("title"),
            exc,
            exc_info=True,
        )
        error = _format_download_error(exc)
        _series_bulk_mark_failed(context, season_plan.season, error)
        _series_bulk_mark_failed_candidate(context, season_plan.season, candidate_index)
        _series_bulk_record_job_season(
            context,
            season_plan.season,
            "failed",
            error=error,
            result=candidate.result,
        )
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice=f"❌ Сезон {season_plan.season}: {error}",
        )

    summary = f"скачан вручную: {task_id or method}"
    _series_bulk_clear_failed(context, season_plan.season)
    _series_bulk_mark_resolved(
        context,
        season_plan.season,
        summary,
    )
    _series_bulk_record_job_season(
        context,
        season_plan.season,
        "downloaded",
        task_id=task_id or "",
        method=method,
        summary=summary,
        result=candidate.result,
    )
    return await _series_bulk_show_review_or_plan(
        query,
        context,
        notice=f"✅ Сезон {season_plan.season}: добавил задачу {task_id or method}.",
    )


async def search_series_bulk_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    season_plan = _series_bulk_current_review(context)
    if season_plan is None:
        return await _series_bulk_show_plan(query, context)
    failed_error = _series_bulk_failed(context).get(season_plan.season)
    if not failed_error:
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice="Для этого сезона больше нет ошибки. Откройте план ещё раз.",
        )
    candidate, candidate_index = _series_bulk_failed_candidate(context, season_plan)
    if candidate is None:
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice="Не нашёл раздачу для повтора. Можно пропустить сезон или пересобрать план позже.",
        )

    disk_check = await asyncio.to_thread(_check_disk_space_for_download)
    if disk_check is not None and disk_check[0] == "block":
        await query.edit_message_text(
            disk_check[1],
            reply_markup=_series_bulk_review_keyboard(
                season_plan,
                failed_error,
                _series_bulk_failed_candidate_index(context, season_plan.season),
            ),
            parse_mode="HTML",
        )
        return SEARCH_RESULTS

    await query.edit_message_text(
        f"⏳ Повторяю сезон {season_plan.season}: {_series_bulk_candidate_label(candidate)}",
        reply_markup=_series_bulk_wait_keyboard(),
    )
    try:
        chat_id = _chat_id_from_query(query)
        task_id, method = await _series_bulk_add_download(
            context,
            candidate.result,
            chat_id=chat_id,
            meta_source="series_bulk_retry",
        )
    except Exception as exc:
        logger.warning(
            "Series bulk retry failed: season=%s title=%s error=%s",
            season_plan.season,
            candidate.result.get("title"),
            exc,
            exc_info=True,
        )
        error = _format_download_error(exc)
        _series_bulk_mark_failed(context, season_plan.season, error)
        _series_bulk_mark_failed_candidate(context, season_plan.season, candidate_index or 0)
        _series_bulk_record_job_season(
            context,
            season_plan.season,
            "failed",
            error=error,
            result=candidate.result,
        )
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice=f"❌ Сезон {season_plan.season}: {error}",
        )

    summary = f"скачан после повтора: {task_id or method}"
    _series_bulk_clear_failed(context, season_plan.season)
    _series_bulk_mark_resolved(context, season_plan.season, summary)
    _series_bulk_record_job_season(
        context,
        season_plan.season,
        "downloaded",
        task_id=task_id or "",
        method=method,
        summary=summary,
        result=candidate.result,
    )
    return await _series_bulk_show_review_or_plan(
        query,
        context,
        notice=f"✅ Сезон {season_plan.season}: добавил задачу {task_id or method}.",
    )


async def search_series_bulk_soft_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    plan = _series_bulk_plan_from_context(context)
    season_plan = _series_bulk_current_review(context)
    profile = context.user_data.get("srch_series_bulk_profile")
    if plan is None or season_plan is None:
        return await _series_bulk_show_plan(query, context)

    await query.edit_message_text(
        f"⏳ Ищу шире: сезон {season_plan.season}",
        reply_markup=_series_bulk_wait_keyboard(),
    )
    found_results: list[dict] = []
    search_warnings: list[str] = []
    for search_query in _series_bulk_soft_search_queries(plan.series_title, season_plan.season):
        results, warnings = await _series_bulk_search_once(context, search_query)
        found_results = _series_bulk_merge_results(found_results, results)
        search_warnings.extend(warnings)

    existing_results = (
        context.user_data.get("srch_series_bulk_results")
        or context.user_data.get("srch_results")
        or []
    )
    combined_results = _series_bulk_merge_results(existing_results, found_results)
    new_result_count = max(0, len(combined_results) - len(existing_results))
    soft_plan = build_series_bulk_plan(
        series_title=plan.series_title,
        seasons=[season_plan.season],
        results=combined_results,
        profile=_series_bulk_soft_profile(profile),
        verified_season_range=getattr(plan, "verified_season_range", True),
    )
    updated = _series_bulk_manual_season_from_soft_result(season_plan, soft_plan.seasons[0])
    updated_plan = _series_bulk_replace_season(plan, updated)
    updated_plan = await _gpt_enrich_series_bulk_plan(updated_plan, _series_bulk_soft_profile(profile), max_seasons=1)
    context.user_data["srch_series_bulk_plan"] = updated_plan
    context.user_data["srch_series_bulk_results"] = combined_results
    if search_warnings:
        existing_warnings = tuple(context.user_data.get("srch_series_bulk_warnings") or ())
        context.user_data["srch_series_bulk_warnings"] = tuple(
            dict.fromkeys((*existing_warnings, *search_warnings))
        )
    context.user_data["srch_series_bulk_review_season"] = season_plan.season
    if updated.status == STATUS_MISSING:
        notice = f"🔄 Сезон {season_plan.season}: новых вариантов не нашёл."
    elif new_result_count == 0:
        notice = f"🔄 Сезон {season_plan.season}: новых вариантов не нашёл, оставил текущие."
    else:
        notice = f"🔄 Сезон {season_plan.season}: нашёл варианты шире, выберите вручную."
    return await _series_bulk_show_review_or_plan(query, context, notice=notice)


async def search_series_bulk_partial_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    season_plan = _series_bulk_current_review(context)
    if season_plan is None:
        return await _series_bulk_show_plan(query, context)
    if season_plan.status != STATUS_PARTIAL or not season_plan.candidates:
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice="Этот сезон уже не выглядит неполным. Откройте план ещё раз.",
        )

    action = query.data.rsplit(":", 1)[-1]
    candidate = season_plan.candidates[0]
    result = candidate.result
    chat_id = _chat_id_from_query(query)

    if action == "download":
        disk_check = await asyncio.to_thread(_check_disk_space_for_download)
        if disk_check is not None and disk_check[0] == "block":
            await query.edit_message_text(
                disk_check[1],
                reply_markup=_series_bulk_review_keyboard(season_plan),
                parse_mode="HTML",
            )
            return SEARCH_RESULTS
        await query.edit_message_text(
            f"⏳ Добавляю доступные серии сезона {season_plan.season}",
            reply_markup=_series_bulk_wait_keyboard(),
        )
        try:
            task_id, method = await _series_bulk_add_download(
                context,
                result,
                chat_id=chat_id,
                meta_source="series_bulk_partial",
            )
        except Exception as exc:
            logger.warning(
                "Series bulk partial download failed: season=%s title=%s error=%s",
                season_plan.season,
                result.get("title"),
                exc,
                exc_info=True,
            )
            error = _format_download_error(exc)
            _series_bulk_record_job_season(
                context,
                season_plan.season,
                "failed",
                error=error,
                result=result,
            )
            return await _series_bulk_show_review_or_plan(
                query,
                context,
                notice=f"❌ Сезон {season_plan.season}: {error}",
            )
        summary = f"доступные серии добавлены: {task_id or method}"
        _series_bulk_mark_resolved(
            context,
            season_plan.season,
            summary,
        )
        _series_bulk_clear_failed(context, season_plan.season)
        _series_bulk_record_job_season(
            context,
            season_plan.season,
            "partial_downloaded",
            task_id=task_id or "",
            method=method,
            summary=summary,
            result=result,
        )
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice=f"✅ Сезон {season_plan.season}: доступные серии добавлены.",
        )

    if action not in _SUB_PRESETS:
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice="Неизвестное действие для неполного сезона.",
        )

    notify_policy, download_policy, download_now = _SUB_PRESETS[action]
    task_id = ""
    method = ""
    if download_now:
        disk_check = await asyncio.to_thread(_check_disk_space_for_download)
        if disk_check is not None and disk_check[0] == "block":
            await query.edit_message_text(
                disk_check[1],
                reply_markup=_series_bulk_review_keyboard(season_plan),
                parse_mode="HTML",
            )
            return SEARCH_RESULTS
        await query.edit_message_text(
            f"⏳ Добавляю доступные серии и подписку сезона {season_plan.season}",
            reply_markup=_series_bulk_wait_keyboard(),
        )
        try:
            task_id, method = await _series_bulk_add_download(
                context,
                result,
                chat_id=chat_id,
                meta_source="series_bulk_partial",
            )
        except Exception as exc:
            logger.warning(
                "Series bulk partial download+subscription failed: season=%s title=%s error=%s",
                season_plan.season,
                result.get("title"),
                exc,
                exc_info=True,
            )
            error = _format_download_error(exc)
            _series_bulk_record_job_season(
                context,
                season_plan.season,
                "failed",
                error=error,
                result=result,
            )
            return await _series_bulk_show_review_or_plan(
                query,
                context,
                notice=f"❌ Сезон {season_plan.season}: {error}",
            )

    try:
        _saved_key, saved_sub = _save_subscription_for_result(
            context,
            result,
            chat_id=chat_id if isinstance(chat_id, int) else None,
            notify_policy=notify_policy,
            download_policy=download_policy,
            seen_results=(
                context.user_data.get("srch_series_bulk_results")
                or context.user_data.get("srch_results", [])
            ),
        )
    except RuntimeError as exc:
        user_error = _subscription_save_user_error_text(downloaded=bool(task_id))
        logger.info("Series bulk subscription save failed: %s", exc)
        if task_id:
            summary = user_error
            _series_bulk_mark_resolved(
                context,
                season_plan.season,
                summary,
            )
            _series_bulk_clear_failed(context, season_plan.season)
            _series_bulk_record_job_season(
                context,
                season_plan.season,
                "partial_downloaded_subscription_failed",
                task_id=task_id,
                method=method,
                error=user_error,
                summary=summary,
                result=result,
            )
            return await _series_bulk_show_review_or_plan(
                query,
                context,
                notice=f"⚠️ Сезон {season_plan.season}: {user_error}.",
            )
        _series_bulk_record_job_season(
            context,
            season_plan.season,
            "failed",
            error=user_error,
            result=result,
        )
        return await _series_bulk_show_review_or_plan(
            query,
            context,
            notice=f"❌ Сезон {season_plan.season}: {user_error}.",
        )

    summary = _series_bulk_partial_summary(action, saved_sub)
    _series_bulk_mark_resolved(context, season_plan.season, summary)
    _series_bulk_clear_failed(context, season_plan.season)
    _series_bulk_record_job_season(
        context,
        season_plan.season,
        "downloaded_and_subscribed" if download_now else "subscribed",
        task_id=task_id or None,
        method=method or None,
        summary=summary,
        result=result,
        subscription=saved_sub,
    )
    return await _series_bulk_show_review_or_plan(
        query,
        context,
        notice=f"✅ Сезон {season_plan.season}: {summary}.",
    )


async def search_series_bulk_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    season_plan = _series_bulk_current_review(context)
    if season_plan is None:
        return await _series_bulk_show_plan(query, context)
    _series_bulk_mark_resolved(context, season_plan.season, "пропущен")
    _series_bulk_clear_failed(context, season_plan.season)
    _series_bulk_record_job_season(
        context,
        season_plan.season,
        "skipped",
        summary="пропущен",
    )
    return await _series_bulk_show_review_or_plan(
        query,
        context,
        notice=f"⏭ Сезон {season_plan.season}: пропущен.",
    )


async def search_series_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    plan = _series_bulk_plan_from_context(context)
    resolved = _series_bulk_resolved(context)
    failed = _series_bulk_failed(context)
    ready_count = len(_series_bulk_ready_seasons(plan, resolved, failed)) if plan is not None else 0
    if not ready_count:
        await query.edit_message_text(
            _series_bulk_confirm_text(plan, resolved, failed) if plan is not None else "План потерян. Соберите его заново.",
            reply_markup=_series_bulk_plan_keyboard(plan, resolved, failed),
        )
        return SEARCH_RESULTS

    await query.edit_message_text(
        _series_bulk_confirm_text(plan, resolved, failed),
        reply_markup=_series_bulk_confirm_keyboard(ready_count),
    )
    return SEARCH_RESULTS


async def search_series_bulk_back_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _series_bulk_show_plan(query, context)


async def search_series_bulk_rebuild(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _series_bulk_set_job_status(context, "replaced")
    profile = context.user_data.get("srch_series_bulk_profile")
    if not isinstance(profile, SeriesBulkProfile):
        profile = _series_bulk_profile_from_context(context)
    if isinstance(profile, SeriesBulkProfile):
        context.user_data["srch_series_bulk_profile_draft"] = profile
    for key in (
        "srch_series_bulk_plan",
        "srch_series_bulk_results",
        "srch_series_bulk_result_count",
        "srch_series_bulk_warnings",
        "srch_series_bulk_review_season",
        "srch_series_bulk_job_id",
    ):
        context.user_data.pop(key, None)
    context.user_data["srch_series_bulk_resolved"] = {}
    context.user_data["srch_series_bulk_failed"] = {}
    context.user_data["srch_series_bulk_failed_candidates"] = {}
    return await _series_bulk_show_profile(query, context)


async def search_series_bulk_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    plan = _series_bulk_plan_from_context(context)
    resolved = _series_bulk_resolved(context)
    failed = _series_bulk_failed(context)
    ready = _series_bulk_ready_seasons(plan, resolved, failed) if plan is not None else []
    if not ready:
        await query.edit_message_text(
            _series_bulk_confirm_text(plan, resolved, failed) if plan is not None else (
                "📚 Уверенных сезонов для автоскачивания нет.\n\n"
                "Я не буду добавлять раздачи наугад. Спорные, неполные или ненайденные сезоны нужно разобрать вручную."
            ),
            reply_markup=_series_bulk_plan_keyboard(plan, resolved, failed),
        )
        return SEARCH_RESULTS

    if not _series_bulk_start_action(context, "batch"):
        return await _series_bulk_show_plan(
            query,
            context,
            notice="⏳ Уже выполняю скачивание по этому плану. Дождитесь завершения текущего действия.",
        )

    try:
        disk_check = await asyncio.to_thread(_check_disk_space_for_download)
        if disk_check is not None and disk_check[0] == "block":
            await query.edit_message_text(
                disk_check[1],
                reply_markup=_search_error_keyboard(),
                parse_mode="HTML",
            )
            return SEARCH_RESULTS

        chat_id = _chat_id_from_query(query)
        successes: list[dict] = []
        failures: list[dict] = []
        pending_retries: list[dict] = []
        total = len(ready)
        _series_bulk_set_job_status(context, "batch_running")
        await query.edit_message_text(
            f"⏳ Добавляю уверенные сезоны: 0/{total}",
            reply_markup=_series_bulk_wait_keyboard(),
        )
        for position, season in enumerate(ready, start=1):
            assert season.selected is not None
            result = season.selected.result
            await query.edit_message_text(
                f"⏳ Добавляю уверенные сезоны: {position}/{total}\n"
                f"Сезон {season.season}: {_series_bulk_candidate_label(season.selected)}",
                reply_markup=_series_bulk_wait_keyboard(),
            )
            try:
                entry = _pending_download_entry_from_result(
                    result,
                    chat_id=chat_id,
                    subscribe=False,
                    error="",
                )
                task_id, method = await _attempt_pending_download(entry)
                if task_id:
                    _remember_task_owner(task_id, chat_id)
                    _remember_task_meta(task_id, _build_task_meta_from_result(result, source="series_bulk"))
                    _record_download_added_history(
                        task_id,
                        chat_id,
                        result,
                        method=method,
                        meta_source="series_bulk",
                    )
                successes.append({
                    "season": season.season,
                    "task_id": task_id or "-",
                    "method": method,
                })
                _series_bulk_mark_resolved(
                    context,
                    season.season,
                    f"скачан: {task_id or method}",
                )
                _series_bulk_clear_failed(context, season.season)
                _series_bulk_record_job_season(
                    context,
                    season.season,
                    "downloaded",
                    task_id=task_id or "",
                    method=method,
                    result=result,
                )
            except Exception as exc:
                logger.warning(
                    "Series bulk download failed: season=%s title=%s error=%s",
                    season.season,
                    (result or {}).get("title"),
                    exc,
                    exc_info=True,
                )
                error = _format_download_error(exc)
                if _pending_downloads_enabled() and _is_pending_retryable_download_error(exc):
                    job_id = str(context.user_data.get("srch_series_bulk_job_id") or "")
                    entry_id, _entry = _queue_pending_download_from_result(
                        result,
                        chat_id=chat_id,
                        subscribe=False,
                        error=error,
                        series_bulk={
                            "job_id": job_id,
                            "season": season.season,
                        } if job_id else None,
                    )
                    pending_retries.append({
                        "season": season.season,
                        "entry_id": entry_id,
                        "error": error,
                    })
                    summary = f"в очереди на повтор: {entry_id}"
                    _series_bulk_mark_resolved(context, season.season, summary)
                    _series_bulk_clear_failed(context, season.season)
                    _series_bulk_record_job_season(
                        context,
                        season.season,
                        "pending_retry",
                        error=error,
                        summary=summary,
                        result=result,
                        pending_entry_id=entry_id,
                    )
                    continue
                failures.append({
                    "season": season.season,
                    "error": error,
                })
                _series_bulk_mark_failed(context, season.season, error)
                _series_bulk_mark_failed_candidate(
                    context,
                    season.season,
                    _series_bulk_candidate_index(season, season.selected),
                )
                _series_bulk_record_job_season(
                    context,
                    season.season,
                    "failed",
                    error=error,
                    result=result,
                )

        final_resolved = _series_bulk_resolved(context)
        final_failed = _series_bulk_failed(context)
        remaining_decisions = len(_series_bulk_decision_seasons(plan, final_resolved, final_failed))
        if failures and (successes or pending_retries):
            _series_bulk_set_job_status(context, "batch_completed_with_errors")
        elif pending_retries:
            _series_bulk_set_job_status(context, "batch_completed_with_pending")
        elif failures:
            _series_bulk_set_job_status(context, "batch_failed")
        elif remaining_decisions:
            _series_bulk_set_job_status(context, "batch_completed_with_decisions")
        else:
            _series_bulk_set_job_status(context, "batch_completed")
        await query.edit_message_text(
            _series_bulk_done_text(successes, failures, pending_retries, remaining_decisions),
            reply_markup=_series_bulk_done_keyboard(
                bool(successes or pending_retries),
                bool(failures),
                remaining_decisions,
            ),
        )
        return SEARCH_RESULTS if remaining_decisions else ConversationHandler.END
    finally:
        _series_bulk_finish_action(context, "batch")


async def search_series_bulk_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    result = results[index]
    series_query = _extract_series_base_query(str(result.get("title") or ""))
    if not series_query:
        await query.edit_message_text(
            "Не смог уверенно определить сериал по названию раздачи.",
            reply_markup=_series_bulk_error_keyboard(index),
        )
        return SEARCH_RESULTS

    profile = _series_bulk_profile_from_result(context, result)
    base_quality = profile.quality if profile.quality and profile.quality != "any" else "1080p"
    _clear_search_intent(context)
    context.user_data["srch_series_bulk_index"] = index
    context.user_data["srch_series_bulk_profile_draft"] = profile
    context.user_data["srch_series_bulk_base_quality"] = base_quality
    return await _series_bulk_show_profile(query, context)


async def search_series_bulk_build_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    build_token = _series_bulk_start_build(context)
    try:
        index = int(context.user_data.get("srch_series_bulk_index"))
    except (TypeError, ValueError):
        _series_bulk_finish_build(context, build_token)
        await query.edit_message_text(
            "План потерян. Вернитесь к результатам и откройте его заново.",
            reply_markup=_search_error_keyboard(),
        )
        return SEARCH_RESULTS

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        _series_bulk_finish_build(context, build_token)
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    result = results[index]
    series_query = _extract_series_base_query(str(result.get("title") or ""))
    if not series_query:
        _series_bulk_finish_build(context, build_token)
        await query.edit_message_text(
            "Не смог уверенно определить сериал по названию раздачи.",
            reply_markup=_series_bulk_error_keyboard(index),
        )
        return SEARCH_RESULTS

    _cancel_didmean_prefetch(context)
    chat_id = _chat_id_from_query(query)
    animation_msg = await _series_bulk_send_animation(context, chat_id)
    long_notice_task: asyncio.Task | None = None
    try:
        await _series_bulk_edit_wait(query, context, series_query, "seasons")
        long_notice_task = asyncio.create_task(
            _series_bulk_long_notice_loop(query, context, series_query, build_token)
        )
        seasons, verified = await _series_bulk_known_seasons(series_query, results)
        if _series_bulk_build_cancelled(context, build_token):
            return ConversationHandler.END
        clicked_season = _extract_season_from_query(str(result.get("title") or ""))
        if clicked_season and clicked_season not in seasons:
            seasons = sorted({*seasons, clicked_season})
        profile = _series_bulk_profile_from_context(context)
        if profile is None:
            profile = _series_bulk_profile_from_result(context, result)

        await _series_bulk_edit_wait(query, context, series_query, "plex")
        plex_seasons = await _get_plex_seasons_for_series(series_query)
        if _series_bulk_build_cancelled(context, build_token):
            return ConversationHandler.END

        await _series_bulk_edit_wait(query, context, series_query, "downloads")
        downloading_seasons = await _series_bulk_downloading_seasons(series_query)
        if _series_bulk_build_cancelled(context, build_token):
            return ConversationHandler.END

        await _series_bulk_edit_wait(query, context, series_query, "search")
        wide_results, wide_warnings = await _series_bulk_search_once(context, series_query)
        search_warnings = list(wide_warnings)
        wide_hit_fetch_limit = _series_bulk_has_fetch_limit_warning(wide_warnings)
        if _series_bulk_build_cancelled(context, build_token):
            return ConversationHandler.END
        combined_results = _series_bulk_merge_results(results, wide_results)
        if not verified:
            seasons = sorted({*seasons, *_seasons_available_in_results(combined_results)})
        if not seasons:
            _series_bulk_finish_build(context, build_token)
            await query.edit_message_text(
                "Не нашёл ни одного сезона для плана.",
                reply_markup=_series_bulk_error_keyboard(index),
            )
            return SEARCH_RESULTS

        preliminary_plan = build_series_bulk_plan(
            series_title=series_query,
            seasons=seasons,
            results=combined_results,
            profile=profile,
            plex_seasons=plex_seasons,
            downloading_seasons=downloading_seasons,
            verified_season_range=verified,
        )
        targeted_seasons = _series_bulk_seasons_for_targeted_search(
            preliminary_plan,
            fetch_limit_supplement=wide_hit_fetch_limit,
        )
        targeted_hit_fetch_limit = False
        if targeted_seasons:
            await _series_bulk_edit_wait(query, context, series_query, "targeted")
            for season in targeted_seasons:
                season_query = _normalize_season_in_query(f"{series_query} Сезон {season}")
                season_results, season_warnings = await _series_bulk_search_once(context, season_query)
                if _series_bulk_build_cancelled(context, build_token):
                    return ConversationHandler.END
                if _series_bulk_has_fetch_limit_warning(season_warnings):
                    targeted_hit_fetch_limit = True
                search_warnings.extend(season_warnings)
                combined_results = _series_bulk_merge_results(combined_results, season_results)
        if wide_hit_fetch_limit and verified and not targeted_hit_fetch_limit:
            search_warnings = _series_bulk_without_fetch_limit_warnings(search_warnings)

        await _series_bulk_edit_wait(query, context, series_query, "plan")
        if _series_bulk_build_cancelled(context, build_token):
            return ConversationHandler.END
        plan = build_series_bulk_plan(
            series_title=series_query,
            seasons=seasons,
            results=combined_results,
            profile=profile,
            plex_seasons=plex_seasons,
            downloading_seasons=downloading_seasons,
            verified_season_range=verified,
        )
        plan = await _gpt_enrich_series_bulk_plan(plan, profile)
        context.user_data["srch_series_bulk_plan"] = plan
        context.user_data["srch_series_bulk_profile"] = profile
        context.user_data["srch_series_bulk_results"] = combined_results
        context.user_data["srch_series_bulk_warnings"] = tuple(search_warnings)
        context.user_data["srch_series_bulk_resolved"] = {}
        context.user_data["srch_series_bulk_failed"] = {}
        context.user_data["srch_series_bulk_failed_candidates"] = {}
        context.user_data.pop("srch_series_bulk_review_season", None)
        context.user_data.pop("srch_series_bulk_job_id", None)
        if _series_bulk_terminal_no_action_plan(plan):
            await query.edit_message_text(
                _series_bulk_no_action_text(
                    plan,
                    result_count=len(combined_results),
                    warnings=tuple(search_warnings),
                ),
                reply_markup=_series_bulk_done_keyboard(False),
            )
            return ConversationHandler.END
        _series_bulk_create_job(
            context,
            plan=plan,
            profile=profile,
            results=combined_results,
            warnings=tuple(search_warnings),
            source_result=result,
            chat_id=chat_id,
        )
        await query.edit_message_text(
            _series_bulk_plan_text(
                plan,
                profile,
                result_count=len(combined_results),
                warnings=tuple(search_warnings),
            ),
            reply_markup=_series_bulk_plan_keyboard(plan, {}),
        )
        return SEARCH_RESULTS
    except Exception as exc:
        logger.exception("Series bulk plan failed: %s", exc)
        _series_bulk_finish_build(context, build_token)
        await query.edit_message_text(
            "Не удалось собрать план сезонов. Можно попробовать ещё раз.",
            reply_markup=_series_bulk_error_keyboard(index),
        )
        return SEARCH_RESULTS
    finally:
        await _series_bulk_stop_long_notice(long_notice_task)
        context.user_data.pop("srch_series_bulk_wait_stage", None)
        context.user_data.pop("srch_series_bulk_long_notice", None)
        _series_bulk_finish_build(context, build_token)
        await _series_bulk_delete_animation(animation_msg)


def _subscribe_picker_text(result: dict) -> str:
    title = html_module.escape(str(result.get("title") or "")[:120])
    ep_str = html_module.escape(str(result.get("ep_str") or ""))
    progress = f"\n\nСейчас доступно: <b>{ep_str}</b>" if ep_str else ""
    return (
        f"🎬 {title}{progress}\n\n"
        "Когда присылать уведомление?"
    )


def _subscribe_picker_keyboard(index: int) -> InlineKeyboardMarkup:
    prefix = SEARCH_CALLBACK_PREFIX
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Уведомлять о новых сериях",
                              callback_data=f"{prefix}:sub_preset:{index}:notify")],
        [InlineKeyboardButton("🎯 Сообщить, когда сезон завершится",
                              callback_data=f"{prefix}:sub_preset:{index}:final")],
        [InlineKeyboardButton("⬅️ К результатам",
                              callback_data=f"{prefix}:sub_back_results:0")],
        [InlineKeyboardButton("❌ Отмена",
                              callback_data=f"{prefix}:cancel")],
    ])


async def search_download_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User tapped «⬇️ N» on a series result — show download choices."""
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    result = results[index]
    await query.edit_message_text(
        _download_picker_text(result),
        reply_markup=_download_picker_keyboard(
            index,
            partial=bool(result.get("partial")),
            show_bulk_plan=bool(result.get("series") or _extract_series_base_query(result.get("title", ""))),
        ),
        parse_mode="HTML",
    )
    return SEARCH_RESULTS


async def search_subscribe_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User tapped «🔔 N» — show notification-only choices."""
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["srch_sub_index"] = index
    await query.edit_message_text(
        _subscribe_picker_text(results[index]),
        reply_markup=_subscribe_picker_keyboard(index),
        parse_mode="HTML",
    )
    return SEARCH_RESULTS


async def search_subscribe_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Commit one picker option."""
    query = update.callback_query
    await query.answer()
    try:
        _prefix, _action, idx_str, code = query.data.rsplit(":", 3)
        index = int(idx_str)
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    pair = _SUB_PRESETS.get(code)
    if pair is None:
        await query.edit_message_text(
            "Неизвестный пресет подписки.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    notify_policy, download_policy, download_current_now = pair
    if not download_current_now:
        return await _create_subscription_only(
            query, context, index,
            notify_policy=notify_policy,
            download_policy=download_policy,
        )
    return await _download_and_add(
        query, context, index,
        subscribe=True,
        notify_policy=notify_policy,
        download_policy=download_policy,
    )


# ─── Advanced (2-step) menu ──────────────────────────────────────────────

def _advanced_notify_text(result: dict) -> str:
    title = html_module.escape(str(result.get("title") or "")[:120])
    return (
        f"🎬 {title}\n\n"
        "<b>Шаг 1/2.</b> Когда отправлять уведомления в Telegram?"
    )


def _advanced_notify_keyboard(index: int) -> InlineKeyboardMarkup:
    prefix = SEARCH_CALLBACK_PREFIX
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 О каждой новой серии",
                              callback_data=f"{prefix}:sub_set_notify:{index}:{NOTIFY_EACH_UPDATE}")],
        [InlineKeyboardButton("🎯 Только когда сезон завершится",
                              callback_data=f"{prefix}:sub_set_notify:{index}:{NOTIFY_FINAL_ONLY}")],
        [InlineKeyboardButton("🔇 Не уведомлять",
                              callback_data=f"{prefix}:sub_set_notify:{index}:{NOTIFY_SILENT}")],
        [InlineKeyboardButton("⬅️ Назад к пресетам",
                              callback_data=f"{prefix}:sub_pick:{index}")],
        [InlineKeyboardButton("❌ Отмена",
                              callback_data=f"{prefix}:cancel")],
    ])


def _advanced_download_text(result: dict, notify_policy: str) -> str:
    title = html_module.escape(str(result.get("title") or "")[:120])
    notify_label = {
        NOTIFY_EACH_UPDATE: "🔔 О каждой новой серии",
        NOTIFY_FINAL_ONLY:  "🎯 Только когда сезон завершится",
        NOTIFY_SILENT:      "🔇 Не уведомлять",
    }.get(notify_policy, notify_policy)
    return (
        f"🎬 {title}\n\n"
        f"Уведомления: <b>{notify_label}</b>\n\n"
        "<b>Шаг 2/2.</b> Когда скачивать?"
    )


def _advanced_download_keyboard(index: int, notify_policy: str = NOTIFY_EACH_UPDATE) -> InlineKeyboardMarkup:
    prefix = SEARCH_CALLBACK_PREFIX
    rows = [
        [InlineKeyboardButton("⬇️ Новые серии по мере выхода",
                              callback_data=f"{prefix}:sub_set_download:{index}:{DOWNLOAD_AUTO_EACH_UPDATE}")],
        [InlineKeyboardButton("📦 Когда сезон завершится",
                              callback_data=f"{prefix}:sub_set_download:{index}:{DOWNLOAD_ONLY_WHEN_COMPLETE}")],
    ]
    if notify_policy != NOTIFY_SILENT:
        rows.append([
            InlineKeyboardButton("⏸ Не скачивать автоматически",
                                 callback_data=f"{prefix}:sub_set_download:{index}:{DOWNLOAD_NOTIFY_ONLY}")
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад",
                                      callback_data=f"{prefix}:sub_advanced:{index}")])
    rows.append([InlineKeyboardButton("❌ Отмена",
                                      callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(rows)


async def search_subscribe_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User tapped «⚙️ Настроить вручную» — enter step 1 of advanced menu."""
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["srch_sub_index"] = index
    await query.edit_message_text(
        _advanced_notify_text(results[index]),
        reply_markup=_advanced_notify_keyboard(index),
        parse_mode="HTML",
    )
    return SEARCH_RESULTS


async def search_subscribe_set_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1 selection → save notify_policy, show step 2 (download policy)."""
    query = update.callback_query
    await query.answer()
    try:
        _prefix, _action, idx_str, notify_policy = query.data.rsplit(":", 3)
        index = int(idx_str)
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    if notify_policy not in VALID_NOTIFY_POLICIES:
        await query.edit_message_text(
            "Неизвестный режим уведомлений.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    results = context.user_data.get("srch_results", [])
    if not (0 <= index < len(results)):
        await query.edit_message_text(
            "Результат недоступен.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    # Stash the step-1 choice so step 2 can combine them on commit.
    context.user_data["srch_sub_notify_policy"] = notify_policy
    await query.edit_message_text(
        _advanced_download_text(results[index], notify_policy),
        reply_markup=_advanced_download_keyboard(index, notify_policy),
        parse_mode="HTML",
    )
    return SEARCH_RESULTS


async def search_subscribe_set_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2 selection → final commit: subscribe with the chosen pair."""
    query = update.callback_query
    await query.answer()
    try:
        _prefix, _action, idx_str, download_policy = query.data.rsplit(":", 3)
        index = int(idx_str)
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Ошибка при разборе запроса.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    if download_policy not in VALID_DOWNLOAD_POLICIES:
        await query.edit_message_text(
            "Неизвестный режим загрузки.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    notify_policy = str(
        context.user_data.pop("srch_sub_notify_policy", None) or NOTIFY_EACH_UPDATE
    )
    if _subscription_policy_pair_does_nothing(notify_policy, download_policy):
        results = context.user_data.get("srch_results", [])
        if 0 <= index < len(results):
            context.user_data["srch_sub_notify_policy"] = notify_policy
            await query.edit_message_text(
                _advanced_download_text(results[index], notify_policy)
                + "\n\nТакой режим ничего не делает: уведомления выключены и загрузка тоже.",
                reply_markup=_advanced_download_keyboard(index, notify_policy),
                parse_mode="HTML",
            )
            return SEARCH_RESULTS
        await query.edit_message_text(
            "Такой режим ничего не делает: уведомления выключены и загрузка тоже.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END
    return await _download_and_add(
        query, context, index,
        subscribe=True,
        notify_policy=notify_policy,
        download_policy=download_policy,
    )


async def search_subscribe_back_to_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """«⬅️ К результатам» — re-render the results keyboard from cached data."""
    query = update.callback_query
    await query.answer()
    results = context.user_data.get("srch_results", [])
    if not results:
        await query.edit_message_text(
            "Результаты потеряны — начните поиск заново.",
            reply_markup=_search_error_keyboard(),
        )
        return ConversationHandler.END

    page = int(context.user_data.get("srch_results_page", 0))
    search_query = str(
        context.user_data.get("srch_search_query") or context.user_data.get("srch_query") or ""
    )
    banner = str(context.user_data.get("srch_banner") or "")
    source = str(context.user_data.get("srch_source") or "")
    text = _build_results_text(results, search_query, page, banner=banner)
    kb = _search_results_keyboard(
        results, page=page,
        show_switch_trackers=bool(
            context.user_data.get("srch_show_switch_trackers", False)
            or (jackett_client and source == "jackett")
        ),
        show_retry_jackett=bool(
            context.user_data.get("srch_show_retry_jackett", False)
            or (jackett_client and source == "rutracker")
        ),
        show_direct_rutracker=bool(
            context.user_data.get("srch_show_direct_rutracker", False)
            or (rutracker_client and source == "jackett")
        ),
        show_back_to_discovery=bool(
            context.user_data.get("srch_show_back_to_discovery", False)
            or source == "movie_discovery"
        ),
        show_back_to_cluster_picker=bool(context.user_data.get("srch_cluster_picker_return")),
        series_master=_search_is_series_master(context),
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=kb,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        # Fallback if message can't be edited (e.g. media message) — just acknowledge.
        logger.debug("Could not re-render results after back-to-results", exc_info=True)
    return SEARCH_RESULTS


async def plex_confirm_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirmed downloading despite Plex duplicate warning."""
    query = update.callback_query
    await query.answer()

    pending = context.user_data.pop("plex_pending", None)
    if not pending:
        await query.edit_message_text(
            "Данные потеряны — начните загрузку заново.",
            reply_markup=_task_error_keyboard(),
        )
        return ConversationHandler.END

    if pending["type"] == "search":
        return await _download_and_add(
            query, context, pending["index"],
            subscribe=pending.get("subscribe", False),
            notify_policy=pending.get("notify_policy"),
            download_policy=pending.get("download_policy"),
            _skip_plex_check=True,
            _movie_handled_cards=pending.get("movie_handled_cards"),
        )

    # magnet / torrent — handled via global plex_confirm_standalone below
    await query.edit_message_text("Неизвестный тип ожидания.", reply_markup=_task_error_keyboard())
    return ConversationHandler.END


async def plex_upgrade_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked «🔼 Заменить версией получше» — R.2 quality upgrade.

    Same code path as ``plex_confirm_download`` (download proceeds despite
    the existing Plex entry), but we additionally log the old season's
    rating_key for future cleanup. Auto-deletion of the old version is
    intentionally NOT included in v1 — that's a destructive action and
    we want operator/user feedback first. The rating_key plumbing here
    sets the groundwork for a follow-up commit (R.2.3 in roadmap).
    """
    query = update.callback_query
    await query.answer()

    pending = context.user_data.pop("plex_pending", None)
    if not pending:
        await query.edit_message_text(
            "Данные потеряны — начните загрузку заново.",
            reply_markup=_task_error_keyboard(),
        )
        return ConversationHandler.END

    old_key = pending.get("plex_old_season_key")
    if old_key:
        logger.info(
            "Plex upgrade requested: old_season_rating_key=%s — auto-deletion "
            "not yet implemented (R.2.3 roadmap item), keeping both versions",
            old_key,
        )

    if pending["type"] == "search":
        return await _download_and_add(
            query, context, pending["index"],
            subscribe=pending.get("subscribe", False),
            notify_policy=pending.get("notify_policy"),
            download_policy=pending.get("download_policy"),
            _skip_plex_check=True,
            _movie_handled_cards=pending.get("movie_handled_cards"),
        )

    await query.edit_message_text("Неизвестный тип ожидания.", reply_markup=_task_error_keyboard())
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
        await query.edit_message_text(
            "Данные потеряны — пришлите файл или ссылку заново.",
            reply_markup=_task_error_keyboard(),
        )
        return

    chat_id = query.message.chat.id if query.message else None

    if pending["type"] == "magnet":
        magnet_uri = pending.get("magnet_uri", "")
        if not magnet_uri:
            await query.edit_message_text(
                "Магнет-ссылка потеряна — пришлите её заново.",
                reply_markup=_task_error_keyboard(),
            )
            return
        await _do_process_magnet(query.message, context, magnet_uri, chat_id=chat_id)

    elif pending["type"] == "torrent":
        temp_path_str = pending.get("temp_path", "")
        safe_name = pending.get("safe_name", "download.torrent")
        temp_path = Path(temp_path_str)
        if not temp_path_str or not temp_path.exists():
            await query.edit_message_text(
                "Torrent-файл не найден — пришлите его заново.",
                reply_markup=_task_error_keyboard(),
            )
            return
        await _do_process_torrent(query.message, context, temp_path, safe_name, chat_id=chat_id)

    else:
        await query.edit_message_text("Неизвестный тип ожидания.", reply_markup=_task_error_keyboard())


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
    _series_bulk_mark_build_cancelled(context)
    _series_bulk_set_job_status(context, "cancelled")
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
        await _delete_command_message_safely(update, context, "cancel command")

    for key in (
        "srch_query", "srch_search_query", "srch_settings", "srch_results",
        "srch_intent",
        "srch_picked", "srch_kp_info", "srch_results_page",
        "srch_base_title", "srch_total_seasons", "srch_series_query",
        "srch_picked_quality", "srch_series_success_text", "srch_series_success_task_id",
        "srch_plex_seasons", "srch_season_input_msg_id", "srch_season_input_chat_id",
        "srch_ui_msg_id", "srch_ui_chat_id", "srch_banner",
        "srch_jackett_indexers", "srch_jackett_selected", "srch_source",
        "srch_picker_return_to", "srch_jackett_mode",
        "srch_series_bulk_action_running", "srch_series_bulk_base_quality",
        "srch_series_bulk_build_token", "srch_series_bulk_cancelled_token",
        "srch_series_bulk_failed", "srch_series_bulk_failed_candidates",
        "srch_series_bulk_index", "srch_series_bulk_job_id",
        "srch_series_bulk_long_notice", "srch_series_bulk_plan",
        "srch_series_bulk_profile", "srch_series_bulk_profile_draft",
        "srch_series_bulk_profile_screen", "srch_series_bulk_resolved",
        "srch_series_bulk_result_count", "srch_series_bulk_results",
        "srch_series_bulk_review_season", "srch_series_bulk_voice_expanded",
        "srch_series_bulk_voice_manual", "srch_series_bulk_wait_stage",
        "srch_series_bulk_warnings",
        # Cluster picker state (Proposal #1 — preserved between picker render
        # and the user's cluster choice; cleaned out at conversation exit).
        "srch_results_full", "srch_clusters", "srch_picker_clusters",
        "srch_cluster_picker_return",
    ):
        context.user_data.pop(key, None)
    # Cancel any in-flight did-you-mean prefetch task (Proposal #2).
    _cancel_didmean_prefetch(context)

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
    return await _run_search(query.edit_message_text, context, search_query)


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
        "srch_intent",
        "srch_picked", "srch_kp_info", "srch_results_page",
        "srch_base_title", "srch_total_seasons", "srch_series_query",
        "srch_picked_quality", "srch_series_success_text", "srch_series_success_task_id",
        "srch_plex_seasons", "srch_season_input_msg_id", "srch_season_input_chat_id",
        "srch_ui_msg_id", "srch_ui_chat_id", "srch_banner",
        "srch_jackett_indexers", "srch_jackett_selected", "srch_source",
        "srch_picker_return_to", "srch_jackett_mode",
        "srch_series_bulk_action_running", "srch_series_bulk_base_quality",
        "srch_series_bulk_build_token", "srch_series_bulk_cancelled_token",
        "srch_series_bulk_failed", "srch_series_bulk_failed_candidates",
        "srch_series_bulk_index", "srch_series_bulk_job_id",
        "srch_series_bulk_long_notice", "srch_series_bulk_plan",
        "srch_series_bulk_profile", "srch_series_bulk_profile_draft",
        "srch_series_bulk_profile_screen", "srch_series_bulk_resolved",
        "srch_series_bulk_result_count", "srch_series_bulk_results",
        "srch_series_bulk_review_season", "srch_series_bulk_voice_expanded",
        "srch_series_bulk_voice_manual", "srch_series_bulk_wait_stage",
        "srch_series_bulk_warnings",
        # Cluster picker state (Proposal #1 — preserved between picker render
        # and the user's cluster choice; cleaned out at conversation exit).
        "srch_results_full", "srch_clusters", "srch_picker_clusters",
        "srch_cluster_picker_return",
    ):
        context.user_data.pop(key, None)
    # Cancel any in-flight did-you-mean prefetch task (Proposal #2).
    _cancel_didmean_prefetch(context)

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
        await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())
        return ConversationHandler.END

    chat_id = query.message.chat.id if query.message else None
    if not _can_manage_subscription(chat_id, sub):
        await query.edit_message_text(
            "Эта подписка не относится к вашему чату.",
            reply_markup=_subscription_back_keyboard(),
        )
        return ConversationHandler.END

    _clear_search_intent(context)
    search_query = sub.get("query", "")
    if not search_query or jackett_client is None:
        await query.edit_message_text(
            "Подписка или Jackett недоступны.",
            reply_markup=_subscription_back_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["srch_query"] = search_query
    return await _run_search(query.edit_message_text, context, search_query)


# --- Subscription management ---


def _subscription_policy_texts(sub: dict) -> tuple[str, str]:
    notify_policy, download_policy = _coerce_subscription_policies(
        sub.get("notify_policy"), sub.get("download_policy")
    )
    return (
        notify_policy_label_ru(notify_policy),
        download_policy_label_ru(download_policy),
    )


def _subscription_episode_pair(sub: dict) -> tuple[int | None, int | None]:
    def _as_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    return _as_int(sub.get("last_episode_end")), _as_int(sub.get("total_episodes"))


def _subscription_progress_text(sub: dict) -> str:
    last_episode, total_episodes = _subscription_episode_pair(sub)
    if last_episode is not None and total_episodes is not None:
        return f"{last_episode} из {total_episodes} эп."
    if last_episode is not None:
        return f"{last_episode} эп., всего неизвестно"
    return "нет данных"


def _format_subscription_dt(dt: datetime) -> str:
    today = datetime.now(DISPLAY_TIMEZONE).date()
    if dt.date() == today:
        return f"сегодня {dt.strftime('%H:%M')}"
    if dt.date() == today + timedelta(days=1):
        return f"завтра {dt.strftime('%H:%M')}"
    return dt.strftime("%d.%m %H:%M")


def _format_subscription_datetime(value: object) -> str:
    if not value:
        return "—"
    raw = str(value)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=DISPLAY_TIMEZONE)
    except ValueError:
        return raw

    return _format_subscription_dt(dt)


def _subscription_source_text(sub: dict) -> str:
    if sub.get("type") != "jackett":
        return "Rutracker"

    tracker = str(sub.get("tracker") or "").strip()
    if not tracker:
        return "Jackett"
    if tracker.lower() == "jackett":
        return "Jackett"
    return f"Jackett · {tracker}"


def _subscription_status_text(sub: dict) -> str:
    if sub.get("unavailable_at"):
        reason = str(sub.get("unavailable_reason") or "").strip()
        if reason:
            return f"⚠️ проверка приостановлена: {reason}"
        return "⚠️ проверка приостановлена"

    pending = sub.get("pending_notification")
    if isinstance(pending, dict):
        if pending.get("complete"):
            return "финал найден, ждём повторной отправки уведомления"
        return "обновление найдено, ждём повторной отправки уведомления"

    last_episode, total_episodes = _subscription_episode_pair(sub)
    if last_episode is None:
        return "нет данных о прогрессе"
    if total_episodes is not None and total_episodes > 0 and last_episode >= total_episodes:
        return "сезон уже выглядит завершённым"

    notify_policy, download_policy = _coerce_subscription_policies(
        sub.get("notify_policy"), sub.get("download_policy")
    )
    if notify_policy == NOTIFY_FINAL_ONLY or download_policy == DOWNLOAD_ONLY_WHEN_COMPLETE:
        return "ждём финал сезона"
    return "ждём новые серии"


def _subscription_title(sub_key: str, sub: dict) -> str:
    if sub.get("type") == "jackett":
        title = str(sub.get("query") or sub.get("title") or sub_key)
    else:
        title = str(sub.get("title") or sub_key)
    return _format_sub_title(title)


def _subscription_icon(sub: dict) -> str:
    return "🌐" if sub.get("type") == "jackett" else "📺"


def _build_subscriptions_view(chat_id: int | None) -> tuple[str, InlineKeyboardMarkup | None]:
    subs = state_store.load_topic_subscriptions()
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
        return (
            "📭 <b>Подписок пока нет</b>\n\n"
            "Здесь появятся правила, по которым бот следит за новыми сериями, "
            "финалом сезона или новинками /new.\n\n"
            "Как добавить подписку: выберите «следить» или «скачать и следить» "
            "при работе с сериалом, либо подпишитесь на /new.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
            ]]),
        )

    total_count = len(my_subs) + len(jackett_subs_all) + (1 if is_movie_sub else 0)
    lines = [f"🔔 <b>Подписки</b> ({total_count})"]
    if _next_subscription_check_at is not None:
        next_dt = datetime.fromtimestamp(_next_subscription_check_at, DISPLAY_TIMEZONE)
        lines.append(f"Следующая проверка: {_format_subscription_dt(next_dt)}")
    rows = []

    for i, (topic_id, sub) in enumerate(my_subs.items(), 1):
        title = _subscription_title(topic_id, sub)
        notify_text, download_text = _subscription_policy_texts(sub)
        lines.append(
            f"\n{i}. 📺 <b>{html_module.escape(title)}</b>\n"
            f"   Источник: {_subscription_source_text(sub)}\n"
            f"   Прогресс: {_subscription_progress_text(sub)}\n"
            f"   Уведомления: {notify_text}\n"
            f"   Скачивание: {download_text}\n"
            f"   Статус: {html_module.escape(_subscription_status_text(sub))}"
        )
        rows.append([
            InlineKeyboardButton(
                f"⚙️ {i}. Настроить",
                callback_data=f"{SUB_CALLBACK_PREFIX}:settings:{topic_id}",
            ),
            InlineKeyboardButton(
                f"🔕 {i}. Отписаться",
                callback_data=f"{SUB_CALLBACK_PREFIX}:unsub:{topic_id}",
            ),
        ])

    offset = len(my_subs)
    for i, (key, sub) in enumerate(jackett_subs_all.items(), offset + 1):
        title = _subscription_title(key, sub)
        notify_text, download_text = _subscription_policy_texts(sub)
        lines.append(
            f"\n{i}. 🌐 <b>{html_module.escape(title)}</b>\n"
            f"   Источник: {html_module.escape(_subscription_source_text(sub))}\n"
            f"   Прогресс: {_subscription_progress_text(sub)}\n"
            f"   Уведомления: {notify_text}\n"
            f"   Скачивание: {download_text}\n"
            f"   Проверено: {_format_subscription_datetime(sub.get('last_check'))}\n"
            f"   Статус: {html_module.escape(_subscription_status_text(sub))}"
        )
        rows.append([
            InlineKeyboardButton(
                f"⚙️ {i}. Настроить",
                callback_data=f"{SUB_CALLBACK_PREFIX}:settings:{key}",
            ),
            InlineKeyboardButton(
                f"🔕 {i}. Отписаться",
                callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}",
            ),
        ])

    if is_movie_sub:
        lines.append(
            "\n🎬 <b>Новинки /new</b>\n"
            "   Уведомления: включены\n"
            "   Статус: присылаю новые фильмы и мультфильмы из подборки"
        )
        rows.append([
            InlineKeyboardButton(
                "🔕 Отписаться от /new",
                callback_data=f"{SUB_CALLBACK_PREFIX}:new_unsub",
            )
        ])

    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _subscription_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ К подпискам", callback_data=f"{SUB_CALLBACK_PREFIX}:list")],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])


def _subscription_settings_keyboard(sub_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Уведомления", callback_data=f"{SUB_CALLBACK_PREFIX}:settings_notify:{sub_key}")],
        [InlineKeyboardButton("⬇️ Скачивание", callback_data=f"{SUB_CALLBACK_PREFIX}:settings_download:{sub_key}")],
        [InlineKeyboardButton("⬅️ К подпискам", callback_data=f"{SUB_CALLBACK_PREFIX}:list")],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])


def _subscription_settings_text(sub_key: str, sub: dict, notice: str = "") -> str:
    title = html_module.escape(_subscription_title(sub_key, sub))
    notify_text, download_text = _subscription_policy_texts(sub)
    lines = []
    if notice:
        lines.append(notice)
        lines.append("")
    lines.extend([
        "⚙️ <b>Подписка</b>",
        f"{_subscription_icon(sub)} <b>{title}</b>",
        "",
        f"Источник: {html_module.escape(_subscription_source_text(sub))}",
        f"Прогресс: {_subscription_progress_text(sub)}",
        "",
        "Сейчас:",
        f"Уведомления: <b>{notify_text}</b>",
        f"Скачивание: <b>{download_text}</b>",
        "",
        "Что изменить?",
    ])
    return "\n".join(lines)


def _subscription_settings_locked_text(sub_key: str, sub: dict) -> str:
    title = html_module.escape(_subscription_title(sub_key, sub))
    return (
        "⚠️ <b>Настройки временно недоступны</b>\n\n"
        f"{_subscription_icon(sub)} <b>{title}</b>\n\n"
        "По этой подписке уже найдено обновление, и бот ждёт повторной отправки уведомления. "
        "После отправки можно будет менять правила снова."
    )


def _subscription_notify_keyboard(sub_key: str, sub: dict) -> InlineKeyboardMarkup:
    current_notify, current_download = _coerce_subscription_policies(
        sub.get("notify_policy"), sub.get("download_policy")
    )

    def _label(policy: str, text: str) -> str:
        return f"✅ {text}" if policy == current_notify else text

    choices = [
        (NOTIFY_EACH_UPDATE, "🔔 О каждой новой серии"),
        (NOTIFY_FINAL_ONLY, "🎯 Только когда сезон завершится"),
    ]
    if current_download != DOWNLOAD_NOTIFY_ONLY:
        choices.append((NOTIFY_SILENT, "🔇 Не уведомлять"))

    rows = [
        [InlineKeyboardButton(_label(policy, text), callback_data=f"{SUB_CALLBACK_PREFIX}:set_notify:{policy}:{sub_key}")]
        for policy, text in choices
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{SUB_CALLBACK_PREFIX}:settings:{sub_key}")])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _subscription_download_keyboard(sub_key: str, sub: dict) -> InlineKeyboardMarkup:
    current_notify, current_download = _coerce_subscription_policies(
        sub.get("notify_policy"), sub.get("download_policy")
    )

    def _label(policy: str, text: str) -> str:
        return f"✅ {text}" if policy == current_download else text

    choices = [
        (DOWNLOAD_AUTO_EACH_UPDATE, "⬇️ Новые серии по мере выхода"),
        (DOWNLOAD_ONLY_WHEN_COMPLETE, "📦 Когда сезон завершится"),
    ]
    if current_notify != NOTIFY_SILENT:
        choices.append((DOWNLOAD_NOTIFY_ONLY, "⏸ Не скачивать автоматически"))

    rows = [
        [InlineKeyboardButton(_label(policy, text), callback_data=f"{SUB_CALLBACK_PREFIX}:set_download:{policy}:{sub_key}")]
        for policy, text in choices
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{SUB_CALLBACK_PREFIX}:settings:{sub_key}")])
    rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _subscription_policy_choice_text(sub_key: str, sub: dict, axis: str) -> str:
    title = html_module.escape(_subscription_title(sub_key, sub))
    notify_text, download_text = _subscription_policy_texts(sub)
    if axis == "notify":
        text = (
            "🔔 <b>Когда уведомлять?</b>\n\n"
            f"{_subscription_icon(sub)} <b>{title}</b>\n\n"
            f"Текущее: <b>{notify_text}</b>"
        )
        _, download_policy = _coerce_subscription_policies(
            sub.get("notify_policy"), sub.get("download_policy")
        )
        if download_policy == DOWNLOAD_NOTIFY_ONLY:
            text += "\n\nНужно оставить хотя бы одно действие: уведомления или скачивание."
        return text

    text = (
        "⬇️ <b>Когда скачивать?</b>\n\n"
        f"{_subscription_icon(sub)} <b>{title}</b>\n\n"
        f"Текущее: <b>{download_text}</b>"
    )
    notify_policy, _ = _coerce_subscription_policies(
        sub.get("notify_policy"), sub.get("download_policy")
    )
    if notify_policy == NOTIFY_SILENT:
        text += "\n\nНужно оставить хотя бы одно действие: уведомления или скачивание."
    return text


def _subscription_noop_policy_text() -> str:
    return (
        "Так подписка ничего не будет делать.\n\n"
        "Выберите хотя бы одно действие: уведомлять или скачивать."
    )


def _subscription_noop_policy_keyboard(sub_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Настроить уведомления", callback_data=f"{SUB_CALLBACK_PREFIX}:settings_notify:{sub_key}")],
        [InlineKeyboardButton("⬇️ Настроить скачивание", callback_data=f"{SUB_CALLBACK_PREFIX}:settings_download:{sub_key}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"{SUB_CALLBACK_PREFIX}:settings:{sub_key}")],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])


def _split_subscription_policy_payload(payload: str) -> tuple[str, str]:
    policy, sep, sub_key = payload.partition(":")
    if not sep:
        return policy, ""
    return policy, sub_key


async def subs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    text, keyboard = _build_subscriptions_view(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    await _delete_command_message_safely(update, context, "subs command")


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
        text, keyboard = _build_subscriptions_view(chat_id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        asyncio.create_task(_send_auto_delete(context.bot, chat_id, "🔕 Уведомления о новинках отключены"))
        return

    if action == "list":
        text, keyboard = _build_subscriptions_view(chat_id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    if len(parts) < 3:
        return

    if action == "settings":
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(topic_id)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())
            return
        if isinstance(sub.get("pending_notification"), dict):
            await query.edit_message_text(
                _subscription_settings_locked_text(topic_id, sub),
                reply_markup=_subscription_back_keyboard(),
                parse_mode="HTML",
            )
            return
        await query.edit_message_text(
            _subscription_settings_text(topic_id, sub),
            reply_markup=_subscription_settings_keyboard(topic_id),
            parse_mode="HTML",
        )

    elif action == "settings_notify":
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(topic_id)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())
            return
        if isinstance(sub.get("pending_notification"), dict):
            await query.edit_message_text(
                _subscription_settings_locked_text(topic_id, sub),
                reply_markup=_subscription_back_keyboard(),
                parse_mode="HTML",
            )
            return
        await query.edit_message_text(
            _subscription_policy_choice_text(topic_id, sub, "notify"),
            reply_markup=_subscription_notify_keyboard(topic_id, sub),
            parse_mode="HTML",
        )

    elif action == "settings_download":
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(topic_id)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())
            return
        if isinstance(sub.get("pending_notification"), dict):
            await query.edit_message_text(
                _subscription_settings_locked_text(topic_id, sub),
                reply_markup=_subscription_back_keyboard(),
                parse_mode="HTML",
            )
            return
        await query.edit_message_text(
            _subscription_policy_choice_text(topic_id, sub, "download"),
            reply_markup=_subscription_download_keyboard(topic_id, sub),
            parse_mode="HTML",
        )

    elif action == "set_notify":
        notify_policy, sub_key = _split_subscription_policy_payload(topic_id)
        if notify_policy not in VALID_NOTIFY_POLICIES or not sub_key:
            await query.edit_message_text("Некорректная настройка подписки.", reply_markup=_subscription_back_keyboard())
            return

        subs = state_store.load_topic_subscriptions()
        sub = subs.get(sub_key)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())
            return
        if isinstance(sub.get("pending_notification"), dict):
            await query.edit_message_text(
                _subscription_settings_locked_text(sub_key, sub),
                reply_markup=_subscription_back_keyboard(),
                parse_mode="HTML",
            )
            return

        _current_notify, download_policy = _coerce_subscription_policies(
            sub.get("notify_policy"), sub.get("download_policy")
        )
        if notify_policy == NOTIFY_SILENT and download_policy == DOWNLOAD_NOTIFY_ONLY:
            await query.edit_message_text(
                _subscription_noop_policy_text(),
                reply_markup=_subscription_noop_policy_keyboard(sub_key),
            )
            return

        sub["notify_policy"] = notify_policy
        sub["download_policy"] = download_policy
        state_store.save_topic_subscriptions(subs)
        logger.info(
            "Subscription notify policy updated: key=%s notify=%s download=%s by chat=%s",
            sub_key, notify_policy, download_policy, chat_id,
        )
        await query.edit_message_text(
            _subscription_settings_text(sub_key, sub, notice="✅ Настройки обновлены"),
            reply_markup=_subscription_settings_keyboard(sub_key),
            parse_mode="HTML",
        )

    elif action == "set_download":
        download_policy, sub_key = _split_subscription_policy_payload(topic_id)
        allowed_download_updates = {
            DOWNLOAD_AUTO_EACH_UPDATE,
            DOWNLOAD_ONLY_WHEN_COMPLETE,
            DOWNLOAD_NOTIFY_ONLY,
        }
        if download_policy not in allowed_download_updates or not sub_key:
            await query.edit_message_text("Некорректная настройка подписки.", reply_markup=_subscription_back_keyboard())
            return

        subs = state_store.load_topic_subscriptions()
        sub = subs.get(sub_key)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())
            return
        if isinstance(sub.get("pending_notification"), dict):
            await query.edit_message_text(
                _subscription_settings_locked_text(sub_key, sub),
                reply_markup=_subscription_back_keyboard(),
                parse_mode="HTML",
            )
            return

        notify_policy, _current_download = _coerce_subscription_policies(
            sub.get("notify_policy"), sub.get("download_policy")
        )
        if notify_policy == NOTIFY_SILENT and download_policy == DOWNLOAD_NOTIFY_ONLY:
            await query.edit_message_text(
                _subscription_noop_policy_text(),
                reply_markup=_subscription_noop_policy_keyboard(sub_key),
            )
            return

        sub["notify_policy"] = notify_policy
        sub["download_policy"] = download_policy
        state_store.save_topic_subscriptions(subs)
        logger.info(
            "Subscription download policy updated: key=%s notify=%s download=%s by chat=%s",
            sub_key, notify_policy, download_policy, chat_id,
        )
        await query.edit_message_text(
            _subscription_settings_text(sub_key, sub, notice="✅ Настройки обновлены"),
            reply_markup=_subscription_settings_keyboard(sub_key),
            parse_mode="HTML",
        )

    elif action == "unsub":
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(topic_id)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        sub = subs.pop(topic_id, None)
        state_store.save_topic_subscriptions(subs)
        if sub:
            short = _format_sub_title(sub.get("title", ""))
            await query.edit_message_text(
                f"🔕 Подписка отменена:\n{short}",
                reply_markup=_subscription_back_keyboard(),
            )
        else:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())

    elif action == "jackett_unsub":
        key = topic_id
        subs = state_store.load_topic_subscriptions()
        sub = subs.get(key)
        if sub and not _can_manage_subscription(chat_id, sub):
            await query.edit_message_text(
                "Эта подписка не относится к вашему чату.",
                reply_markup=_subscription_back_keyboard(),
            )
            return
        sub = subs.pop(key, None)
        state_store.save_topic_subscriptions(subs)
        if sub:
            await query.edit_message_text(
                f"🔕 Подписка отменена:\n{sub.get('query', key)}",
                reply_markup=_subscription_back_keyboard(),
            )
        else:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_subscription_back_keyboard())

    elif action in {"admin_unsub", "admin_jackett_unsub"}:
        if not _is_admin_chat(chat_id):
            await query.edit_message_text(
                "Только администратор может управлять всеми подписками.",
                reply_markup=_task_error_keyboard(),
            )
            return

        subs = state_store.load_topic_subscriptions()
        sub = subs.pop(topic_id, None)
        state_store.save_topic_subscriptions(subs)
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_task_error_keyboard())
            return

        text, keyboard = _build_admin_subscriptions_view()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif action == "admin_set_mode":
        # Toggle the notification axis of an existing subscription. Works for
        # both Rutracker and Jackett; download_policy is deliberately preserved.
        if not _is_admin_chat(chat_id):
            await query.edit_message_text(
                "Только администратор может управлять всеми подписками.",
                reply_markup=_task_error_keyboard(),
            )
            return

        subs = state_store.load_topic_subscriptions()
        sub = subs.get(topic_id)
        if not sub:
            await query.edit_message_text("Подписка не найдена.", reply_markup=_task_error_keyboard())
            return
        current, new_policy = _toggle_subscription_notify_policy(sub)
        state_store.save_topic_subscriptions(subs)
        logger.info(
            "Subscription notify policy toggled: key=%s %s → %s by chat=%s "
            "download_policy=%s",
            topic_id, current, new_policy, chat_id, sub.get("download_policy"),
        )

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
        sub["unavailable_reason"] = "тема больше недоступна или удалена"
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

    is_complete = new_end >= new_total
    short_q = str(sub.get("query") or sub.get("title") or key)
    short_q = short_q[:40] + "…" if len(short_q) > 40 else short_q
    progress = f"\nСерии: {last_end} → {new_end} из {new_total}"

    # 1.3 policy split: ask the helpers whether to download and whether to push.
    from subscription_policy import should_download, should_notify
    wants_download = should_download(sub, is_complete=is_complete)
    wants_notify = should_notify(sub, is_complete=is_complete)

    # Conditional download — when download_policy=only_when_complete the
    # very point is to skip downloads on intermediate episodes.
    task_id = ""
    if wants_download:
        safe_name = _safe_filename(f"rutracker_{topic_id}.torrent")
        temp_path = _temp_path(safe_name)
        try:
            torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
            temp_path.write_bytes(torrent_bytes)
            task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
            if not task_id:
                raise _missing_task_id_error("для torrent-файла подписки Rutracker")
            if chat_id and task_id:
                _remember_task_owner(task_id, chat_id)
                _remember_task_meta(
                    task_id,
                    _build_task_meta_from_title(new_title or "", source="jackett_sub"),
                )
                _record_download_added_from_title_history(
                    task_id,
                    chat_id,
                    new_title or "",
                    method="torrent-файл",
                    meta_source="jackett_sub",
                )
        except (RutrackerError, DownloadStationError) as e:
            logger.warning("Failed to download Rutracker update for Jackett sub %s: %s", key, e)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    # State advance rules:
    # - If we ATTEMPTED a download and it FAILED, keep state frozen so the
    #   next check retries the same update (Bug A from 1.2).
    # - If we SKIPPED the download intentionally (only_when_complete on a
    #   partial episode), advance state — the user explicitly asked us not
    #   to download yet, so re-attempting next check would also skip.
    download_attempted_and_failed = wants_download and not task_id
    if not download_attempted_and_failed:
        sub["last_episode_end"] = new_end
        sub["total_episodes"] = new_total
        sub["title"] = new_title
        # Remove subscription only when the season is done AND we actually
        # downloaded it (or we explicitly didn't want to — but then there's
        # nothing else to do anyway).
        if is_complete and (task_id or not wants_download):
            subs.pop(key, None)
    else:
        logger.info(
            "Jackett/RT sub %s: download failed — keeping state at last_end=%s for retry",
            key, last_end,
        )

    # Silent advance: download succeeded (or was skipped by policy) AND
    # the user doesn't want a push right now → just log + return.
    if not wants_notify and not download_attempted_and_failed:
        logger.info(
            "Jackett/RT sub silent advance: key=%s ep=%s→%s/%s task=%s "
            "policy=%s/%s complete=%s",
            key, last_end, new_end, new_total, task_id or "-",
            sub.get("notify_policy"), sub.get("download_policy"), is_complete,
        )
        return True

    # Build notification — four branches by (download outcome, completion).
    if task_id and is_complete:
        text = (
            f"🔔 Подписка «{short_q}» — сезон завершён! ✅\n"
            f"{progress}\n"
            "Торрент обновлён в Download Station. Подписка снята."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(BUTTON_DOWNLOAD_LIST, callback_data=_task_callback("list", task_id)),
        ]])
    elif task_id:
        text = (
            f"🔔 Подписка «{short_q}» обновилась — задача добавлена!\n"
            f"\n🔎 {new_title}{progress}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(BUTTON_SHOW_TASK, callback_data=_task_callback("info", task_id)),
            InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}"),
        ]])
    elif not wants_download and is_complete:
        text = (
            f"🔔 Подписка «{short_q}» — сезон завершён! ✅\n"
            f"{progress}\n"
            "Авто-загрузка отключена для этой подписки. Подписка снята."
        )
        rows = []
        if topic_url:
            rows.append([InlineKeyboardButton("🔍 Открыть раздачу", url=topic_url)])
        rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
        kb = InlineKeyboardMarkup(rows)
    elif not wants_download:
        # Notify-only mode (download_policy=notify_only) — we intentionally
        # didn't download. Tell the user what's available with a download
        # button so they can pull it manually if they want.
        text = (
            f"🔔 Подписка «{short_q}» обновилась.\n"
            f"\n🔎 {new_title}{progress}\n\n"
            "Авто-загрузка отключена для этой подписки."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Посмотреть и скачать", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_view:{key}"),
            InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}"),
        ]])
    else:
        # We tried to download and failed — explicit error path.
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

    Returns a non-empty task_id. Raises when DS accepts the request but does not
    expose a task id, so the caller can fall back to notify-only mode.
    """
    title = candidate.title
    safe_name = _safe_filename(f"{title}.torrent")
    temp_path = _temp_path(safe_name)
    task_id = ""

    async def _create_subscription_magnet_task(magnet_url: str) -> str:
        try:
            before_tasks = await asyncio.to_thread(ds_client.list_tasks)
            known_task_ids = {task["id"] for task in before_tasks if task.get("id")}
        except DownloadStationError:
            logger.warning(
                "Failed to fetch task list before subscription magnet create",
                exc_info=True,
            )
            known_task_ids = set()

        magnet_task_id = await asyncio.to_thread(ds_client.create_magnet, magnet_url)
        if not magnet_task_id:
            magnet_task_id = await _wait_for_magnet_task_id(
                magnet_url, known_task_ids, None
            )
        if not magnet_task_id:
            raise _missing_task_id_error("для magnet-ссылки подписки")
        return magnet_task_id

    try:
        if candidate.torrent_url:
            try:
                torrent_bytes = await asyncio.to_thread(
                    jackett_client.download_torrent, candidate.torrent_url
                )
                temp_path.write_bytes(torrent_bytes)
                task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
                if not task_id:
                    raise _missing_task_id_error("для torrent-файла подписки")
                if chat_id and task_id:
                    _remember_task_owner(task_id, chat_id)
                    _remember_task_meta(task_id, _build_task_meta_from_title(title, source="jackett_sub"))
                    _record_download_added_from_title_history(
                        task_id,
                        chat_id,
                        title,
                        method="torrent-файл",
                        meta_source="jackett_sub",
                    )
                # Add public trackers unless private torrent
                if not _torrent_file_is_private(temp_path):
                    await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
                return task_id
            except JackettMagnetRedirect as redir:
                magnet = redir.magnet_url or candidate.magnet_url or ""
                if not magnet:
                    raise
                logger.info("Subscription torrent redirected to magnet, using it directly")
                task_id = await _create_subscription_magnet_task(magnet)
                if chat_id and task_id:
                    _remember_task_owner(task_id, chat_id)
                    _remember_task_meta(task_id, _build_task_meta_from_title(title, source="jackett_sub"))
                    _record_download_added_from_title_history(
                        task_id,
                        chat_id,
                        title,
                        method="magnet",
                        meta_source="jackett_sub",
                    )
                return task_id
            except MissingTaskIdError:
                raise
            except (JackettError, DownloadStationError) as e:
                logger.warning("Subscription torrent_url download failed (%s), trying magnet", e)

        if candidate.magnet_url:
            task_id = await _create_subscription_magnet_task(candidate.magnet_url)
            if chat_id and task_id:
                _remember_task_owner(task_id, chat_id)
                _remember_task_meta(task_id, _build_task_meta_from_title(title, source="jackett_sub"))
                _record_download_added_from_title_history(
                    task_id,
                    chat_id,
                    title,
                    method="magnet",
                    meta_source="jackett_sub",
                )
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
            is_complete = bool(episode_info and episode_info[0] >= episode_info[1] > 0)

            # 1.3 policy split: helpers decide whether to download / notify.
            from subscription_policy import should_download, should_notify
            wants_download = should_download(sub, is_complete=is_complete)
            wants_notify = should_notify(sub, is_complete=is_complete)

            # Conditional auto-download — only when policy permits AND there's
            # something to try. download_policy=only_when_complete waits.
            task_id: str | None = None
            if wants_download:
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

            download_attempted_and_failed = wants_download and not task_id
            final_action_succeeded = is_complete and (bool(task_id) or not wants_download)

            # Silent advance: download didn't fail AND user doesn't want a
            # push right now. Advance state (so we don't re-process this
            # same candidate next loop) and return.
            if not wants_notify and not download_attempted_and_failed:
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
                if final_action_succeeded:
                    subs.pop(key, None)
                changed = True
                logger.info(
                    "Jackett subscription silent advance: key=%s title=%s "
                    "policy=%s/%s complete=%s",
                    key, candidate.title,
                    sub.get("notify_policy"), sub.get("download_policy"), is_complete,
                )
                continue
            if download_attempted_and_failed:
                logger.info(
                    "Jackett subscription %s: auto-download failed — falling back "
                    "to notify-with-manual-link (will retry next check)", key,
                )

            # Build notification text — three branches by what happened.
            if task_id and is_complete:
                text = (
                    f"🔔 Подписка «{short_q}» — сезон завершён! ✅\n"
                    f"\n🔎 {candidate.title}"
                    f"\n📦 {candidate.size} | 🌱 {candidate.seeders} | 📡 {candidate.tracker}"
                    f"{progress}\n\n"
                    "Задача добавлена в DS. Подписка снята."
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        BUTTON_SHOW_TASK,
                        callback_data=_task_callback("info", task_id),
                    ),
                    InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
                ]])
            elif task_id:
                text = (
                    f"🔔 Подписка «{short_q}» обновилась — задача добавлена в DS!\n"
                    f"\n🔎 {candidate.title}"
                    f"\n📦 {candidate.size} | 🌱 {candidate.seeders} | 📡 {candidate.tracker}"
                    f"{progress}"
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        BUTTON_SHOW_TASK,
                        callback_data=_task_callback("info", task_id),
                    ),
                    InlineKeyboardButton(
                        "🔕 Отписаться",
                        callback_data=f"{SUB_CALLBACK_PREFIX}:jackett_unsub:{key}",
                    ),
                ]])
            elif not wants_download and is_complete:
                text = (
                    f"🔔 Подписка «{short_q}» — сезон завершён! ✅\n"
                    f"\n🔎 {candidate.title}"
                    f"\n📦 {candidate.size} | 🌱 {candidate.seeders} | 📡 {candidate.tracker}"
                    f"{progress}\n\n"
                    "Авто-загрузка отключена для этой подписки. Подписка снята."
                )
                rows = []
                topic_url = str(getattr(candidate, "topic_url", "") or "")
                if topic_url:
                    rows.append([InlineKeyboardButton("🔍 Открыть раздачу", url=topic_url)])
                rows.append([InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))])
                kb = InlineKeyboardMarkup(rows)
            elif not wants_download:
                # Notify-only mode — we intentionally didn't download.
                text = (
                    f"🔔 Найдено обновление подписки «{short_q}»:\n"
                    f"\n🔎 {candidate.title}"
                    f"\n📦 {candidate.size} | 🌱 {candidate.seeders} | 📡 {candidate.tracker}"
                    f"{progress}"
                    "\n\nАвто-загрузка отключена для этой подписки."
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
            else:
                # Tried to download and failed — explicit error path.
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
                if final_action_succeeded:
                    subs.pop(key, None)
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
    sub["unavailable_reason"] = "тема больше недоступна или удалена"
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

    if is_complete and task_id:
        text = (
            f"🔔 {short}: сезон завершён!\n"
            f"Эпизодов: {last_end} → {new_end} из {new_total} ✅\n"
            "Торрент обновлён в Download Station.\n"
            "Подписка снята автоматически."
        )
        return text, _download_list_keyboard()

    if is_complete:
        text = (
            f"🔔 {short}: сезон завершён!\n"
            f"Эпизодов: {last_end} → {new_end} из {new_total} ✅\n"
            "Авто-загрузка отключена для этой подписки.\n"
            "Подписка снята автоматически."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🔍 Открыть тему",
            url=_rutracker_topic_url(topic_id),
        )]])
        return text, keyboard

    if not task_id:
        text = (
            f"🔔 {short}: новая серия!\n"
            f"Эпизодов: {last_end} → {new_end} из {new_total}\n"
            "Авто-загрузка отключена для этой подписки."
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Открыть тему", url=_rutracker_topic_url(topic_id)),
            InlineKeyboardButton("🔕 Отписаться", callback_data=f"{SUB_CALLBACK_PREFIX}:unsub:{topic_id}"),
        ]])
        return text, keyboard

    text = (
        f"🔔 {short}: новая серия!\n"
        f"Эпизодов: {last_end} → {new_end} из {new_total}\n"
        "Торрент обновлён в Download Station."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(BUTTON_DOWNLOAD_LIST, callback_data=_task_callback("list", task_id)),
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

            # 1.3 policy split.
            from subscription_policy import should_download, should_notify
            wants_download = should_download(sub, is_complete=is_complete)
            wants_notify = should_notify(sub, is_complete=is_complete)

            task_id = ""
            if wants_download:
                safe_name = _safe_filename(f"rutracker_{topic_id}.torrent")
                temp_path = _temp_path(safe_name)
                try:
                    torrent_bytes = await asyncio.to_thread(rutracker_client.download_torrent, topic_id)
                    temp_path.write_bytes(torrent_bytes)
                    task_id = await asyncio.to_thread(ds_client.create_torrent_file, temp_path, safe_name)
                    if not task_id:
                        raise _missing_task_id_error("для torrent-файла подписки Rutracker")
                    if chat_id and task_id:
                        _remember_task_owner(task_id, chat_id)
                        _remember_task_meta(
                            task_id,
                            _build_task_meta_from_title(new_title or "", source="rutracker_sub"),
                        )
                        _record_download_added_from_title_history(
                            task_id,
                            chat_id,
                            new_title or "",
                            method="torrent-файл",
                            meta_source="rutracker_sub",
                        )
                except (RutrackerError, DownloadStationError) as e:
                    logger.warning("Failed to update subscription %s: %s", topic_id, e)
                    # Bug A (1.2) — don't advance state when download fails,
                    # so next check retries the same update.
                    continue
                finally:
                    try:
                        if temp_path.exists():
                            temp_path.unlink()
                    except OSError:
                        pass

            # Silent advance: download didn't fail (succeeded or was skipped
            # intentionally) AND no push wanted yet.
            if not wants_notify:
                sub["last_episode_end"] = new_end
                sub["total_episodes"] = new_total
                sub["title"] = new_title
                if is_complete and (task_id or not wants_download):
                    subs.pop(topic_id, None)
                logger.info(
                    "Subscription silent advance: topic=%s episodes=%s→%s/%s "
                    "policy=%s/%s complete=%s",
                    topic_id, last_end, new_end, new_total,
                    sub.get("notify_policy"), sub.get("download_policy"), is_complete,
                )
                changed = True
                continue

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

    search_enabled = RUTRACKER_ENABLED or JACKETT_ENABLED
    kp_hint = " или ссылку с Кинопоиска" if KINOPOISK_ENABLED else ""

    main_bullets: list[str] = []
    if search_enabled:
        main_bullets.append(
            f"• 🔍 Пришлите название фильма{kp_hint} — найду и предложу варианты"
        )
    if VOICE_SEARCH_ENABLED and search_enabled:
        main_bullets.append(
            "• 🎙 Или запишите голосом — бот распознает и запустит поиск"
        )
    if MOVIE_DISCOVERY_ENABLED and search_enabled:
        main_bullets.append(
            "• 🎬 /new — свежие фильмы и сериалы с рейтингом КП, пометками «уже в Plex» и кнопкой скачать"
        )
    if PLEX_ENABLED:
        main_bullets.append("• 📺 /continue — найти сезоны, которые можно продолжить")
    main_bullets.append("• 📋 /status — текущие загрузки и недавняя история")

    auto_bullets: list[str] = ["• когда скачивание завершилось"]
    auto_bullets.append("• когда вышла новая серия в подписке")
    if PLEX_ENABLED:
        auto_bullets.append("• когда контент появился в Plex")

    # Smart-search teaser when GPT is configured — single short line, не
    # хочется засорять welcome подробным списком (это уже в /help).
    smart_teaser = ""
    if GPT_ENABLED and search_enabled:
        smart_teaser = (
            "\n🧠 <b>Умный поиск:</b> AI правит опечатки, проверяет привязку к Кинопоиску, "
            "объясняет «почему этот фильм» в /new. Подробнее — /help.\n"
        )

    text = (
        "👋 Готов к работе!\n"
        "\n"
        "<b>Главное:</b>\n"
        f"{chr(10).join(main_bullets)}\n"
        f"{smart_teaser}"
        "\n"
        "<b>Уведомления приходят сами:</b>\n"
        f"{chr(10).join(auto_bullets)}\n"
        "\n"
        "Подробнее — /help."
    )
    await update.message.reply_text(text, parse_mode="HTML")
    await _delete_command_message_safely(update, context, "start command")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /help from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    search_enabled = RUTRACKER_ENABLED or JACKETT_ENABLED
    kp_hint = " или ссылку с Кинопоиска" if KINOPOISK_ENABLED else ""
    chat_id = update.effective_chat.id if update.effective_chat else None
    is_admin = _is_admin_chat(chat_id)

    main_bullets: list[str] = []
    if search_enabled:
        main_bullets.append(
            f"• 🔍 Пришлите название фильма/сериала{kp_hint} — найду раздачи и предложу скачать"
        )
    if VOICE_SEARCH_ENABLED and search_enabled:
        main_bullets.append(
            "• 🎙 Или запишите голосовое сообщение — распознаю и запущу тот же поиск"
        )
    if search_enabled:
        main_bullets.append(
            "• ⚙️ /settings — предпочтения поиска: качество, Original, субтитры и озвучка"
        )
    if MOVIE_DISCOVERY_ENABLED and search_enabled:
        main_bullets.append(
            "• 🎬 /new — свежие фильмы и мультфильмы; в push можно скачать 1-3 новинки или все доступные"
        )
    if is_admin:
        main_bullets.append("• 📋 /status — все загрузки, переключатель «мои / все»")
    else:
        main_bullets.append("• 📋 /status — ваши загрузки и недавняя история")
    main_bullets.append("• 🔔 /subs — активные подписки: прогресс и правила уведомлений/скачивания")

    extras: list[str] = []
    extras.append("• Прислать .torrent-файл или magnet-ссылку — добавлю в очередь загрузок")
    if search_enabled:
        extras.append(
            "• Подписаться на новые серии: у неполного сериала «⬇️ N» открывает варианты скачивания, а «🔔 N» — уведомления о новых сериях или финале"
        )
    if PLEX_ENABLED:
        extras.append("• /continue — найти в Plex сезоны, которые можно докачать по истории загрузок")
    if MOVIE_DISCOVERY_ENABLED and search_enabled:
        extras.append("• Подписаться на /new — пришлю push с постером, ссылкой на КП и быстрыми кнопками скачивания")

    smart_lines: list[str] = []
    if GPT_ENABLED and search_enabled:
        smart_lines.append("• AI ловит опечатки: «Дюра» → подскажу «Дюна»")
        smart_lines.append("• AI проверяет привязку фильма к Кинопоиску — меньше неверных матчей")
        if MOVIE_DISCOVERY_ENABLED:
            smart_lines.append("• 💭 короткое объяснение «почему этот фильм» в карточках /new")

    auto: list[str] = ["• когда скачивание завершилось или упало с ошибкой"]
    if search_enabled:
        auto.append("• когда вышла новая серия в подписке")
    if PLEX_ENABLED:
        auto.append("• когда контент появился в Plex (с кнопкой «▶️ Открыть в Plex»)")

    service: list[str] = ["• /ping — проверка связи", "• /id — показать ваш chat_id"]
    if is_admin:
        service.append("• /admin — админ-панель (диагностика, пользователи, подписки)")
        service.append("• /users — управление доступом пользователей")

    sections: list[str] = []
    if main_bullets:
        sections.append("<b>Главное:</b>\n" + "\n".join(main_bullets))
    if extras:
        sections.append("<b>Можно ещё:</b>\n" + "\n".join(extras))
    if smart_lines:
        sections.append("<b>🧠 Умный поиск:</b>\n" + "\n".join(smart_lines))
    if auto:
        sections.append("<b>Уведомления приходят сами:</b>\n" + "\n".join(auto))
    sections.append("<b>Служебное:</b>\n" + "\n".join(service))

    await update.message.reply_text(
        "\n\n".join(sections),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(BUTTON_CLOSE, callback_data="help:close")]]
        ),
    )
    await _delete_command_message_safely(update, context, "help command")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_admin_chat(chat_id):
        return

    progress_message = await update.message.reply_text("🛠️ Обновляю админ-панель…")
    await _safe_edit_message(
        progress_message,
        await _build_admin_panel_text(),
        parse_mode="HTML",
        reply_markup=_admin_panel_keyboard(**_admin_panel_kb_kwargs()),
    )
    await _delete_command_message_safely(update, context, "admin command")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    admin_chat_id = query.message.chat.id if query.message else None
    if not _is_admin_chat(admin_chat_id):
        await query.answer("Только для администратора", show_alert=True)
        logger.warning("Rejected admin callback from chat_id=%s", admin_chat_id)
        return

    parts = (query.data or "").split(":", 1)
    action = parts[1] if len(parts) > 1 else "home"
    if action != "plex_unmatched_toggle":
        await query.answer()

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
        await _safe_edit_callback(
            query,
            "🧭 Проверяю сервисы…",
            reply_markup=_admin_diagnostics_keyboard(),
        )
        await _safe_edit_callback(
            query,
            await _build_diagnostics_text(context),
            parse_mode="HTML",
            reply_markup=_admin_diagnostics_keyboard(),
        )
        return

    if action == "diagnostics_back":
        await _safe_edit_callback(
            query,
            await _build_cached_diagnostics_text(context),
            parse_mode="HTML",
            reply_markup=_admin_diagnostics_keyboard(),
        )
        return

    if action.startswith("diag_refresh:"):
        section = action.removeprefix("diag_refresh:")
        await _safe_edit_callback(
            query,
            "🧭 Проверяю раздел…",
            reply_markup=_admin_diagnostics_detail_keyboard(section),
        )
        await _safe_edit_callback(
            query,
            await _build_diagnostics_section_text(section, context, refresh=True),
            parse_mode="HTML",
            reply_markup=_admin_diagnostics_detail_keyboard(section),
        )
        return

    if action.startswith("diag_"):
        section = action.removeprefix("diag_")
        if _cached_diagnostics_report(context) is None:
            await _safe_edit_callback(
                query,
                "🧭 Проверяю раздел…",
                reply_markup=_admin_diagnostics_detail_keyboard(section),
            )
        await _safe_edit_callback(
            query,
            await _build_diagnostics_section_text(section, context, refresh=False),
            parse_mode="HTML",
            reply_markup=_admin_diagnostics_detail_keyboard(section),
        )
        return

    if action == "subscriptions":
        text, keyboard = _build_admin_subscriptions_view()
        await _safe_edit_callback(query, text, parse_mode="HTML", reply_markup=keyboard)
        return

    if action == "plex_unmatched":
        movies, shows = _get_plex_unmatched_lists()
        text = _format_unmatched_list(movies, shows)
        await _safe_edit_callback(
            query, text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data=f"{ADMIN_CALLBACK_PREFIX}:home"),
                InlineKeyboardButton(BUTTON_CLOSE, callback_data=f"{ADMIN_CALLBACK_PREFIX}:close"),
            ]]),
        )
        return

    if action == "plex_unmatched_toggle":
        new_state = not _is_plex_unmatched_notify_enabled()
        _set_plex_unmatched_notify_enabled(new_state)
        # The pop-up confirms the action; the panel re-renders below with
        # the toggle button's new label and any required initial-summary
        # push will trigger on the next refresh.
        await query.answer(
            "🔔 Уведомления включены" if new_state else "🔕 Уведомления выключены",
        )
        await _safe_edit_callback(
            query,
            await _build_admin_panel_text(),
            parse_mode="HTML",
            reply_markup=_admin_panel_keyboard(**_admin_panel_kb_kwargs()),
        )
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
        busy_mode = _movie_discovery_refresh_busy_mode()
        wait_note = f"\n\n{_movie_discovery_admin_refresh_wait_note('full')}" if busy_mode else ""
        asyncio.create_task(_refresh_movie_discovery_cache(max_stale_kp_refresh=None, force_refresh=True))
        await _safe_edit_callback(
            query,
            "🔄 <b>Запускаю полное обновление KP кэша</b>\n\n"
            f"Все <b>{len(kp_cache_dict)}</b> {_plural(len(kp_cache_dict), 'запись', 'записи', 'записей')} "
            f"помечены устаревшими.\n"
            "Обновление идёт в фоне — займёт несколько минут."
            + wait_note,
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
        busy_mode = _movie_discovery_refresh_busy_mode()
        wait_note = f"\n\n{_movie_discovery_admin_refresh_wait_note('gradual')}" if busy_mode else ""
        asyncio.create_task(_refresh_movie_discovery_cache(force_refresh=True))
        runs_needed = (len(kp_cache_dict) + _KP_MAX_STALE_REFRESH - 1) // _KP_MAX_STALE_REFRESH if kp_cache_dict else 1
        await _safe_edit_callback(
            query,
            "🔄 <b>Запускаю постепенное обновление KP кэша</b>\n\n"
            f"Все <b>{len(kp_cache_dict)}</b> {_plural(len(kp_cache_dict), 'запись', 'записи', 'записей')} "
            f"помечены устаревшими.\n"
            f"Обновляется по {_KP_MAX_STALE_REFRESH} за прогон — "
            f"~{runs_needed} {_plural(runs_needed, 'прогон', 'прогона', 'прогонов')} автообновления."
            + wait_note,
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

    if action == "reset_notify_failures":
        notified = _load_notified_tasks()
        reset_count = 0
        for entry in notified.values():
            if not isinstance(entry, dict):
                continue
            failures = entry.get("failures")
            if isinstance(failures, dict) and failures:
                entry["failures"] = {}
                reset_count += 1
        _save_notified_tasks(notified)
        logger.info(
            "Admin reset notification failure counters: tasks affected=%s", reset_count,
        )
        await _safe_edit_callback(
            query,
            f"✅ Сброшено счётчиков для <b>{reset_count}</b> "
            f"{_plural(reset_count, 'задачи', 'задач', 'задач')}.\n\n"
            "При следующем тике уведомлений (раз в 180с) попытки доставки "
            "начнутся заново с 0.",
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

    if action == "movie_status":
        # Drill-down opened from main /admin via «🎬 Новинки». Shows full
        # discovery configuration + KP cache info; KP management buttons are
        # hidden when KINOPOISK_API_KEY is not configured.
        await _safe_edit_callback(
            query,
            _format_admin_movie_discovery_details(),
            parse_mode="HTML",
            reply_markup=_admin_movie_status_keyboard(show_kp_buttons=KINOPOISK_ENABLED),
        )
        return

    await _safe_edit_callback(query, "🛠️ Обновляю админ-панель…")
    await _safe_edit_callback(
        query,
        await _build_admin_panel_text(),
        parse_mode="HTML",
        reply_markup=_admin_panel_keyboard(**_admin_panel_kb_kwargs()),
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /ping from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    await update.message.reply_text("pong")
    await _delete_command_message_safely(update, context, "ping command")


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)

    if not _is_allowed(update):
        logger.warning("Rejected /id from chat_id=%s", chat_id)
        await _reply_access_pending(update, context)
        return

    await update.message.reply_text(f"Ваш chat_id: {chat_id}")
    await _delete_command_message_safely(update, context, "id command")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        logger.warning("Rejected /status from chat_id=%s", _chat_id(update))
        await _reply_access_pending(update, context)
        return

    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    progress_message = await context.bot.send_message(chat_id=chat.id, text="📋 Получаю список загрузок…")
    await _delete_command_message_safely(update, context, "status command")

    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError as e:
        logger.exception("Failed to list Download Station tasks")
        await _safe_edit_message(
            progress_message,
            _download_station_user_error_text("Не удалось получить список загрузок."),
        )
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
        await _delete_command_message_safely(update, context, "new command")
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    cache = _load_movie_discovery_cache()
    logger.info(
        "movie_discovery: /new render path=command chat=%s cache_cards=%d top10_kp=[%s]",
        chat_id,
        len(cache.get("cards") or []),
        ",".join(str(c.get("kp_id") or "-") for c in (cache.get("cards") or [])[:10]),
    )
    if not cache.get("cards"):
        busy_mode = _movie_discovery_refresh_busy_mode()
        progress_text = (
            _movie_discovery_refresh_wait_text(busy_mode)
            if busy_mode
            else _movie_discovery_refresh_start_text()
        )
        progress = await update.message.reply_text(progress_text)
        cache = await _refresh_movie_discovery_cache()
        await _safe_edit_message(
            progress,
            _format_movie_discovery_cache(cache, chat_id=chat_id),
            reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        _mark_user_shown_in_new(chat_id, (cache.get("cards") or [])[:10])
        await _delete_command_message_safely(update, context, "new command")
        return

    # Re-apply Plex badges from current in-memory library (cheap, no network).
    # Needed after restart: JSON cache may have stale in_plex values.
    _enrich_cards_with_plex(cache.get("cards") or [])

    # Recompute scores under current formula/year and resort. Cache stores
    # `score` snapshotted at last refresh — formula or year boundary changes
    # leave the order stale until the next refresh. Pure CPU, no network.
    _recompute_and_resort_cards(cache.get("cards") or [])

    await update.message.reply_text(
        _format_movie_discovery_cache(cache, chat_id=chat_id),
        reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    # Mark the displayed top-10 as 'seen' for this user — the «🆕» badge will
    # disappear next time they open /new (until a new film appears).
    _mark_user_shown_in_new(chat_id, (cache.get("cards") or [])[:10])
    await _delete_command_message_safely(update, context, "new command")


async def movie_new_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return

    chat_id = query.message.chat.id if query.message else None
    await query.answer()
    busy_mode = _movie_discovery_refresh_busy_mode()
    progress_text = (
        _movie_discovery_refresh_wait_text(busy_mode)
        if busy_mode
        else _movie_discovery_refresh_start_text()
    )
    await _safe_edit_callback(query, progress_text)
    logger.info("movie_discovery: /new render path=refresh_callback chat=%s — refreshing now", chat_id)
    cache = await _refresh_movie_discovery_cache()
    logger.info(
        "movie_discovery: /new render path=refresh_callback chat=%s post_refresh cache_cards=%d top10_kp=[%s]",
        chat_id,
        len(cache.get("cards") or []),
        ",".join(str(c.get("kp_id") or "-") for c in (cache.get("cards") or [])[:10]),
    )
    await _safe_edit_callback(
        query,
        _format_movie_discovery_cache(cache, chat_id=chat_id),
        reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    _mark_user_shown_in_new(chat_id, (cache.get("cards") or [])[:10])


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
    logger.info(
        "movie_discovery: /new render path=open_callback chat=%s cache_cards=%d top10_kp=[%s]",
        chat_id,
        len(cache.get("cards") or []),
        ",".join(str(c.get("kp_id") or "-") for c in (cache.get("cards") or [])[:10]),
    )
    _enrich_cards_with_plex(cache.get("cards") or [])
    _recompute_and_resort_cards(cache.get("cards") or [])
    await _safe_edit_callback(
        query,
        _format_movie_discovery_cache(cache, chat_id=chat_id),
        reply_markup=_movie_discovery_keyboard(cache.get("cards", []), chat_id=chat_id),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    _mark_user_shown_in_new(chat_id, (cache.get("cards") or [])[:10])


def _movie_notification_stale_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Открыть /new", callback_data="new:open")],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])


def _movie_notification_bulk_keyboard(push_id: str, count: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append([InlineKeyboardButton(f"✅ Скачать {count}", callback_data=f"new:bulk_ok:{push_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"new:push_back:{push_id}")])
    return InlineKeyboardMarkup(rows)


def _movie_notification_done_keyboard(chat_id: int | None) -> InlineKeyboardMarkup:
    scope = _default_list_scope(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 К списку загрузок", callback_data=_task_callback("list", scope))],
        [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
    ])


def _movie_notification_confirm_text(items: list[dict]) -> tuple[str, int]:
    download_items: list[dict] = []
    skipped_plex: list[dict] = []
    unavailable: list[dict] = []
    for item in items:
        card = item.get("card") if isinstance(item, dict) else {}
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(card, dict) and card.get("in_plex"):
            skipped_plex.append(item)
        elif isinstance(result, dict):
            download_items.append(item)
        else:
            unavailable.append(item)

    lines = ["⬇️ <b>Скачать все из уведомления?</b>", ""]
    if download_items:
        lines.append("Будет добавлено:")
        for index, item in enumerate(download_items, 1):
            card = item["card"]
            result = item["result"]
            title = html_module.escape(str(card.get("title") or result.get("title") or "Без названия"))
            lines.append(f"{index}. {title} — {html_module.escape(_movie_notification_result_label(result))}")
            for note in item.get("notes") or []:
                lines.append(f"   {html_module.escape(str(note))}")
        total_gb = _movie_notification_total_size_gb(download_items)
        if total_gb > 0:
            lines.extend(["", f"Всего примерно: {total_gb:.1f} GB"])
    if skipped_plex:
        lines.extend(["", f"Уже есть в Plex, пропущу: {len(skipped_plex)}"])
    if unavailable:
        lines.extend(["", f"Без доступной раздачи, пропущу: {len(unavailable)}"])
    return "\n".join(lines), len(download_items)


async def movie_new_notification_push_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    try:
        push_id = (query.data or "").split(":")[2]
    except IndexError:
        push_id = ""
    snapshot = _load_movie_notification_snapshot(push_id, chat_id)
    if not snapshot:
        await query.edit_message_text(
            "Уведомление устарело. Откройте свежий список /new.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    cards = _movie_notification_cards_from_items(items)
    await query.edit_message_text(
        _format_movie_notification_text(cards),
        reply_markup=_movie_notification_keyboard(
            push_id,
            len(items),
            _movie_notification_downloadable_indices(items),
        ),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def movie_new_notification_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    try:
        push_id = (query.data or "").split(":")[2]
    except IndexError:
        push_id = ""
    snapshot = _load_movie_notification_snapshot(push_id, chat_id)
    if not snapshot:
        await query.edit_message_text(
            "Уведомление устарело. Откройте свежий список /new.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    text, count = _movie_notification_confirm_text(items)
    await query.edit_message_text(
        text,
        reply_markup=_movie_notification_bulk_keyboard(push_id, count),
        parse_mode="HTML",
    )


async def movie_new_notification_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    try:
        _prefix, _action, push_id, index_raw = (query.data or "").split(":", 3)
        index = int(index_raw)
    except (ValueError, IndexError):
        await query.edit_message_text(
            "Не удалось разобрать кнопку скачивания.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return ConversationHandler.END
    snapshot = _load_movie_notification_snapshot(push_id, chat_id)
    items = snapshot.get("items") if isinstance(snapshot, dict) and isinstance(snapshot.get("items"), list) else []
    if not (0 <= index < len(items)):
        await query.edit_message_text(
            "Уведомление устарело. Откройте свежий список /new.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return ConversationHandler.END
    item = items[index]
    card = item.get("card") if isinstance(item.get("card"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else None
    if card.get("in_plex"):
        await query.edit_message_text(
            "Этот фильм уже есть в Plex. Откройте /new, если хотите выбрать другую новинку.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return ConversationHandler.END
    if result is None:
        await query.edit_message_text(
            "По этой новинке нет доступной раздачи. Откройте /new и выберите другой фильм.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return ConversationHandler.END

    search_query = f"{card.get('title', '')} {card.get('year', '')}".strip()
    context.user_data["srch_results"] = [result]
    context.user_data["srch_results_page"] = 0
    context.user_data["srch_search_query"] = search_query
    context.user_data["srch_query"] = search_query
    context.user_data["srch_source"] = "movie_discovery_notification"
    context.user_data["srch_banner"] = "🎬 Раздача из уведомления /new"
    return await _download_and_add(
        query,
        context,
        0,
        subscribe=False,
        _movie_handled_cards=[card] if card else None,
    )


async def movie_new_notification_bulk_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat.id if query.message else None
    try:
        push_id = (query.data or "").split(":")[2]
    except IndexError:
        push_id = ""
    snapshot = _load_movie_notification_snapshot(push_id, chat_id)
    if not snapshot:
        await query.edit_message_text(
            "Уведомление устарело. Откройте свежий список /new.",
            reply_markup=_movie_notification_stale_keyboard(),
        )
        return
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    to_download = [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("card"), dict)
        and isinstance(item.get("result"), dict)
        and not item["card"].get("in_plex")
    ]
    skipped_plex = [
        item for item in items
        if isinstance(item, dict) and isinstance(item.get("card"), dict) and item["card"].get("in_plex")
    ]
    if not to_download:
        await query.edit_message_text(
            "Все фильмы из уведомления уже есть в Plex или недоступны для скачивания.",
            reply_markup=_movie_notification_done_keyboard(chat_id),
        )
        return

    disk_check = await asyncio.to_thread(_check_disk_space_for_download)
    disk_warn = ""
    if disk_check is not None:
        severity, msg = disk_check
        if severity == "block":
            await query.edit_message_text(
                msg,
                reply_markup=_movie_notification_done_keyboard(chat_id),
                parse_mode="HTML",
            )
            return
        disk_warn = msg

    successes: list[dict] = []
    failures: list[dict] = []
    total = len(to_download)
    for index, item in enumerate(to_download, 1):
        card = item["card"]
        result = item["result"]
        title = str(card.get("title") or result.get("title") or "Без названия")
        await query.edit_message_text(
            f"⏳ Добавляю загрузки\n\n{index}/{total}: {html_module.escape(title)}",
            parse_mode="HTML",
        )
        try:
            entry = _pending_download_entry_from_result(
                result,
                chat_id=chat_id,
                subscribe=False,
                error="",
            )
            task_id, method = await _attempt_pending_download(entry)
            if task_id:
                _remember_task_owner(task_id, chat_id)
                _remember_task_meta(task_id, _build_task_meta_from_result(result, source="movie_discovery"))
            _record_download_added_history(
                task_id,
                chat_id,
                result,
                method=method,
                meta_source="movie_discovery",
                subscribe=False,
            )
            if chat_id:
                _mark_user_handled_in_new(chat_id, [card])
            successes.append({"title": title, "task_id": task_id, "method": method})
        except (JackettError, RutrackerError, DownloadStationError, RuntimeError) as exc:
            failures.append({"title": title, "error": _format_download_error(exc)})
            _record_download_history(
                "download_failed",
                chat_id=chat_id,
                result=result,
                meta=_build_task_meta_from_result(result, source="movie_discovery"),
                error=_format_download_error(exc),
            )

    lines = ["⬇️ <b>Скачивание из уведомления</b>", ""]
    if successes:
        lines.append(f"Добавлено: {len(successes)}")
        for item in successes:
            task = f" · ID: {item['task_id']}" if item.get("task_id") else ""
            lines.append(f"• {html_module.escape(item['title'])}{task}")
    if skipped_plex:
        lines.extend(["", f"Уже есть в Plex, пропущено: {len(skipped_plex)}"])
    if failures:
        lines.extend(["", f"Ошибок: {len(failures)}"])
        for item in failures:
            lines.append(f"• {html_module.escape(item['title'])}: {html_module.escape(item['error'])}")
    if disk_warn:
        lines.extend(["", html_module.escape(disk_warn)])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=_movie_notification_done_keyboard(chat_id),
        parse_mode="HTML",
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
    chat_id = query.message.chat.id if query.message else None
    cache = _load_movie_discovery_cache()
    cards = cache.get("cards") if isinstance(cache.get("cards"), list) else []
    try:
        parts = (query.data or "").split(":")
        index = int(parts[2])
        token = parts[3] if len(parts) > 3 else ""
    except (TypeError, ValueError):
        await query.edit_message_text(
            "Не удалось открыть новинку. Обновите список и выберите фильм ещё раз.",
            reply_markup=_movie_discovery_keyboard(cards, chat_id=chat_id),
        )
        return ConversationHandler.END

    card = _find_movie_discovery_card(cards, index, token)
    if card is None:
        await query.edit_message_text(
            "Новинка изменилась после обновления кэша. Обновите список и выберите фильм ещё раз.",
            reply_markup=_movie_discovery_keyboard(cards, chat_id=chat_id),
        )
        return ConversationHandler.END

    releases = [_movie_release_to_search_result(release) for release in card.get("releases", [])]
    releases = sorted(releases, key=_score_result, reverse=True)
    if not releases:
        await query.edit_message_text(
            "По этой новинке пока нет подходящих раздач. Можно вернуться к списку и выбрать другой фильм.",
            reply_markup=_movie_discovery_keyboard(cards, chat_id=chat_id),
        )
        return ConversationHandler.END

    search_query = f"{card.get('title', '')} {card.get('year', '')}".strip()
    banner = "🎬 Раздачи по выбранной новинке"
    context.user_data["srch_results"] = releases
    context.user_data["srch_results_page"] = 0
    context.user_data["srch_search_query"] = search_query
    context.user_data["srch_query"] = search_query
    context.user_data["srch_source"] = "movie_discovery"
    context.user_data["srch_banner"] = banner
    await query.edit_message_text(
        _build_results_text(releases, search_query, 0, banner=banner),
        reply_markup=_search_results_keyboard(releases, page=0, show_back_to_discovery=True),
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
    stored_approved_users = state_store.load_approved_users()
    approved_users = {
        uid: info
        for uid, info in sorted(stored_approved_users.items())
        if uid not in ALLOWED_CHAT_IDS and uid not in ADMIN_CHAT_IDS
    }
    pending_users = {
        uid: label
        for uid, label in sorted(ACCESS_PENDING_USERS.items())
        if uid not in ALLOWED_CHAT_IDS and uid not in ADMIN_CHAT_IDS and uid not in stored_approved_users
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

    lines.append("\n⏳ Ожидают решения:")
    if pending_users:
        lines.extend(f"  • {uid} — {label}" if label else f"  • {uid}" for uid, label in pending_users.items())
    else:
        lines.append("  (нет)")

    return "\n".join(lines), users_keyboard(
        approved_users,
        pending_users,
        back_to_admin=back_to_admin,
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not _is_admin_chat(chat_id):
        return

    text, keyboard = _format_users_panel(back_to_admin=False)
    await update.message.reply_text(text, reply_markup=keyboard)
    await _delete_command_message_safely(update, context, "users command")


def _access_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Админ-панель", callback_data=f"{ADMIN_CALLBACK_PREFIX}:home"),
        InlineKeyboardButton(BUTTON_CLOSE, callback_data=f"{ADMIN_CALLBACK_PREFIX}:close"),
    ]])


def _access_remove_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Удалить доступ", callback_data=f"{ACCESS_CALLBACK_PREFIX}:remove:{chat_id}")],
        [InlineKeyboardButton(BUTTON_BACK, callback_data=f"{ACCESS_CALLBACK_PREFIX}:users_refresh")],
    ])


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
        await query.edit_message_text(
            "Не удалось разобрать запрос доступа.",
            reply_markup=_access_result_keyboard(),
        )
        return

    if action == "approve":
        already_allowed = target_chat_id in _all_allowed_chat_ids()
        name = ACCESS_PENDING_USERS.pop(target_chat_id, "")
        state_store.add_approved_user(target_chat_id, name)

        note = "уже был разрешен" if already_allowed else "разрешен"
        label = f" ({name})" if name else ""
        await query.edit_message_text(
            f"Доступ {note}.\nchat_id: {target_chat_id}{label}",
            reply_markup=_access_result_keyboard(),
        )
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=_build_access_approved_text(),
            )
        except Exception:
            logger.warning("Failed to notify approved chat_id=%s", target_chat_id, exc_info=True)
        return

    if action == "deny":
        ACCESS_PENDING_USERS.pop(target_chat_id, None)
        await query.edit_message_text(
            f"Запрос доступа отклонен.\nchat_id: {target_chat_id}",
            reply_markup=_access_result_keyboard(),
        )
        return

    if action == "remove_confirm":
        users = state_store.load_approved_users()
        info = users.get(target_chat_id, {})
        name = info.get("name", "") if isinstance(info, dict) else ""
        label = f"\nПользователь: {name}" if name else ""
        await query.edit_message_text(
            "Удалить доступ?\n"
            f"chat_id: {target_chat_id}{label}\n\n"
            "Связанные задачи и подписки пользователя будут очищены.",
            reply_markup=_access_remove_confirm_keyboard(target_chat_id),
        )
        return

    if action == "remove":
        state_store.remove_approved_user(target_chat_id)
        _revoke_chat_runtime_state(target_chat_id)
        ACCESS_PENDING_USERS.pop(target_chat_id, None)
        logger.info("Admin removed access for chat_id=%s", target_chat_id)
        text, keyboard = _format_users_panel()
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    await query.edit_message_text(
        "Неизвестное действие с доступом.",
        reply_markup=_access_result_keyboard(),
    )


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
        await query.edit_message_text(
            "Не удалось разобрать действие.",
            reply_markup=_task_error_keyboard(),
        )
        return

    chat_id = _chat_id_from_query(query)
    message_id = query.message.message_id if query.message else None
    retry_callback = query.data or None
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
            await query.edit_message_text(
                _download_station_user_error_text("Не удалось получить список загрузок."),
                reply_markup=_task_error_keyboard(retry_callback=retry_callback),
            )
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
            await query.edit_message_text(
                _download_station_user_error_text("Не удалось получить список загрузок."),
                reply_markup=_task_error_keyboard(retry_callback=retry_callback, list_scope=scope),
            )
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
            await query.edit_message_text(
                "Эта задача не относится к вашим загрузкам.",
                reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
            )
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
            await query.edit_message_text(
                _download_station_user_error_text("Не удалось получить список загрузок."),
                reply_markup=_task_error_keyboard(retry_callback=retry_callback, list_scope=scope),
            )
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
            await query.edit_message_text(
                _download_station_user_error_text("Не удалось удалить завершенные задачи."),
                reply_markup=_task_error_keyboard(retry_callback=retry_callback, list_scope=scope),
            )
            return

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError:
            await query.edit_message_text(
                f"Удалено завершенных задач: {len(finished_ids)}.",
                reply_markup=_task_error_keyboard(list_scope=scope),
            )
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
            await query.edit_message_text(
                "Эта задача не относится к вашим загрузкам.",
                reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
            )
            return

        await _safe_edit_callback(query, "➕ Добавляю public-трекеры…")
        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError as e:
            await query.edit_message_text(
                _download_station_user_error_text("Не удалось получить задачу.", task_id=task_id),
                reply_markup=_task_error_keyboard(retry_callback=retry_callback, list_scope=_default_list_scope(chat_id)),
            )
            return

        task = _find_task(tasks, task_id)
        if not task:
            await query.edit_message_text(
                f"Задача не найдена.\nID: {task_id}",
                reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
            )
            return
        if (task.get("type") or "").lower() != "bt":
            await query.edit_message_text(
                f"Public-трекеры доступны только для BT-задач.\n\n{_format_task_card(task, chat_id)}",
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
            "\n".join(["Трекеры обновлены.", *tracker_lines, "", _format_task_card(task, chat_id)]),
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
                    [InlineKeyboardButton(BUTTON_SHOW_TASK, callback_data=_task_callback("info", task_id))],
                    [InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", ""))],
                ])
                await query.edit_message_reply_markup(reply_markup=new_kb)
        except Exception:
            logger.debug("Failed to update keyboard after sub_notify", exc_info=True)
        return

    if action in {"resume", "pause", "delete"}:
        if not _can_access_task_id(chat_id, task_id):
            await query.edit_message_text(
                "Эта задача не относится к вашим загрузкам.",
                reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
            )
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
                            BUTTON_DOWNLOAD_LIST,
                            callback_data=_task_callback("list", scope),
                        )
                    ], [
                        InlineKeyboardButton(BUTTON_CLOSE, callback_data=_task_callback("close", "")),
                    ]]),
                )
                if del_chat_id and del_msg_id:
                    asyncio.create_task(
                        _delayed_delete_message(context.bot, del_chat_id, del_msg_id, delay=5.0)
                    )
                return
        except DownloadStationError as e:
            await query.edit_message_text(
                _download_station_user_error_text("Не удалось выполнить действие с задачей.", task_id=task_id),
                reply_markup=_task_error_keyboard(
                    retry_callback=retry_callback,
                    list_scope=_default_list_scope(chat_id),
                ),
            )
            return

        try:
            tasks = await asyncio.to_thread(ds_client.list_tasks)
        except DownloadStationError:
            await query.edit_message_text(
                f"{notice}\nID: {task_id}",
                reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
            )
            return

        task = _find_task(tasks, task_id)
        if task:
            await query.edit_message_text(
                f"{notice}\n\n{_format_task_card(task, chat_id)}",
                reply_markup=_make_task_keyboard(task_id, task.get("status", ""), task.get("type", "")),
            )
            _register_task_card_from_query(query, task_id)
        else:
            await query.edit_message_text(
                f"{notice}\nID: {task_id}",
                reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
            )
        return

    if not _can_access_task_id(chat_id, task_id):
        await query.edit_message_text(
            "Эта задача не относится к вашим загрузкам.",
            reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
        )
        return

    await _safe_edit_callback(query, "🔎 Получаю задачу…")
    try:
        tasks = await asyncio.to_thread(ds_client.list_tasks)
    except DownloadStationError as e:
        await query.edit_message_text(
            _download_station_user_error_text("Не удалось получить задачу.", task_id=task_id),
            reply_markup=_task_error_keyboard(
                retry_callback=retry_callback,
                list_scope=_default_list_scope(chat_id),
            ),
        )
        return

    task = _find_task(tasks, task_id)
    if not task:
        await query.edit_message_text(
            f"Задача не найдена.\nID: {task_id}",
            reply_markup=_task_error_keyboard(list_scope=_default_list_scope(chat_id)),
        )
        return

    status = (task.get("status") or "").lower()
    await query.edit_message_text(
        _format_task_card(task, chat_id),
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
        await _delete_command_message_safely(update, context, "resume command")
        return

    task_id = context.args[0].strip()
    if not _can_access_task_id(update.effective_chat.id if update.effective_chat else None, task_id):
        await update.message.reply_text("Эта задача не относится к вашим загрузкам.")
        await _delete_command_message_safely(update, context, "resume command")
        return

    progress_message = await update.message.reply_text("▶️ Отправляю команду запуска…")
    await _delete_command_message_safely(update, context, "resume command")
    try:
        await asyncio.to_thread(ds_client.resume_task, task_id)
    except DownloadStationError as e:
        logger.exception("Failed to resume Download Station task")
        await _safe_edit_message(
            progress_message,
            _download_station_user_error_text("Не удалось запустить задачу.", task_id=task_id),
        )
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
        if task_id:
            _remember_task_owner(task_id, chat_id)
            dn = _extract_magnet_dn(magnet_uri)
            meta = await _build_task_meta_from_title_with_gpt(dn, source="magnet") if dn else None
            if dn:
                _remember_task_meta(task_id, meta)
            _record_download_added_from_title_history(
                task_id,
                chat_id,
                dn,
                method="magnet-ссылка",
                meta_source="magnet",
                meta=meta,
            )
            await asyncio.sleep(_TRACKER_INJECT_INITIAL_DELAY)
            tracker_result = await asyncio.to_thread(_add_public_trackers_to_download_task, task_id)
            _mark_tracker_processed_if_final(task_id, tracker_result)
        else:
            tracker_result = TrackerApplyResult(
                skipped_reason="ID задачи пока не найден, трекеры не добавляю"
            )
    except DownloadStationError as e:
        logger.exception("Failed to create Download Station task")
        await _safe_edit_message(
            progress_message,
            _download_station_user_error_text("Не удалось добавить magnet-ссылку."),
        )
        return

    msg_text = _task_added_message(
        "magnet-ссылка",
        task_id=task_id,
        tracker_result=tracker_result,
        accepted_without_task_id=not task_id,
    )
    if not task_id:
        msg_text += f"\n\n{_magnet_without_task_id_note()}"

    await _safe_edit_message(
        progress_message,
        msg_text,
        reply_markup=_task_reply_markup(task_id),
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
        if not task_id:
            raise _missing_task_id_error("для torrent-файла")
        _remember_task_owner(task_id, chat_id)
        meta_title = _normalize_torrent_filename_for_match(safe_name)
        meta = await _build_task_meta_from_title_with_gpt(meta_title, source="torrent_file") if meta_title else None
        if meta_title:
            _remember_task_meta(task_id, meta)
        _record_download_added_from_title_history(
            task_id,
            chat_id,
            meta_title or safe_name,
            method="torrent-файл",
            meta_source="torrent_file",
            meta=meta,
        )
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
        await _safe_edit_message(progress_message, _torrent_file_user_error_text())
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


def _voice_record_usage(
    *,
    duration_sec: float,
    text: str,
    outcome: str,  # "ok" | "error"
    error_label: str | None,
) -> None:
    """Append a voice-search request to the rolling usage file.

    Keeps a single monthly bucket (auto-resets when the month rolls over) plus
    `last_request` and `last_error` records for /admin diagnostics. Cheap enough
    to call synchronously inside the voice handler — one JSON load/save.
    """
    now = datetime.now(DISPLAY_TIMEZONE)
    current_month = now.strftime("%Y-%m")
    usage = state_store.load_voice_usage()
    if usage.get("month") != current_month:
        usage = {
            "month": current_month,
            "request_count": 0,
            "total_seconds": 0.0,
            "estimated_cost_usd": 0.0,
            "last_request": None,
            "last_error": usage.get("last_error"),  # preserve last_error across month rollover
        }

    if outcome == "ok":
        usage["request_count"] = int(usage.get("request_count", 0)) + 1
        usage["total_seconds"] = float(usage.get("total_seconds", 0.0)) + duration_sec
        usage["estimated_cost_usd"] = float(usage.get("estimated_cost_usd", 0.0)) + voice_estimate_cost_usd(duration_sec)

    last_record = {
        "ts": now.isoformat(timespec="seconds"),
        "duration_sec": round(duration_sec, 1),
        "text_preview": (text or "")[:80],
        "outcome": outcome,
    }
    usage["last_request"] = last_record

    if outcome == "error" and error_label:
        usage["last_error"] = {
            "ts": now.isoformat(timespec="seconds"),
            "type": error_label,
            "text_preview": (text or "")[:80],
        }

    state_store.save_voice_usage(usage)


def _title_hash(title: str) -> str:
    """Compact 16-char sha1 prefix used as the key in torrent_titles_cache.

    Collisions at 16 hex chars (64 bits) are astronomical for our cache size
    (max ~5000 entries) — birthday paradox kicks in around 2^32 entries.
    Trade-off: shorter keys → smaller JSON storage; negligible collision risk.
    """
    return hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:16]


_TITLE_CACHE_MAX_ENTRIES = 5000
_TITLE_CACHE_EVICT_BATCH = 500


async def _enrich_top_results_with_metadata(
    results_data: list[dict], max_n: int = 10,
) -> None:
    """Mutate top-N results in place, attaching `parsed_meta` from a GPT
    parse of each title. Cache hits are instant; misses parallelize via
    asyncio.gather so the total wall time for 10 misses is ~one round-trip,
    not 10x sequential.

    Silently no-op when GPT_ENABLED is false — gives a clean fallback path
    (results render with raw titles as before).
    """
    if not GPT_ENABLED or not results_data:
        return
    cache = state_store.load_torrent_titles_cache()
    # Step 1: hit-or-miss classification (in-place attach for hits).
    misses: list[tuple[str, dict]] = []
    for r in results_data[:max_n]:
        title = r.get("title") or ""
        if not title:
            continue
        h = _title_hash(title)
        cached = cache.get(h)
        if isinstance(cached, dict):
            r["parsed_meta"] = cached
        else:
            misses.append((h, r))

    if not misses:
        return

    # Step 2: parallel GPT-parse the misses. Each call carries its own
    # usage_sink so we can plumb the real API-reported token counts back
    # to the per-feature usage tracker (vs hardcoded estimates).
    miss_sinks: list[list] = [[] for _ in misses]

    def _parse_with_sink(title: str, sink: list):
        return gpt_features_parse_torrent_title(
            title=title, api_key=OPENAI_API_KEY, model=GPT_MODEL,
            usage_sink=sink,
        )

    tasks = [
        asyncio.to_thread(_parse_with_sink, r["title"], sink)
        for (_h, r), sink in zip(misses, miss_sinks)
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    # Step 3: attach parsed_meta + update cache + record usage.
    cache_dirty = False
    for (h, r), outcome, sink in zip(misses, outcomes, miss_sinks):
        if isinstance(outcome, Exception):
            logger.warning(
                "parse_torrent_title raised for %r: %s", r.get("title", "")[:60], outcome,
            )
            continue
        meta, err = outcome
        usage = sink[0] if sink else None
        _gpt_record_usage(
            feature="quality_parse",
            input_tokens=200, output_tokens=80,  # fallback only — usage wins
            error_label=err,
            usage=usage,
        )
        if meta:
            r["parsed_meta"] = meta
            cache[h] = meta
            cache_dirty = True

    # Step 4: LRU-cap the cache so disk doesn't grow unbounded.
    if cache_dirty and len(cache) > _TITLE_CACHE_MAX_ENTRIES:
        # Drop oldest (Python dicts preserve insertion order) — simple LRU.
        for old_h in list(cache.keys())[:_TITLE_CACHE_EVICT_BATCH]:
            cache.pop(old_h, None)
        logger.info(
            "Title cache evicted %d oldest entries (size now %d)",
            _TITLE_CACHE_EVICT_BATCH, len(cache),
        )

    if cache_dirty:
        state_store.save_torrent_titles_cache(cache)


async def _gpt_get_did_you_mean(search_query: str) -> list[str]:
    """Return up to 3 GPT-suggested alternative queries, or empty list.

    Cheap to call (~$0.00005 per request, only fires on 0 results). Failures
    silently degrade to empty list — the «no results» screen still works
    with just the existing «без качества» / «все трекеры» / «отмена» buttons.
    """
    if not GPT_ENABLED:
        logger.info("did_you_mean skipped for %r: GPT disabled", search_query)
        return []
    sink: list = []
    try:
        suggestions, error = await asyncio.to_thread(
            gpt_did_you_mean,
            query=search_query,
            api_key=OPENAI_API_KEY,
            model=GPT_MODEL,
            usage_sink=sink,
        )
    except Exception:
        logger.warning("did_you_mean call failed", exc_info=True)
        return []
    _gpt_record_usage(
        feature="did_you_mean",
        input_tokens=100,
        output_tokens=120,  # fallback only — real usage from sink wins
        error_label=error,
        usage=(sink[0] if sink else None),
    )
    if error:
        logger.info("did_you_mean returned no suggestions for %r: %s", search_query, error)
    elif not suggestions:
        logger.info("did_you_mean returned no suggestions for %r", search_query)
    return suggestions


def _format_search_failure_advice(advice: dict | None) -> str:
    if not isinstance(advice, dict):
        return ""
    message = str(advice.get("message") or "").strip()
    if not message:
        return ""
    if len(message) > 180:
        message = message[:177].rstrip() + "..."
    action = str(advice.get("suggested_action") or "").strip()
    hints = {
        "remove_quality": "Лучшее следующее действие: нажать «Искать без качества».",
        "expand_trackers": "Лучшее следующее действие: нажать «Искать на всех трекерах».",
        "retry": "Лучшее следующее действие: повторить поиск через минуту.",
        "try_original_title": "Лучшее следующее действие: попробовать вариант названия ниже.",
        "manual_search": "Лучшее следующее действие: изменить запрос вручную.",
    }
    hint = hints.get(action, "")
    return "\n".join(part for part in (f"🤖 {message}", hint) if part)


async def _gpt_get_search_failure_advice(
    search_query: str,
    *,
    base_query: str = "",
    preferred_quality: str | None = None,
    audio_required: bool = False,
    subs_required: bool = False,
    has_quality: bool = False,
    jackett_can_expand: bool = False,
    season_requested: bool = False,
    source_status: str = "empty",
    suggestions: list[str] | None = None,
) -> dict | None:
    """Return GPT no-results advice, or None on disabled/error fallback."""
    if not GPT_ENABLED:
        return None
    sink: list = []
    try:
        advice, error = await asyncio.to_thread(
            gpt_diagnose_search_failure,
            query=search_query,
            base_query=base_query,
            preferred_quality=preferred_quality,
            audio_required=audio_required,
            subs_required=subs_required,
            has_quality_retry=has_quality,
            can_expand_trackers=jackett_can_expand,
            season_requested=season_requested,
            source_status=source_status,
            suggestions=suggestions or [],
            api_key=OPENAI_API_KEY,
            model=GPT_MODEL,
            usage_sink=sink,
        )
    except Exception:
        logger.warning("search_failure advice call failed", exc_info=True)
        return None
    _gpt_record_usage(
        feature="search_failure",
        input_tokens=220,
        output_tokens=120,
        error_label=error,
        usage=(sink[0] if sink else None),
    )
    if error:
        logger.info("search_failure advice returned no advice for %r: %s", search_query, error)
    return advice


# ─── KP verification for original-query did-you-mean suppression ───────────
#
# One failure mode the bare GPT did-you-mean exhibits:
# User's original query is a REAL title that just wasn't on trackers this
# minute (Jackett timeout, etc). GPT doesn't know it's real, tries to «fix»
# it into a similar-sounding film and confuses the user.
# KP API knows what's real, but keyword search is fuzzy and aliases are messy
# (localized title vs original title vs transliteration). We only use KP to
# suppress did-you-mean when the user's original query plausibly matches the
# returned KP title. GPT suggestions are intentionally not KP-filtered: a bad
# suggestion is cheap, while dropping a useful alias makes the recovery UI fail.

# In-process cache: KP verification of a normalized title is immutable for
# our purposes (films don't un-exist). Saves ~1s per repeat lookup.
_kp_exists_cache: dict[str, bool] = {}
_KP_EXISTS_CACHE_MAX = 500  # rough upper bound; LRU eviction below
_KP_VERIFY_TITLE_MATCH_RATIO = 0.86


def _kp_exists_cache_get(norm_title: str) -> bool | None:
    return _kp_exists_cache.get(norm_title)


def _kp_exists_cache_put(norm_title: str, exists: bool) -> None:
    if len(_kp_exists_cache) >= _KP_EXISTS_CACHE_MAX:
        # Drop oldest 100 entries (insertion-order dict). Rare enough to be ok.
        for old in list(_kp_exists_cache.keys())[:100]:
            _kp_exists_cache.pop(old, None)
    _kp_exists_cache[norm_title] = exists


def _kp_verify_norm_title(title: str) -> str:
    norm = _normalize_movie_title(title or "").lower()
    norm = re.sub(r"\b(?:19|20)\d{2}\b", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def _kp_verify_title_sync(title: str, *, default_on_unknown: bool = True) -> bool:
    """Synchronous KP existence check used inside ``asyncio.to_thread``.

    ``default_on_unknown`` controls what we return when KP can't give a
    definite answer (client is None, network fails, KP API errors out):
      • True  — for callers that want permissive behaviour on flaky infra
      • False — for original-query verification: «don't suppress did-you-mean
        on flaky infra» (otherwise a single KP outage would silently strip
        all suggestions even when the original is a typo)

    Returns:
      • True/False from KP when the check succeeded
      • default_on_unknown when KP is unreachable / not configured
    """
    if kinopoisk_client is None:
        return default_on_unknown
    norm = (title or "").strip().lower()
    if not norm:
        return False
    cached = _kp_exists_cache_get(norm)
    if cached is not None:
        return cached
    try:
        match = kinopoisk_client.search_movie(title)
    except Exception as exc:
        logger.debug("KP verify error for %r: %s — falling back to %s",
                     title, exc, default_on_unknown)
        return default_on_unknown
    exists = _kp_match_plausibly_equals_query(title, match)
    _kp_exists_cache_put(norm, exists)
    logger.debug("KP verify %r → %s (match=%r)", title, exists, match)
    return exists


def _kp_match_plausibly_equals_query(
    query: str,
    match,
) -> bool:
    """Guard against overly fuzzy KP keyword hits.

    KP `search-by-keyword` may return a semantically related title even when
    the queried title itself does not exist. For did-you-mean suppression we
    only want to treat the query as "exists on KP" when the returned title is
    plausibly the same film/series name.
    """
    if match is None:
        return False

    q_norm = _kp_verify_norm_title(query)
    if not q_norm:
        return False
    q_compact = q_norm.replace(" ", "")
    q_tokens = {t for t in q_norm.split() if len(t) >= 3}

    candidates = []
    for raw in (getattr(match, "title_ru", ""), getattr(match, "title_en", ""), getattr(match, "title", "")):
        norm = _kp_verify_norm_title(str(raw))
        if norm:
            candidates.append(norm)

    if not candidates:
        return False

    for cand_norm in candidates:
        cand_compact = cand_norm.replace(" ", "")
        if q_compact == cand_compact:
            return True
        if len(q_compact) >= 6 and len(cand_compact) >= 6:
            if SequenceMatcher(None, q_compact, cand_compact).ratio() >= _KP_VERIFY_TITLE_MATCH_RATIO:
                return True
        cand_tokens = {t for t in cand_norm.split() if len(t) >= 3}
        if q_tokens and cand_tokens:
            overlap = len(q_tokens & cand_tokens) / max(len(q_tokens), len(cand_tokens))
            if overlap >= 0.8:
                return True

    logger.info(
        "KP verify rejected fuzzy hit: query=%r match=%r/%r",
        query, getattr(match, "title_ru", ""), getattr(match, "title_en", ""),
    )
    return False


_EN_TO_RU_TITLE_WORDS = {
    "gangster": "гангстер",
    "land": "ленд",
}

_EN_TO_RU_DIGRAPHS = {
    "sch": "ш",
    "sh": "ш",
    "ch": "ч",
    "ph": "ф",
    "th": "т",
    "ck": "к",
    "ng": "нг",
}

_EN_TO_RU_CHARS = {
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "дж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "и",
    "z": "з",
}


def _capitalize_title_guess(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title[:1].upper() + title[1:] if title else title


def _english_word_to_cyrillic_guess(word: str) -> str:
    lower = word.lower()
    if lower in _EN_TO_RU_TITLE_WORDS:
        return _EN_TO_RU_TITLE_WORDS[lower]

    out = []
    i = 0
    while i < len(lower):
        matched = False
        for src, dst in sorted(_EN_TO_RU_DIGRAPHS.items(), key=lambda item: len(item[0]), reverse=True):
            if lower.startswith(src, i):
                out.append(dst)
                i += len(src)
                matched = True
                break
        if matched:
            continue
        out.append(_EN_TO_RU_CHARS.get(lower[i], lower[i]))
        i += 1
    return "".join(out)


def _english_title_to_cyrillic_guesses(title: str) -> list[str]:
    words = re.findall(r"[A-Za-z]+|\d+", title)
    if not words or not any(re.search(r"[A-Za-z]", word) for word in words):
        return []

    cyr_words = [
        _english_word_to_cyrillic_guess(word) if re.search(r"[A-Za-z]", word) else word
        for word in words
    ]
    variants = ["".join(cyr_words)]
    return [_capitalize_title_guess(value) for value in variants if value.strip()]


def _kp_query_shaped_suggestions(query: str, match) -> list[str]:
    """Guess a direct typo-fix button from KP original titles.

    KP often returns canonical titles that are good aliases but not the most
    ergonomic correction. For a query like "ганстерленд" and KP title_en
    "Gangster Land", the best first button is "Гангстерленд".
    """
    query_compact = _kp_verify_norm_title(query).replace(" ", "")
    if not query_compact:
        return []

    suggestions: list[str] = []
    for raw in (getattr(match, "title_en", ""), getattr(match, "title", "")):
        for guess in _english_title_to_cyrillic_guesses(str(raw or "")):
            guess_norm = _kp_verify_norm_title(guess)
            guess_compact = guess_norm.replace(" ", "")
            if not guess_compact or guess_compact == query_compact:
                continue
            if SequenceMatcher(None, query_compact, guess_compact).ratio() >= 0.82:
                suggestions.append(guess)
    return suggestions


def _kp_suggestion_titles_from_match(query: str, match) -> list[str]:
    """Return direct typo-fix + KP titles as candidates for a loose hit."""
    if match is None or _kp_match_plausibly_equals_query(query, match):
        return []

    seen = {_kp_verify_norm_title(query)}
    suggestions: list[str] = []
    for value in _kp_query_shaped_suggestions(query, match):
        norm = _kp_verify_norm_title(value)
        if norm and norm not in seen:
            seen.add(norm)
            suggestions.append(value)

    for raw in (getattr(match, "title_ru", ""), getattr(match, "title_en", ""), getattr(match, "title", "")):
        value = str(raw or "").strip()
        norm = _kp_verify_norm_title(value)
        if not value or not norm or norm in seen:
            continue
        seen.add(norm)
        suggestions.append(value)
    return suggestions[:3]


def _kp_loose_suggestions_sync(title: str) -> list[str]:
    """Use KP keyword search as a fallback source for typo suggestions.

    This is deliberately separate from `_kp_verify_title_sync`: the same loose
    KP hit must not prove "the user's title exists", but it can still be a
    useful button when GPT returns no variants.
    """
    if kinopoisk_client is None:
        return []
    if not (title or "").strip():
        return []
    try:
        match = kinopoisk_client.search_movie(title)
    except Exception as exc:
        logger.debug("KP loose suggestion failed for %r: %s", title, exc)
        return []
    suggestions = _kp_suggestion_titles_from_match(title, match)
    if suggestions:
        logger.info("KP loose suggestions for %r: %s", title, suggestions)
    return suggestions


def _is_rutracker_only_indexer_set(
    selected: set[str] | None, indexers: list[dict] | None,
) -> bool:
    """Return True if the user's Jackett indexer selection is rutracker-only.

    R.2-fail context: when Jackett times out we fall back to a direct
    Rutracker search. If the user's selection was ONLY rutracker, the
    fallback covers the same source → an empty result from RT-direct
    is a reliable «not found» signal → did-you-mean is appropriate.

    But if the selection included OTHER trackers (kinozal/nnmclub/etc),
    the Jackett failure means we lost that coverage; the RT-direct fallback
    only sees one tracker. An empty result here ISN'T conclusive — could be
    everywhere else. In that case did-you-mean would be misleading; show
    «retry» instead.
    """
    if not selected:
        return True  # treat empty as «default = rutracker-only»
    indexers = indexers or []
    rutracker_ids = {
        str(i.get("id", "")).lower() for i in indexers
        if "rutracker" in str(i.get("id", "")).lower()
    }
    selected_lower = {str(s).lower() for s in selected}
    non_rt = selected_lower - rutracker_ids
    return not non_rt


async def _enrich_top10_with_explanations(cache: dict) -> None:
    """Generate 1-line GPT explanations for the top-10 cards in /new.

    Runs at the end of _refresh_movie_discovery_cache (only if GPT_ENABLED).
    Two-step enrichment per card:
      1. Fetch synopsis from KP if missing (cached in kp_cache forever).
      2. Generate explanation via GPT if missing (also cached forever).

    Both steps gracefully skip on errors — card simply won't have an
    explanation line in the renderer. Already-cached explanations are
    reused, so this is near-free for unchanged top-10s.

    Why only top-10: that's what /new renders. Cards at positions 11-30
    are buffer/spares; generating for them wastes GPT/KP budget on
    content the user may never see. If a buffer card rises into top-10
    on the next refresh, it gets enriched then.
    """
    cards = cache.get("cards") or []
    top10 = cards[:10]
    if not top10:
        return

    kp_cache = cache.get("kp_cache") if isinstance(cache.get("kp_cache"), dict) else {}
    cache["kp_cache"] = kp_cache  # ensure dict back-reference

    generated = 0
    skipped_cached = 0
    for card in top10:
        kp_id = card.get("kp_id")
        if not kp_id:
            continue  # no KP match → nothing to look up / generate against

        # Find the kp_cache entry for this card. kp_cache is keyed by
        # _kp_cache_key(title, year), not by kp_id — iterate to find a
        # match. Small N (top-10) → fine.
        cache_entry = None
        for entry in kp_cache.values():
            if isinstance(entry, dict) and entry.get("kp_id") == kp_id:
                cache_entry = entry
                break
        if cache_entry is None:
            continue

        # Step 1: synopsis (KP call, cached forever).
        synopsis = cache_entry.get("synopsis")
        if synopsis is None and kinopoisk_client is not None:
            try:
                synopsis = await asyncio.to_thread(
                    kinopoisk_client.get_film_synopsis, kp_id,
                )
            except Exception:
                logger.warning("KP synopsis fetch failed for kp_id=%s", kp_id, exc_info=True)
                synopsis = ""  # cache as empty string to avoid re-trying every refresh
            cache_entry["synopsis"] = synopsis

        # Step 2: GPT explanation (cached forever).
        explanation = cache_entry.get("explanation")
        if not explanation:
            explain_sink: list = []
            text, error = await asyncio.to_thread(
                gpt_features_explain_movie_card,
                title=str(card.get("title") or ""),
                year=card.get("year"),
                rating=card.get("rating"),
                genres=card.get("genres") or [],
                synopsis=synopsis or "",
                api_key=OPENAI_API_KEY,
                model=GPT_MODEL,
                usage_sink=explain_sink,
            )
            approx_in = 200 + (len(synopsis or "") // 4)
            _gpt_record_usage(
                feature="explain_card",
                input_tokens=approx_in,
                output_tokens=60,  # fallback only
                error_label=error,
                usage=(explain_sink[0] if explain_sink else None),
            )
            if text:
                explanation = text
                cache_entry["explanation"] = explanation
                generated += 1

        if explanation:
            card["explanation"] = explanation
        else:
            skipped_cached += 1

    logger.info(
        "movie_discovery: top10 explanations — generated=%d cached=%d total_top10=%d",
        generated, len(top10) - generated - skipped_cached, len(top10),
    )


def _gpt_validate_kp_match(query: str, match) -> bool:
    """Ask GPT whether the KP-search result actually matches the torrent query.

    Returns True (= accept the match) when GPT is disabled, when GPT errors
    (we'd rather show possibly-imperfect KP data than nothing), or when GPT
    is confident enough (confidence >= KP_CONFIDENCE_THRESHOLD).

    Returns False (= reject and treat as no-match) only when GPT explicitly
    picks "none" or returns low confidence. This protects /new from showing
    cards with wrong KP rating / title attached to the wrong film.

    Called synchronously from inside _movie_build_cards (which itself runs
    in a worker thread), so blocking HTTP is fine.
    """
    if not GPT_ENABLED or match is None:
        return True

    candidates = [{
        "title_ru": match.title_ru or "",
        "title_en": match.title_en or "",
        "year": match.year,
        "rating": match.rating,
        "genres": match.genres or [],
    }]

    kp_sink: list = []
    pick, confidence, error = gpt_kp_confidence_check(
        query=query,
        candidates=candidates,
        api_key=OPENAI_API_KEY,
        model=GPT_MODEL,
        usage_sink=kp_sink,
    )
    _gpt_record_usage(
        feature="kp_confidence",
        input_tokens=200,
        output_tokens=50,  # fallback only — sink carries real counts
        error_label=error,
        usage=(kp_sink[0] if kp_sink else None),
    )

    if error:
        # GPT unreachable / quota / etc. — fall back to accepting the match
        # so the user doesn't lose KP enrichment due to OpenAI hiccups.
        return True
    return pick is not None


def _gpt_record_usage(
    *,
    feature: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_label: str | None,
    usage: dict | None = None,
) -> None:
    """Track GPT call into the monthly per-feature usage bucket.

    Prefer the real ``usage`` dict (``{input_tokens, output_tokens, model}``)
    plumbed up from ``gpt_features._record_usage`` — that's the API-reported
    counts. The ``input_tokens``/``output_tokens`` positional args are kept
    only as fallback estimates for code paths that don't propagate usage yet
    (or for purely-local-failure calls where no API was hit).

    If the model is unknown to ``MODEL_PRICING``, tokens are still recorded
    but cost is left untouched and a ``cost_unknown_calls`` counter is bumped
    so /admin can flag «cost unknown for model X».

    Counters reset on calendar month rollover; ``last_error`` persists across
    rollover so the operator still sees the last problem after the boundary.
    """
    # Pick the most accurate numbers we have. Real usage wins; falls back to
    # caller estimates so a path without a sink still records something.
    if usage:
        real_in = int(usage.get("input_tokens") or 0)
        real_out = int(usage.get("output_tokens") or 0)
        model = str(usage.get("model") or "gpt-4o-mini")
        is_real = True
    else:
        real_in = max(0, input_tokens)
        real_out = max(0, output_tokens)
        model = "gpt-4o-mini"  # default model used everywhere today
        is_real = False

    now = datetime.now(DISPLAY_TIMEZONE)
    current_month = now.strftime("%Y-%m")
    gpt_usage = state_store.load_gpt_usage()
    if gpt_usage.get("month") != current_month:
        gpt_usage = {
            "month": current_month,
            "features": {},
            "last_error": gpt_usage.get("last_error"),
        }

    features = gpt_usage.setdefault("features", {})
    bucket = features.setdefault(feature, {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "real_usage_calls": 0,
        "estimate_calls": 0,
        "cost_unknown_calls": 0,
    })
    bucket["calls"] = int(bucket.get("calls", 0)) + 1
    bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + real_in
    bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + real_out
    bucket["real_usage_calls"] = int(bucket.get("real_usage_calls", 0)) + (1 if is_real else 0)
    bucket["estimate_calls"] = int(bucket.get("estimate_calls", 0)) + (0 if is_real else 1)

    cost = estimate_chat_cost_usd(real_in, real_out, model=model)
    if cost is None:
        bucket["cost_unknown_calls"] = int(bucket.get("cost_unknown_calls", 0)) + 1
        bucket.setdefault("unknown_models", [])
        if model and model not in bucket["unknown_models"]:
            bucket["unknown_models"].append(model)
    else:
        bucket["estimated_cost_usd"] = float(bucket.get("estimated_cost_usd", 0.0)) + cost

    if error_label:
        gpt_usage["last_error"] = {
            "ts": now.isoformat(timespec="seconds"),
            "feature": feature,
            "type": error_label,
        }
    else:
        gpt_usage.pop("last_error", None)

    state_store.save_gpt_usage(gpt_usage)


async def voice_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ConversationHandler entry point for voice messages.

    Pipeline: voice → download OGG → Whisper API → use transcription as a
    regular search query. We reuse the existing search-options flow, so
    the only difference from typing is the upfront transcription step.

    Hard-coded safeguards: feature-flag (VOICE_SEARCH_ENABLED), max duration
    (VOICE_MAX_SECONDS), graceful degradation on any failure (Whisper down,
    empty transcription, network errors → user sees friendly message,
    nothing is charged to OpenAI for non-200 responses).
    """
    if not _is_allowed(update):
        await _reply_access_pending(update, context)
        return ConversationHandler.END

    voice = update.message.voice
    if voice is None:
        return ConversationHandler.END

    _clear_search_intent(context)

    if not VOICE_SEARCH_ENABLED:
        await update.message.reply_text(
            "🎙 Голосовой поиск не настроен. Пришлите текстовое сообщение."
        )
        return ConversationHandler.END

    if voice.duration and voice.duration > VOICE_MAX_SECONDS:
        await update.message.reply_text(
            f"🎙 Голосовое слишком длинное ({voice.duration}с, лимит {VOICE_MAX_SECONDS}с). "
            "Запишите короче или пришлите текстом."
        )
        return ConversationHandler.END

    status = await update.message.reply_text("🎧 Прослушиваем…")

    # Progressive UI: animated mp4 (spy operators) + stage text update at
    # t+10s so a slow Whisper response doesn't feel like the bot froze.
    voice_progressive: ProgressiveStatus | None = ProgressiveStatus(
        bot=context.bot,
        chat_id=status.chat_id,
        initial_text="🎧 Прослушиваем…",
        stages=voice_stages(),
        gif_path=VOICE_ANIMATION_PATH,
    )
    voice_progressive.text_msg = status  # message already sent above
    try:
        if voice_progressive.gif_path.exists():
            with open(voice_progressive.gif_path, "rb") as fh:
                voice_progressive.gif_msg = await context.bot.send_animation(
                    chat_id=status.chat_id, animation=fh,
                )
    except Exception:
        logger.debug("Voice progressive: gif send failed", exc_info=True)
        voice_progressive.gif_msg = None
    if voice_progressive.stages:
        try:
            voice_progressive._task = asyncio.create_task(voice_progressive._run_stages())
        except RuntimeError:
            voice_progressive._task = None

    async def _finalize_voice_progressive() -> None:
        try:
            await voice_progressive.stop()
        except Exception:
            logger.debug("Voice progressive: stop failed", exc_info=True)

    # Download voice file to a temporary path. Telegram returns OGG/Opus.
    temp_path: Path | None = None
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        safe_voice_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(voice.file_id or "voice"))[:160]
        safe_name = f"voice_{safe_voice_id}.ogg"
        temp_path = _temp_path(safe_name)
        await tg_file.download_to_drive(custom_path=str(temp_path))
    except Exception:
        logger.warning("Failed to download voice file id=%s", voice.file_id, exc_info=True)
        try:
            _voice_record_usage(
                duration_sec=float(voice.duration or 0),
                text="",
                outcome="error",
                error_label="download_failed",
            )
        except Exception:
            logger.warning("Failed to record voice download failure", exc_info=True)
        await _finalize_voice_progressive()
        await _safe_edit_message(
            status,
            "🎙 Не удалось скачать голосовое. Попробуйте ещё раз или напишите текстом.",
        )
        return ConversationHandler.END

    # Whisper call runs in a thread — it's a blocking HTTP request that
    # can take 1-5 seconds; we don't want to block the event loop.
    voice_duration = float(voice.duration or 0)
    try:
        transcription, error_label = await asyncio.to_thread(
            transcribe_audio_detailed, temp_path, OPENAI_API_KEY,
        )
    finally:
        try:
            if temp_path and temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        await _finalize_voice_progressive()

    if not transcription:
        logger.info(
            "Voice transcription failed: chat=%s duration=%ss error=%s",
            _chat_id(update), voice.duration, error_label or "unknown",
        )
        # Record the failure for /admin diagnostics — operators want to know
        # which type of error happened (quota_exceeded, auth, timeout, …).
        try:
            _voice_record_usage(
                duration_sec=voice_duration,
                text="",
                outcome="error",
                error_label=error_label or "unknown",
            )
        except Exception:
            logger.warning("Failed to record voice usage failure", exc_info=True)

        # User-facing message stays friendly; details are for the admin only.
        friendly_hint = (
            "🎙 Не получилось распознать."
        )
        if error_label == "quota_exceeded":
            friendly_hint = (
                "🎙 Голосовой поиск сейчас недоступен из-за лимита сервиса. Напишите текстом."
            )
        elif error_label == "auth":
            friendly_hint = (
                "🎙 Голосовой поиск сейчас не настроен. Напишите текстом."
            )
        elif error_label == "timeout":
            friendly_hint = (
                "🎙 Распознавание отвечает слишком долго. Попробуйте ещё раз через минуту."
            )
        await _safe_edit_message(
            status,
            friendly_hint + " Можно прислать текстом.",
        )
        return ConversationHandler.END

    # Success — record the usage for /admin display.
    try:
        _voice_record_usage(
            duration_sec=voice_duration,
            text=transcription,
            outcome="ok",
            error_label=None,
        )
    except Exception:
        logger.warning("Failed to record voice usage success", exc_info=True)

    logger.info(
        "Voice search: chat=%s duration=%ss → %r",
        _chat_id(update), voice.duration, transcription[:80],
    )

    # Hand off to the existing search flow. We replicate the relevant
    # prefix of `search_got_query` rather than call it directly — the
    # search_got_query expects update.message.text which is None on voice.
    draft = await _parse_search_intent_for_user_text(transcription)

    # Clean up any stale conversation state (matches text_message_entry).
    _cleanup_plex_pending(context.user_data.pop("plex_pending", None))
    for stale_key in (
        "srch_series_query", "srch_series_success_text", "srch_series_success_task_id",
        "srch_picked_quality", "srch_plex_seasons",
    ):
        context.user_data.pop(stale_key, None)

    if rutracker_client is None and jackett_client is None:
        await _safe_edit_message(
            status,
            "🎙 Услышал: «{}»\n\nНо ни Rutracker, ни Jackett не настроены — поиск невозможен."
            .format(html_module.escape(_base_query_from_intent(draft, transcription) or transcription)),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    query_text, search_settings = _apply_intent_to_search_state(
        context,
        draft,
        chat_id=_chat_id(update),
        fallback_text=transcription,
    )
    if _should_auto_start_search(draft):
        await _safe_edit_message(
            status,
            f"🎙 Услышал: «{html_module.escape(transcription)}»\n\n🔎 Запускаю поиск…",
            parse_mode="HTML",
        )
        return await _run_search(
            status.edit_text,
            context,
            _build_current_mode_search_query(context, query_text, search_settings),
        )
    await _safe_edit_message(
        status,
        f"🎙 Услышал: «{html_module.escape(query_text)}»\n\n"
        + _search_options_text(query_text, context, escape_html=True),
        reply_markup=_search_options_keyboard(
            _tracker_label_from_context(context),
            _search_intent(context),
        ),
        parse_mode="HTML",
    )
    context.user_data["srch_ui_msg_id"] = status.message_id
    context.user_data["srch_ui_chat_id"] = update.effective_chat.id
    return SEARCH_OPTIONS


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
    _clear_search_intent(context)

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
    # User typed new text — any in-flight did-you-mean prefetch is stale.
    # (_run_search would cancel it too on mismatch, but explicit cancel here
    # avoids a brief window where the task keeps running.)
    _cancel_didmean_prefetch(context)

    if text.lower() in {
        "настройки",
        "настройки по умолчанию",
        "дефолты",
        "предпочтения",
        "предпочтения поиска",
    }:
        await settings_command(update, context)
        return ConversationHandler.END

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
            error_text = _torrent_file_user_error_text()
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
    global SUBSCRIPTION_MONITOR_TASK, JACKETT_WARMUP_TASK

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
        SUBSCRIPTION_MONITOR_TASK = app.create_task(_subscription_check_loop(app))
        logger.info("Subscription check loop started, interval=%sh", SUBSCRIPTION_CHECK_INTERVAL_HOURS)

    if _movie_discovery_enabled():
        MOVIE_DISCOVERY_TASK = app.create_task(_movie_discovery_loop(app))
        logger.info("Movie discovery loop started, interval=%sh", MOVIE_DISCOVERY_INTERVAL_HOURS)
        # Note: separate pending-loop is no longer needed — the per-user 'seen'
        # diff is naturally self-healing: outside quiet hours we just skip the
        # push; next in-window refresh delivers everything still unseen.

    if _jackett_warmup_enabled():
        JACKETT_WARMUP_TASK = app.create_task(_jackett_warmup_loop(app))
        logger.info("Jackett warmup loop started, interval=%ss", JACKETT_WARMUP_INTERVAL_SECONDS)

    if PLEX_ENABLED:
        app.create_task(_plex_cache_loop(app))
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
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("bulk", series_bulk_command))
    app.add_handler(CommandHandler("continue", series_continue_command))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=f"^{ADMIN_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(access_callback, pattern=f"^{ACCESS_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(task_callback, pattern=f"^{TASK_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(series_continue_callback, pattern=f"^{CONTINUE_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(sub_callback, pattern=f"^{SUB_CALLBACK_PREFIX}:"))
    app.add_handler(CallbackQueryHandler(movie_new_refresh_callback, pattern=r"^new:refresh$"))
    app.add_handler(CallbackQueryHandler(movie_new_close_callback, pattern=r"^new:close$"))
    app.add_handler(CallbackQueryHandler(movie_new_subscribe_callback, pattern=r"^new:subscribe$"))
    app.add_handler(CallbackQueryHandler(movie_new_unsubscribe_callback, pattern=r"^new:unsubscribe$"))
    app.add_handler(CallbackQueryHandler(movie_new_open_callback, pattern=r"^new:open$"))
    app.add_handler(CallbackQueryHandler(movie_new_notification_bulk_confirm, pattern=r"^new:bulk:[0-9a-f]{10}$"))
    app.add_handler(CallbackQueryHandler(movie_new_notification_bulk_run, pattern=r"^new:bulk_ok:[0-9a-f]{10}$"))
    app.add_handler(CallbackQueryHandler(movie_new_notification_push_back, pattern=r"^new:push_back:[0-9a-f]{10}$"))
    app.add_handler(CallbackQueryHandler(help_close_callback, pattern=r"^help:close$"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=rf"^{SETTINGS_CALLBACK_PREFIX}:"))
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
            # Voice messages → Whisper API transcription → same search flow.
            MessageHandler(filters.VOICE, voice_message_entry),
            # Jackett subscription "view results" entry point.
            CallbackQueryHandler(
                search_jackett_check_entry,
                pattern=rf"^{SUB_CALLBACK_PREFIX}:jackett_view:",
            ),
            CallbackQueryHandler(movie_new_show_releases, pattern=r"^new:show:\d+(?::[0-9a-f]{12})?$"),
            CallbackQueryHandler(movie_new_notification_download, pattern=r"^new:dl:[0-9a-f]{10}:\d+$"),
            # Re-run the last search from an error message (conversation already ended).
            CallbackQueryHandler(search_retry, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:retry$"),
            CallbackQueryHandler(search_series_bulk_open, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_open:[A-Za-z0-9_]+$"),
        ],
            states={
                SEARCH_OPTIONS: [
                    CallbackQueryHandler(search_series_bulk_open, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_open:[A-Za-z0-9_]+$"),
                    CallbackQueryHandler(search_choose_mode, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:mode:(options|advanced)$"),
                    CallbackQueryHandler(search_quick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:quick$"),
                    CallbackQueryHandler(search_show_advanced, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:adv$"),
                    CallbackQueryHandler(search_pick_tracker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:pick_tracker:"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                    # New voice → re-transcribe and restart with the new query.
                    MessageHandler(filters.VOICE, voice_message_entry),
                ],
                SEARCH_ADVANCED: [
                    CallbackQueryHandler(search_series_bulk_open, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_open:[A-Za-z0-9_]+$"),
                    CallbackQueryHandler(search_choose_mode, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:mode:(options|advanced)$"),
                    CallbackQueryHandler(search_toggle_setting, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:quality:"),
                    CallbackQueryHandler(search_toggle_setting, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:toggle:"),
                    CallbackQueryHandler(search_do, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:do_search$"),
                    CallbackQueryHandler(search_pick_tracker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:pick_tracker:"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                    # New voice → re-transcribe and restart with the new query.
                    MessageHandler(filters.VOICE, voice_message_entry),
                ],
                SEARCH_RESULTS: [
                    CallbackQueryHandler(search_series_bulk_open, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_open:[A-Za-z0-9_]+$"),
                    CallbackQueryHandler(search_download_pick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:dl_pick:\d+$"),
                    CallbackQueryHandler(search_series_bulk_plan, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_plan:\d+$"),
                    CallbackQueryHandler(search_series_bulk_profile_callback, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_prof:[a-z0-9_]+$"),
                    CallbackQueryHandler(search_series_bulk_build_plan, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_build$", block=False),
                    CallbackQueryHandler(search_series_bulk_review, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_review$"),
                    CallbackQueryHandler(search_series_bulk_pack_list, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_packs$"),
                    CallbackQueryHandler(search_series_bulk_pack_confirm, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_pack_confirm:\d+$"),
                    CallbackQueryHandler(search_series_bulk_pack_run, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_pack_run:\d+$"),
                    CallbackQueryHandler(search_series_bulk_retry, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_retry$"),
                    CallbackQueryHandler(search_series_bulk_soft_search, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_soft$"),
                    CallbackQueryHandler(search_series_bulk_candidate_download, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_cand_dl:\d+$"),
                    CallbackQueryHandler(search_series_bulk_partial_action, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_partial:[a-z_]+$"),
                    CallbackQueryHandler(search_series_bulk_skip, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_skip$"),
                    CallbackQueryHandler(search_series_bulk_confirm, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_confirm$"),
                    CallbackQueryHandler(search_series_bulk_back_plan, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_back_plan$"),
                    CallbackQueryHandler(search_series_bulk_rebuild, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_rebuild$"),
                    CallbackQueryHandler(search_series_bulk_run, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_run$"),
                    CallbackQueryHandler(search_retry, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:retry$"),
                    CallbackQueryHandler(search_direct_download, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:dl:\d+$"),
                    # Partial-series download/notification picker callbacks
                    CallbackQueryHandler(search_subscribe_pick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub_pick:\d+$"),
                    CallbackQueryHandler(search_subscribe_preset, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub_preset:\d+:[a-z]+$"),
                    CallbackQueryHandler(search_subscribe_advanced, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub_advanced:\d+$"),
                    CallbackQueryHandler(search_subscribe_set_notify, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub_set_notify:\d+:[a-z_]+$"),
                    CallbackQueryHandler(search_subscribe_set_download, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub_set_download:\d+:[a-z_]+$"),
                    CallbackQueryHandler(search_subscribe_back_to_results, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:sub_back_results:0$"),
                    CallbackQueryHandler(search_results_page, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:res_page:"),
                    CallbackQueryHandler(search_series_entry, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:series_base$"),
                    CallbackQueryHandler(search_no_quality, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:no_quality$"),
                    CallbackQueryHandler(search_expand_all_trackers, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:expand_all_trackers$"),
                    CallbackQueryHandler(search_no_quality_all_trackers, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:no_quality_all_trackers$"),
                    CallbackQueryHandler(search_didmean, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:didmean:\d+$"),
                    CallbackQueryHandler(search_cluster_back, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cluster_back$"),
                    CallbackQueryHandler(search_pick_cluster, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cluster:"),
                    CallbackQueryHandler(search_retry_dl, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:retry_dl:\d+$"),
                    CallbackQueryHandler(search_queue_dl, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:queue_dl:\d+$"),
                    CallbackQueryHandler(search_switch_trackers, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:switch_trackers$"),
                    CallbackQueryHandler(search_direct_rutracker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:direct_rt$"),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                    CallbackQueryHandler(movie_new_back, pattern=r"^new:back$"),
                    # New text → treat as a fresh query, restarting the flow.
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_entry),
                    # New voice → re-transcribe and restart with the new query.
                    MessageHandler(filters.VOICE, voice_message_entry),
                ],
                SEARCH_SEASON_SELECT: [
                    CallbackQueryHandler(search_series_bulk_open, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_open:[A-Za-z0-9_]+$"),
                    CallbackQueryHandler(search_season_pick, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season:\d+$"),
                    CallbackQueryHandler(search_season_skip, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_skip$"),
                    CallbackQueryHandler(search_season_input_ask, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_input$"),
                    CallbackQueryHandler(search_season_back, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_back$"),
                    CallbackQueryHandler(search_season_back_to_picker, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:season_back_to_picker$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, search_season_got_input),
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
                ],
                SEARCH_JACKETT_SELECT: [
                    CallbackQueryHandler(search_series_bulk_open, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:bulk_open:[A-Za-z0-9_]+$"),
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
                    CallbackQueryHandler(plex_upgrade_download, pattern=r"^plex:upgrade$"),
                    CallbackQueryHandler(plex_cancel_download, pattern=r"^plex:cancel$"),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, search_timeout),
                    CallbackQueryHandler(search_timeout),
                ],
                ConversationHandler.WAITING: [
                    CallbackQueryHandler(search_cancel, pattern=rf"^{SEARCH_CALLBACK_PREFIX}:cancel"),
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
