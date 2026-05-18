import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"


def env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default


def env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)).strip().replace(",", "."))
    except (AttributeError, TypeError, ValueError):
        return default


def env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default

    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def parse_chat_ids(raw: str | None) -> set[int]:
    chat_ids = set()
    for value in (raw or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            chat_ids.add(int(value))
        except ValueError:
            continue

    return chat_ids


def required_env(env: Mapping[str, str], name: str, *, strip: bool = True) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value.strip() if strip else value


def parse_statuses(raw: str) -> set[str]:
    return {status.strip().lower() for status in raw.split(",") if status.strip()}


def optional_secret_pair(
    env: Mapping[str, str],
    first_name: str,
    second_name: str,
    service_name: str,
    *,
    strip_second: bool = True,
) -> tuple[str, str, bool]:
    first = env.get(first_name, "").strip()
    raw_second = env.get(second_name, "")
    second = raw_second.strip() if strip_second else raw_second

    if bool(first) != bool(second):
        raise RuntimeError(
            f"{service_name} settings are incomplete: set both {first_name} and {second_name}, or leave both empty"
        )

    return first, second, bool(first and second)


@dataclass(frozen=True)
class AppSettings:
    bot_token: str
    allowed_chat_ids: set[int]
    admin_chat_ids: set[int]
    access_approvals_enabled: bool
    log_level: str
    tmp_dir: Path
    state_dir: Path
    ds_url: str
    ds_account: str
    ds_password: str
    ds_destination: str
    ds_verify_ssl: bool
    bot_timezone: str
    max_torrent_file_mb: int
    max_torrent_file_bytes: int
    trackers_mode: str
    trackers_url: str
    trackers_max: int
    trackers_cache_ttl_hours: int
    trackers_cache_file: Path
    trackers_background_enabled: bool
    trackers_background_interval_seconds: int
    trackers_processed_file: Path
    task_notifications_enabled: bool
    task_notification_statuses: set[str]
    task_notify_external_tasks: bool
    notify_chat_ids_raw: str
    auto_delete_finished_after_hours: float
    auto_delete_finished_statuses: set[str]
    approved_chat_ids_file: Path
    task_owners_file: Path
    task_meta_file: Path
    notified_tasks_file: Path
    auto_delete_tasks_file: Path
    magnet_poll_attempts: int
    magnet_poll_interval_seconds: float
    ds_retry_attempts: int
    ds_retry_delay: float
    rutracker_username: str
    rutracker_password: str
    rutracker_enabled: bool
    rutracker_max_results: int
    kinopoisk_api_key: str
    kinopoisk_enabled: bool
    plex_enabled: bool
    plex_url: str
    plex_token: str
    plex_movie_section: str
    topic_subscriptions_file: Path
    subscription_check_interval_hours: int
    jackett_url: str
    jackett_api_key: str
    jackett_enabled: bool
    jackett_indexers: str
    jackett_max_results: int
    jackett_fetch_limit: int
    movie_discovery_enabled: bool
    movie_discovery_interval_hours: int
    movie_discovery_cache_file: Path
    movie_discovery_settings_file: Path
    movie_discovery_debug_file: Path
    movie_discovery_rutracker_tm: int
    movie_discovery_jackett_require_date: bool
    movie_discovery_jackett_max_age_days: int
    movie_discovery_limit: int
    movie_discovery_min_kp_rating: float
    movie_discovery_qualities: str
    pending_downloads_enabled: bool
    pending_downloads_interval_seconds: int
    pending_downloads_ttl_hours: float
    pending_downloads_file: Path


def load_settings(env: Mapping[str, str] | None = None) -> AppSettings:
    env = os.environ if env is None else env

    bot_token = required_env(env, "BOT_TOKEN")
    allowed_chat_ids_raw = env.get("ALLOWED_CHAT_IDS") or env.get("ALLOWED_CHAT_ID", "")
    allowed_chat_ids = parse_chat_ids(allowed_chat_ids_raw)
    admin_chat_ids = parse_chat_ids(env.get("ADMIN_CHAT_IDS")) or set(allowed_chat_ids)

    tmp_dir = Path(env.get("TMP_DIR", "/tmp/tg_torrent_drop"))
    state_dir = Path(env.get("STATE_DIR", str(tmp_dir)))
    max_torrent_file_mb = max(1, env_int(env, "MAX_TORRENT_FILE_MB", 20))
    rutracker_username, rutracker_password, rutracker_enabled = optional_secret_pair(
        env,
        "RUTRACKER_USERNAME",
        "RUTRACKER_PASSWORD",
        "Rutracker",
        strip_second=False,
    )
    jackett_url, jackett_api_key, jackett_enabled = optional_secret_pair(
        env,
        "JACKETT_URL",
        "JACKETT_API_KEY",
        "Jackett",
    )

    movie_discovery_rutracker_tm = env_int(env, "MOVIE_DISCOVERY_RUTRACKER_TM", 32)
    if movie_discovery_rutracker_tm not in {-1, 1, 3, 7, 14, 32}:
        movie_discovery_rutracker_tm = 32

    return AppSettings(
        bot_token=bot_token,
        allowed_chat_ids=allowed_chat_ids,
        admin_chat_ids=admin_chat_ids,
        access_approvals_enabled=env_bool(env, "ACCESS_APPROVALS_ENABLED", True),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        tmp_dir=tmp_dir,
        state_dir=state_dir,
        ds_url=required_env(env, "DS_URL").rstrip("/"),
        ds_account=required_env(env, "DS_ACCOUNT"),
        ds_password=required_env(env, "DS_PASSWORD", strip=False),
        ds_destination=required_env(env, "DS_DESTINATION"),
        ds_verify_ssl=env_bool(env, "DS_VERIFY_SSL", True),
        bot_timezone=(env.get("BOT_TIMEZONE") or env.get("TZ") or "Europe/Moscow").strip(),
        max_torrent_file_mb=max_torrent_file_mb,
        max_torrent_file_bytes=max_torrent_file_mb * 1024 * 1024,
        trackers_mode=env.get("TRACKERS_MODE", "auto").strip().lower(),
        trackers_url=env.get("TRACKERS_URL", DEFAULT_TRACKERS_URL).strip(),
        trackers_max=max(0, env_int(env, "TRACKERS_MAX", 20)),
        trackers_cache_ttl_hours=max(1, env_int(env, "TRACKERS_CACHE_TTL_HOURS", 24)),
        trackers_cache_file=Path(env.get("TRACKERS_CACHE_FILE", str(state_dir / "public_trackers.txt"))),
        trackers_background_enabled=env_bool(env, "TRACKERS_BACKGROUND_ENABLED", True),
        trackers_background_interval_seconds=max(30, env_int(env, "TRACKERS_BACKGROUND_INTERVAL_SECONDS", 180)),
        trackers_processed_file=Path(
            env.get("TRACKERS_PROCESSED_FILE", str(state_dir / "trackers_processed_v2.json"))
        ),
        task_notifications_enabled=env_bool(env, "TASK_NOTIFICATIONS_ENABLED", True),
        task_notification_statuses=parse_statuses(env.get("TASK_NOTIFICATION_STATUSES", "finished,seeding,error")),
        task_notify_external_tasks=env_bool(env, "TASK_NOTIFY_EXTERNAL_TASKS", False),
        notify_chat_ids_raw=env.get("NOTIFY_CHAT_IDS", "").strip(),
        auto_delete_finished_after_hours=max(0.0, env_float(env, "AUTO_DELETE_FINISHED_AFTER_HOURS", 24.0)),
        auto_delete_finished_statuses=parse_statuses(env.get("AUTO_DELETE_FINISHED_STATUSES", "finished")),
        approved_chat_ids_file=Path(env.get("APPROVED_CHAT_IDS_FILE", str(state_dir / "approved_chat_ids.json"))),
        task_owners_file=Path(env.get("TASK_OWNERS_FILE", str(state_dir / "task_owners.json"))),
        task_meta_file=Path(env.get("TASK_META_FILE", str(state_dir / "task_meta.json"))),
        notified_tasks_file=Path(env.get("NOTIFIED_TASKS_FILE", str(state_dir / "notified_tasks.json"))),
        auto_delete_tasks_file=Path(env.get("AUTO_DELETE_TASKS_FILE", str(state_dir / "auto_delete_tasks.json"))),
        magnet_poll_attempts=max(1, env_int(env, "MAGNET_POLL_ATTEMPTS", 8)),
        magnet_poll_interval_seconds=max(0.5, env_float(env, "MAGNET_POLL_INTERVAL_SECONDS", 1.5)),
        ds_retry_attempts=max(1, env_int(env, "DS_RETRY_ATTEMPTS", 3)),
        ds_retry_delay=max(0.0, env_float(env, "DS_RETRY_DELAY", 2.0)),
        rutracker_username=rutracker_username,
        rutracker_password=rutracker_password,
        rutracker_enabled=rutracker_enabled,
        rutracker_max_results=max(1, min(50, env_int(env, "RUTRACKER_MAX_RESULTS", 50))),
        kinopoisk_api_key=env.get("KINOPOISK_API_KEY", "").strip(),
        kinopoisk_enabled=bool(env.get("KINOPOISK_API_KEY", "").strip()),
        plex_token=env.get("PLEX_TOKEN", "").strip(),
        plex_url=(env.get("PLEX_URL", "").strip() or ""),
        plex_enabled=bool(env.get("PLEX_URL", "").strip() and env.get("PLEX_TOKEN", "").strip()),
        plex_movie_section=env.get("PLEX_MOVIE_SECTION", "").strip(),
        topic_subscriptions_file=Path(
            env.get("TOPIC_SUBSCRIPTIONS_FILE", str(state_dir / "topic_subscriptions.json"))
        ),
        subscription_check_interval_hours=max(1, env_int(env, "SUBSCRIPTION_CHECK_INTERVAL_HOURS", 6)),
        jackett_url=jackett_url.rstrip("/"),
        jackett_api_key=jackett_api_key,
        jackett_enabled=jackett_enabled,
        jackett_indexers=(env.get("JACKETT_INDEXERS", "all").strip() or "all"),
        jackett_max_results=max(1, min(50, env_int(env, "JACKETT_MAX_RESULTS", 20))),
        jackett_fetch_limit=max(10, min(200, env_int(env, "JACKETT_FETCH_LIMIT", 100))),
        movie_discovery_enabled=env_bool(env, "MOVIE_DISCOVERY_ENABLED", True),
        movie_discovery_interval_hours=max(1, env_int(env, "MOVIE_DISCOVERY_INTERVAL_HOURS", 12)),
        movie_discovery_cache_file=Path(
            env.get("MOVIE_DISCOVERY_CACHE_FILE", str(state_dir / "movie_discovery.json"))
        ),
        movie_discovery_settings_file=Path(
            env.get("MOVIE_DISCOVERY_SETTINGS_FILE", str(state_dir / "movie_discovery_settings.json"))
        ),
        movie_discovery_debug_file=Path(
            env.get("MOVIE_DISCOVERY_DEBUG_FILE", str(state_dir / "movie_discovery_debug.json"))
        ),
        movie_discovery_rutracker_tm=movie_discovery_rutracker_tm,
        movie_discovery_jackett_require_date=env_bool(env, "MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE", True),
        movie_discovery_jackett_max_age_days=max(1, env_int(env, "MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS", 32)),
        movie_discovery_limit=max(1, min(50, env_int(env, "MOVIE_DISCOVERY_LIMIT", 20))),
        movie_discovery_min_kp_rating=max(0.0, env_float(env, "MOVIE_DISCOVERY_MIN_KP_RATING", 6.0)),
        movie_discovery_qualities=(env.get("MOVIE_DISCOVERY_QUALITIES", "1080p").strip() or "1080p"),
        pending_downloads_enabled=env_bool(env, "PENDING_DOWNLOADS_ENABLED", True),
        pending_downloads_interval_seconds=max(60, env_int(env, "PENDING_DOWNLOADS_INTERVAL_SECONDS", 300)),
        pending_downloads_ttl_hours=max(0.1, env_float(env, "PENDING_DOWNLOADS_TTL_HOURS", 24.0)),
        pending_downloads_file=Path(
            env.get("PENDING_DOWNLOADS_FILE", str(state_dir / "pending_downloads.json"))
        ),
    )
