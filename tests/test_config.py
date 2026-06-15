import re
import unittest
from pathlib import Path

from app_context import build_app_context
from config import env_bool, env_float, env_int, load_settings, parse_chat_ids, parse_statuses


def _project_root() -> Path:
    return Path(__file__).parent.parent


def _env_example_keys() -> set[str]:
    """Все имена переменных из .env.example."""
    path = _project_root() / ".env.example"
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def _compose_env_keys() -> set[str]:
    """Все имена переменных, объявленных в compose.yaml (паттерн ${VAR_NAME...})."""
    path = _project_root() / "compose.yaml"
    content = path.read_text(encoding="utf-8")
    return set(re.findall(r"\$\{([A-Z_]+)[^}]*\}", content))


BASE_ENV = {
    "BOT_TOKEN": "123:token",
    "ALLOWED_CHAT_IDS": "100, 200",
    "DS_URL": "https://nas.example:5001/",
    "DS_ACCOUNT": "tg_bot",
    "DS_PASSWORD": "secret",
    "DS_DESTINATION": "video",
}


class ConfigParsingTests(unittest.TestCase):
    def test_parse_chat_ids_ignores_empty_and_invalid_values(self) -> None:
        self.assertEqual(parse_chat_ids("1, 2, bad, , -3"), {1, 2, -3})

    def test_env_helpers_use_defaults_for_invalid_values(self) -> None:
        env = {
            "COUNT": "not-a-number",
            "RATIO": "bad",
            "DISABLED": "off",
            "ENABLED": "yes",
        }

        self.assertEqual(env_int(env, "COUNT", 7), 7)
        self.assertEqual(env_float(env, "RATIO", 2.5), 2.5)
        self.assertFalse(env_bool(env, "DISABLED", True))
        self.assertTrue(env_bool(env, "ENABLED", False))

    def test_parse_statuses_normalizes_and_skips_blanks(self) -> None:
        self.assertEqual(parse_statuses(" finished, ERROR, ,seeding "), {"finished", "error", "seeding"})

    def test_load_settings_builds_defaults(self) -> None:
        settings = load_settings(BASE_ENV)

        self.assertEqual(settings.bot_token, "123:token")
        self.assertEqual(settings.allowed_chat_ids, {100, 200})
        self.assertEqual(settings.admin_chat_ids, {100, 200})
        self.assertEqual(settings.ds_url, "https://nas.example:5001")
        self.assertTrue(settings.ds_verify_ssl)
        self.assertEqual(settings.max_torrent_file_mb, 20)
        self.assertEqual(settings.max_torrent_file_bytes, 20 * 1024 * 1024)
        self.assertEqual(settings.trackers_background_interval_seconds, 180)
        self.assertEqual(settings.approved_chat_ids_file, Path("/tmp/tg_torrent_drop/approved_chat_ids.json"))
        self.assertEqual(settings.rutracker_max_results, 50)
        self.assertEqual(settings.jackett_max_results, 20)
        self.assertEqual(settings.jackett_fetch_limit, 100)
        self.assertEqual(settings.jackett_search_timeout_seconds, 90.0)
        self.assertTrue(settings.jackett_warmup_enabled)
        self.assertEqual(settings.jackett_warmup_interval_seconds, 900)
        self.assertEqual(settings.jackett_warmup_query, "1080p")
        self.assertEqual(settings.jackett_warmup_indexers, "auto")
        self.assertEqual(settings.jackett_warmup_batch_size, 3)
        self.assertTrue(settings.movie_discovery_enabled)
        self.assertEqual(settings.movie_discovery_interval_hours, 6)
        self.assertEqual(settings.movie_discovery_cache_file, Path("/tmp/tg_torrent_drop/movie_discovery.json"))
        self.assertEqual(settings.movie_discovery_settings_file, Path("/tmp/tg_torrent_drop/movie_discovery_settings.json"))
        self.assertEqual(settings.movie_discovery_debug_file, Path("/tmp/tg_torrent_drop/movie_discovery_debug.json"))
        self.assertEqual(settings.series_bulk_jobs_file, Path("/tmp/tg_torrent_drop/series_bulk_jobs.json"))
        self.assertEqual(settings.series_continue_totals_file, Path("/tmp/tg_torrent_drop/series_continue_totals.json"))
        self.assertEqual(settings.series_continue_hidden_file, Path("/tmp/tg_torrent_drop/series_continue_hidden.json"))
        self.assertEqual(settings.movie_discovery_rutracker_tm, 32)
        self.assertTrue(settings.movie_discovery_jackett_require_date)
        self.assertEqual(settings.movie_discovery_jackett_max_age_days, 32)
        self.assertEqual(settings.movie_discovery_limit, 30)
        self.assertEqual(settings.movie_discovery_min_kp_rating, 6.0)
        self.assertEqual(settings.movie_discovery_qualities, "1080p")
        self.assertFalse(settings.plex_enabled)
        self.assertEqual(settings.plex_url, "")
        self.assertEqual(settings.plex_token, "")
        self.assertEqual(settings.plex_movie_section, "")
        self.assertFalse(settings.plex_webhook_enabled)
        self.assertEqual(settings.plex_webhook_host, "0.0.0.0")
        self.assertEqual(settings.plex_webhook_port, 8099)
        self.assertEqual(settings.plex_webhook_token, "")
        self.assertEqual(settings.plex_webhook_debounce_seconds, 10.0)
        self.assertFalse(settings.tmdb_enabled)
        self.assertEqual(settings.tmdb_api_token, "")

    def test_load_settings_accepts_overrides_and_clamps_values(self) -> None:
        env = {
            **BASE_ENV,
            "ADMIN_CHAT_IDS": "300",
            "STATE_DIR": "/data",
            "DS_VERIFY_SSL": "false",
            "MAX_TORRENT_FILE_MB": "0",
            "TRACKERS_MAX": "-5",
            "TRACKERS_CACHE_TTL_HOURS": "0",
            "TRACKERS_BACKGROUND_INTERVAL_SECONDS": "10",
            "TASK_NOTIFICATION_STATUSES": "finished,error",
            "AUTO_DELETE_FINISHED_AFTER_HOURS": "0,5",
            "RUTRACKER_MAX_RESULTS": "500",
            "JACKETT_MAX_RESULTS": "500",
            "JACKETT_FETCH_LIMIT": "500",
            "JACKETT_SEARCH_TIMEOUT_SECONDS": "500",
            "JACKETT_WARMUP_ENABLED": "false",
            "JACKETT_WARMUP_INTERVAL_SECONDS": "10",
            "JACKETT_WARMUP_QUERY": " test ",
            "JACKETT_WARMUP_INDEXERS": "rutracker,kinozal",
            "JACKETT_WARMUP_BATCH_SIZE": "500",
            "MOVIE_DISCOVERY_ENABLED": "false",
            "MOVIE_DISCOVERY_INTERVAL_HOURS": "0",
            "MOVIE_DISCOVERY_CACHE_FILE": "/cache/new.json",
            "MOVIE_DISCOVERY_DEBUG_FILE": "/cache/new_debug.json",
            "SERIES_BULK_JOBS_FILE": "/cache/series_bulk_jobs.json",
            "SERIES_CONTINUE_TOTALS_FILE": "/cache/series_continue_totals.json",
            "SERIES_CONTINUE_HIDDEN_FILE": "/cache/series_continue_hidden.json",
            "MOVIE_DISCOVERY_RUTRACKER_TM": "7",
            "MOVIE_DISCOVERY_JACKETT_REQUIRE_DATE": "false",
            "MOVIE_DISCOVERY_JACKETT_MAX_AGE_DAYS": "0",
            "MOVIE_DISCOVERY_LIMIT": "500",
            "MOVIE_DISCOVERY_MIN_KP_RATING": "7.2",
            "MOVIE_DISCOVERY_QUALITIES": "2160p",
            "PLEX_URL": "https://example.com/plex",
            "PLEX_TOKEN": "myplextoken",
            "PLEX_WEBHOOK_ENABLED": "true",
            "PLEX_WEBHOOK_HOST": "127.0.0.1",
            "PLEX_WEBHOOK_PORT": "70000",
            "PLEX_WEBHOOK_TOKEN": "hook-token",
            "PLEX_WEBHOOK_DEBOUNCE_SECONDS": "-1",
            "TMDB_API_TOKEN": "tmdb-token",
        }

        settings = load_settings(env)

        self.assertEqual(settings.admin_chat_ids, {300})
        self.assertFalse(settings.ds_verify_ssl)
        self.assertEqual(settings.max_torrent_file_mb, 1)
        self.assertEqual(settings.trackers_max, 0)
        self.assertEqual(settings.trackers_cache_ttl_hours, 1)
        self.assertEqual(settings.trackers_background_interval_seconds, 30)
        self.assertEqual(settings.task_notification_statuses, {"finished", "error"})
        self.assertEqual(settings.auto_delete_finished_after_hours, 0.5)
        self.assertEqual(settings.rutracker_max_results, 50)
        self.assertEqual(settings.jackett_max_results, 50)
        self.assertEqual(settings.jackett_fetch_limit, 200)
        self.assertEqual(settings.jackett_search_timeout_seconds, 180.0)
        self.assertFalse(settings.jackett_warmup_enabled)
        self.assertEqual(settings.jackett_warmup_interval_seconds, 60)
        self.assertEqual(settings.jackett_warmup_query, "test")
        self.assertEqual(settings.jackett_warmup_indexers, "rutracker,kinozal")
        self.assertEqual(settings.jackett_warmup_batch_size, 20)
        self.assertEqual(settings.task_owners_file, Path("/data/task_owners.json"))
        self.assertFalse(settings.movie_discovery_enabled)
        self.assertEqual(settings.movie_discovery_interval_hours, 1)
        self.assertEqual(settings.movie_discovery_cache_file, Path("/cache/new.json"))
        self.assertEqual(settings.movie_discovery_debug_file, Path("/cache/new_debug.json"))
        self.assertEqual(settings.series_bulk_jobs_file, Path("/cache/series_bulk_jobs.json"))
        self.assertEqual(settings.series_continue_totals_file, Path("/cache/series_continue_totals.json"))
        self.assertEqual(settings.series_continue_hidden_file, Path("/cache/series_continue_hidden.json"))
        self.assertEqual(settings.movie_discovery_rutracker_tm, 7)
        self.assertFalse(settings.movie_discovery_jackett_require_date)
        self.assertEqual(settings.movie_discovery_jackett_max_age_days, 1)
        self.assertEqual(settings.movie_discovery_limit, 50)
        self.assertEqual(settings.movie_discovery_min_kp_rating, 7.2)
        self.assertEqual(settings.movie_discovery_qualities, "2160p")
        self.assertTrue(settings.plex_enabled)
        self.assertEqual(settings.plex_url, "https://example.com/plex")
        self.assertEqual(settings.plex_token, "myplextoken")
        self.assertTrue(settings.plex_webhook_enabled)
        self.assertEqual(settings.plex_webhook_host, "127.0.0.1")
        self.assertEqual(settings.plex_webhook_port, 65535)
        self.assertEqual(settings.plex_webhook_token, "hook-token")
        self.assertEqual(settings.plex_webhook_debounce_seconds, 0.0)
        self.assertTrue(settings.tmdb_enabled)
        self.assertEqual(settings.tmdb_api_token, "tmdb-token")

    def test_load_settings_enables_jackett_only_with_complete_credentials(self) -> None:
        settings = load_settings({
            **BASE_ENV,
            "JACKETT_URL": "http://jackett.local:9117/",
            "JACKETT_API_KEY": "secret",
        })

        self.assertTrue(settings.jackett_enabled)
        self.assertEqual(settings.jackett_url, "http://jackett.local:9117")
        self.assertEqual(settings.jackett_api_key, "secret")

    def test_load_settings_rejects_incomplete_jackett_credentials(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "JACKETT_URL and JACKETT_API_KEY"):
            load_settings({**BASE_ENV, "JACKETT_URL": "http://jackett.local:9117"})

        with self.assertRaisesRegex(RuntimeError, "JACKETT_URL and JACKETT_API_KEY"):
            load_settings({**BASE_ENV, "JACKETT_API_KEY": "secret"})

    def test_load_settings_rejects_incomplete_rutracker_credentials(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "RUTRACKER_USERNAME and RUTRACKER_PASSWORD"):
            load_settings({**BASE_ENV, "RUTRACKER_USERNAME": "user"})

        with self.assertRaisesRegex(RuntimeError, "RUTRACKER_USERNAME and RUTRACKER_PASSWORD"):
            load_settings({**BASE_ENV, "RUTRACKER_PASSWORD": "pass"})

    def test_load_settings_reports_missing_required_values(self) -> None:
        env = dict(BASE_ENV)
        env.pop("BOT_TOKEN")

        with self.assertRaisesRegex(RuntimeError, "BOT_TOKEN"):
            load_settings(env)

    def test_load_settings_falls_back_for_invalid_movie_discovery_tm(self) -> None:
        settings = load_settings({**BASE_ENV, "MOVIE_DISCOVERY_RUTRACKER_TM": "99"})

        self.assertEqual(settings.movie_discovery_rutracker_tm, 32)


class AppContextTests(unittest.TestCase):
    def test_build_app_context_wires_configured_clients(self) -> None:
        settings = load_settings({
            **BASE_ENV,
            "RUTRACKER_USERNAME": "user",
            "RUTRACKER_PASSWORD": "pass",
            "JACKETT_URL": "http://jackett.local:9117",
            "JACKETT_API_KEY": "secret",
            "KINOPOISK_API_KEY": "kp-secret",
            "TMDB_API_TOKEN": "tmdb-secret",
        })

        context = build_app_context(settings)

        self.assertIs(context.settings, settings)
        self.assertEqual(context.ds_client.base_url, "https://nas.example:5001")
        self.assertIsNotNone(context.rutracker_client)
        self.assertIsNotNone(context.jackett_client)
        self.assertEqual(context.jackett_client._search_timeout, 90.0)
        self.assertIsNotNone(context.kinopoisk_client)
        self.assertIsNotNone(context.tmdb_client)
        self.assertIsNotNone(context.tvmaze_client)
        self.assertEqual(context.state_store.series_continue_totals_file, settings.series_continue_totals_file)
        self.assertEqual(context.state_store.series_continue_hidden_file, settings.series_continue_hidden_file)
        self.assertEqual(context.state_store.jackett_guard_file, settings.state_dir / "jackett_guard.json")

    def test_build_app_context_leaves_optional_clients_disabled(self) -> None:
        context = build_app_context(load_settings(BASE_ENV))

        self.assertIsNone(context.rutracker_client)
        self.assertIsNone(context.jackett_client)
        self.assertIsNone(context.kinopoisk_client)
        self.assertIsNone(context.tmdb_client)
        self.assertIsNotNone(context.tvmaze_client)


def _dockerfile_copied_files() -> set[str]:
    """Имена файлов, перечисленных в COPY-строке Dockerfile (только корневые .py)."""
    path = _project_root() / "Dockerfile"
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("COPY") and line.endswith("./"):
            # COPY file1.py file2.py ... ./
            parts = line.split()
            return {p for p in parts[1:-1]}  # без COPY и ./
    return set()


def _project_py_files() -> set[str]:
    """Все .py-файлы в корне проекта (не в подпапках)."""
    root = _project_root()
    return {
        f.name
        for f in root.glob("*.py")
        if f.is_file()
    }


class DockerfileCoverageTest(unittest.TestCase):
    """Все .py-файлы в корне проекта должны быть перечислены в COPY-строке Dockerfile.
    Тест поймает ситуацию, когда добавляется новый модуль (например jackett.py),
    но он забывается в Dockerfile — и контейнер упадёт с ModuleNotFoundError."""

    def test_dockerfile_copies_all_project_py_files(self) -> None:
        copied = _dockerfile_copied_files()
        project_files = _project_py_files()
        missing = project_files - copied
        self.assertFalse(
            missing,
            f"Файлы есть в корне проекта, но отсутствуют в COPY-строке Dockerfile: {sorted(missing)}",
        )


def _config_py_env_keys() -> set[str]:
    """Имена переменных окружения, которые читает config.py (паттерн env.get / env_int / env_bool / env_float / required_env)."""
    path = _project_root() / "config.py"
    content = path.read_text(encoding="utf-8")
    return set(re.findall(r'(?:env\.get|env_int|env_bool|env_float|required_env|optional_secret_pair)\s*\(\s*env\s*,\s*"([A-Z_]+)"', content))


class ComposeEnvCoverageTest(unittest.TestCase):
    """Каждая переменная из .env.example должна быть объявлена в compose.yaml.
    Этот тест поймает ситуацию, когда новая переменная добавлена в .env.example
    и config.py, но забыта в compose.yaml — и контейнер её не получит."""

    def test_compose_exposes_all_env_example_variables(self) -> None:
        missing = _env_example_keys() - _compose_env_keys()
        self.assertFalse(
            missing,
            f"Переменные есть в .env.example, но отсутствуют в compose.yaml: {sorted(missing)}",
        )

    def test_env_example_has_no_unknown_variables(self) -> None:
        """Обратная проверка: compose.yaml не объявляет переменные, которых нет в .env.example.
        Помогает обнаружить опечатки в именах переменных в compose.yaml."""
        extra = _compose_env_keys() - _env_example_keys()
        self.assertFalse(
            extra,
            f"Переменные есть в compose.yaml, но отсутствуют в .env.example: {sorted(extra)}",
        )

    def test_env_example_covers_all_config_py_variables(self) -> None:
        """Все переменные окружения, читаемые config.py, должны быть в .env.example.
        Ловит ситуацию, когда новое поле добавлено в AppSettings/load_settings,
        но забыто в .env.example (и значит в compose.yaml тоже)."""
        config_keys = _config_py_env_keys()
        example_keys = _env_example_keys()
        missing = config_keys - example_keys
        self.assertFalse(
            missing,
            f"Переменные читаются в config.py, но отсутствуют в .env.example: {sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
