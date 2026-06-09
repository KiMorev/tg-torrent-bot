#!/usr/bin/env python3
"""Interactive setup wizard for PlexLoader.

The file is intentionally standalone: install.sh downloads only this script and
compose.yaml, then runs the wizard on a NAS before the bot container exists.
Keep dependencies in the Python standard library.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_INSTALL_DIR = "/volume1/docker/plexloader"
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_DS_URL = "https://host.docker.internal:5001"
DEFAULT_DS_DESTINATION = "video"
DEFAULT_JACKETT_URL = "http://host.docker.internal:9117"
DEFAULT_PLEX_URL = "http://host.docker.internal:32400"
PLEX_PRODUCT = "PlexLoader"
PLEX_PINS_URL = "https://plex.tv/api/v2/pins"
PLEX_AUTH_URL = "https://app.plex.tv/auth#?"
PLEX_RESOURCES_URL = "https://plex.tv/api/resources"

SAFE_ENV_RE = re.compile(r"^[A-Za-z0-9_./:@,+-]*$")
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class WizardError(RuntimeError):
    pass


class ProbeError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ChatCandidate:
    chat_id: int
    label: str


@dataclass(frozen=True)
class PlexPin:
    pin_id: str
    code: str
    auth_url: str


@dataclass(frozen=True)
class InstallerConfig:
    bot_token: str
    allowed_chat_ids: str
    admin_chat_ids: str
    ds_url: str
    ds_account: str
    ds_password: str
    ds_destination: str
    ds_verify_ssl: bool
    timezone: str = DEFAULT_TIMEZONE
    state_dir: str = "/data"
    access_approvals_enabled: bool = True
    rutracker_username: str = ""
    rutracker_password: str = ""
    jackett_url: str = ""
    jackett_api_key: str = ""
    jackett_indexers: str = "all"
    kinopoisk_api_key: str = ""
    tmdb_api_token: str = ""
    movie_discovery_enabled: bool = False
    plex_url: str = ""
    plex_token: str = ""
    plex_movie_section: str = ""
    plex_deeplink_base_url: str = ""
    plex_auth_client_id: str = ""
    openai_api_key: str = ""
    voice_search_enabled: bool = False
    gpt_enabled: bool = False

    def validate(self) -> None:
        required = {
            "BOT_TOKEN": self.bot_token,
            "ALLOWED_CHAT_IDS": self.allowed_chat_ids,
            "ADMIN_CHAT_IDS": self.admin_chat_ids,
            "DS_URL": self.ds_url,
            "DS_ACCOUNT": self.ds_account,
            "DS_PASSWORD": self.ds_password,
            "DS_DESTINATION": self.ds_destination,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise WizardError("Не заполнены обязательные поля: " + ", ".join(missing))
        if bool(self.rutracker_username.strip()) != bool(self.rutracker_password.strip()):
            raise WizardError("Rutracker: задайте и логин, и пароль, либо оставьте оба пустыми")
        if bool(self.jackett_url.strip()) != bool(self.jackett_api_key.strip()):
            raise WizardError("Jackett: задайте и URL, и API key, либо оставьте оба пустыми")
        if bool(self.plex_url.strip()) != bool(self.plex_token.strip()):
            raise WizardError("Plex: задайте и URL, и token, либо оставьте оба пустыми")
        if self.movie_discovery_enabled and not (
            self.rutracker_username.strip() or self.jackett_url.strip()
        ):
            raise WizardError("/new: нужен хотя бы Rutracker или Jackett")


class Console:
    """TTY-backed console so prompts work even when install.sh came from curl | sh."""

    def __init__(self) -> None:
        self._tty = None
        try:
            self._tty = open("/dev/tty", "r+", encoding="utf-8")
        except OSError:
            self._tty = None
        self._in = self._tty or sys.stdin
        self._out = self._tty or sys.stdout

    def close(self) -> None:
        if self._tty is not None:
            self._tty.close()

    def write(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    def ask(self, prompt: str, *, default: str = "", secret: bool = False) -> str:
        suffix = " [оставить прежнее]" if secret and default else (f" [{default}]" if default else "")
        full_prompt = f"{prompt}{suffix}: "
        if secret:
            try:
                value = getpass.getpass(full_prompt, stream=self._out)
            except (EOFError, OSError):
                value = ""
        else:
            self._out.write(full_prompt)
            self._out.flush()
            value = self._in.readline()
            if value == "":
                value = ""
            else:
                value = value.rstrip("\r\n")
        if not value and default:
            return default
        return value.strip()

    def ask_required(self, prompt: str, *, default: str = "", secret: bool = False) -> str:
        while True:
            value = self.ask(prompt, default=default, secret=secret)
            if value:
                return value
            self.write("Значение обязательно. Если хотите пропустить шаг, остановите установку Ctrl+C.")

    def ask_yes_no(self, prompt: str, *, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.ask(f"{prompt} ({hint})").lower()
            if not answer:
                return default
            if answer in {"y", "yes", "д", "да"}:
                return True
            if answer in {"n", "no", "н", "нет"}:
                return False
            self.write("Ответьте yes/no.")


def env_bool_value(value: str | None, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def parse_env_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        inner = text[1:-1]
        return inner.replace("\\'", "'").replace("\\\\", "\\")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        values[match.group(1)] = parse_env_value(match.group(2))
    return values


def normalize_ds_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url
    return url


def normalize_service_url(value: str, *, default_scheme: str = "http") -> str:
    url = value.strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = f"{default_scheme}://" + url
    return url


def installer_probe_url(ds_url: str) -> str:
    """Return URL the wizard should probe from where it currently runs.

    The generated DS_URL must be reachable from the final bot container. On
    Synology that is usually host.docker.internal, because compose.yaml maps it
    to Docker's host gateway. If the wizard itself runs directly on the NAS
    host, host.docker.internal may not resolve there, so probe DSM via
    127.0.0.1 while keeping the generated .env value container-friendly.
    """
    url = normalize_ds_url(ds_url)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.hostname == "host.docker.internal"
        and os.environ.get("PLEXLOADER_WIZARD_IN_DOCKER") != "1"
    ):
        host = "127.0.0.1"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit((
            parsed.scheme,
            host,
            parsed.path,
            parsed.query,
            parsed.fragment,
        ))
    return url


def detect_timezone() -> str:
    for env_name in ("TZ", "BOT_TIMEZONE"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    try:
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            target = os.readlink(localtime)
            marker = "/zoneinfo/"
            if marker in target:
                return target.split(marker, 1)[1]
    except OSError:
        pass
    return DEFAULT_TIMEZONE


def parse_chat_ids(value: str) -> list[int]:
    ids: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def extract_chat_candidates(updates_payload: dict[str, Any]) -> list[ChatCandidate]:
    result = updates_payload.get("result")
    if not isinstance(result, list):
        return []

    seen: set[int] = set()
    candidates: list[ChatCandidate] = []
    for update in result:
        if not isinstance(update, dict):
            continue
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or chat_id in seen:
            continue
        seen.add(chat_id)
        label_parts = [
            str(chat.get("title") or "").strip(),
            str(chat.get("first_name") or "").strip(),
            str(chat.get("last_name") or "").strip(),
            ("@" + str(chat.get("username")).strip()) if chat.get("username") else "",
        ]
        label = " ".join(part for part in label_parts if part).strip() or str(chat_id)
        candidates.append(ChatCandidate(chat_id=chat_id, label=label))
    return candidates


def write_hint(
    console: Console,
    title: str,
    *,
    why: str,
    where: str,
    example: str,
    skip: str,
) -> None:
    console.write("")
    console.write(title)
    console.write(f"Зачем: {why}")
    console.write(f"Где взять: {where}")
    console.write(f"Пример: {example}")
    console.write(f"Можно пропустить: {skip}")


def default_enabled(env: dict[str, str], *keys: str, flag: str = "") -> bool:
    if flag and flag in env:
        return env_bool_value(env.get(flag), False)
    return any(env.get(key, "").strip() for key in keys)


def ask_optional_features(console: Console, env: dict[str, str]) -> dict[str, bool]:
    console.write("Что включить")
    console.write("Базовый бот, Telegram-доступ и Download Station обязательны.")
    features = {
        "rutracker": console.ask_yes_no(
            "Включить прямой Rutracker-поиск и fallback скачивания?",
            default=default_enabled(env, "RUTRACKER_USERNAME", "RUTRACKER_PASSWORD"),
        ),
        "jackett": console.ask_yes_no(
            "Включить Jackett для широкого поиска по индексерам?",
            default=default_enabled(env, "JACKETT_URL", "JACKETT_API_KEY"),
        ),
        "plex": console.ask_yes_no(
            "Включить Plex-проверки дублей и кнопку просмотра?",
            default=default_enabled(env, "PLEX_URL", "PLEX_TOKEN"),
        ),
        "movie_discovery": console.ask_yes_no(
            "Включить /new — подборку свежих фильмов?",
            default=default_enabled(env, flag="MOVIE_DISCOVERY_ENABLED"),
        ),
        "kinopoisk": console.ask_yes_no(
            "Включить Кинопоиск API для ссылок и обогащения карточек?",
            default=default_enabled(env, "KINOPOISK_API_KEY"),
        ),
        "tmdb": console.ask_yes_no(
            "Включить TMDB API для точных эпизодов в /continue?",
            default=default_enabled(env, "TMDB_API_TOKEN"),
        ),
        "openai": console.ask_yes_no(
            "Включить OpenAI для голосового поиска и GPT-подсказок?",
            default=default_enabled(env, "OPENAI_API_KEY"),
        ),
    }
    if features["movie_discovery"] and not (features["rutracker"] or features["jackett"]):
        console.write("/new нужен хотя бы один источник релизов: Rutracker или Jackett.")
        features["rutracker"] = console.ask_yes_no(
            "Включить Rutracker как источник для /new?", default=True
        )
        if not features["rutracker"]:
            features["jackett"] = console.ask_yes_no(
                "Включить Jackett как источник для /new?", default=True
            )
        if not (features["rutracker"] or features["jackett"]):
            console.write("/new отключён: источник релизов не выбран.")
            features["movie_discovery"] = False
    return features


def format_env_value(value: object) -> str:
    text = str(value)
    if text == "":
        return ""
    if SAFE_ENV_RE.fullmatch(text):
        return text
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_env(config: InstallerConfig, previous_env: dict[str, str] | None = None) -> str:
    config.validate()
    entries: list[tuple[str, object]] = [
        ("BOT_TOKEN", config.bot_token),
        ("ALLOWED_CHAT_IDS", config.allowed_chat_ids),
        ("ADMIN_CHAT_IDS", config.admin_chat_ids),
        ("ACCESS_APPROVALS_ENABLED", str(config.access_approvals_enabled).lower()),
        ("LOG_LEVEL", "INFO"),
        ("TZ", config.timezone),
        ("BOT_TIMEZONE", config.timezone),
        ("STATE_DIR", config.state_dir),
        ("DS_URL", config.ds_url),
        ("DS_ACCOUNT", config.ds_account),
        ("DS_PASSWORD", config.ds_password),
        ("DS_DESTINATION", config.ds_destination),
        ("DS_VERIFY_SSL", str(config.ds_verify_ssl).lower()),
        ("TRACKERS_MODE", "auto"),
        ("TASK_NOTIFICATIONS_ENABLED", "true"),
        ("TASK_NOTIFICATION_STATUSES", "finished,seeding,error"),
        ("AUTO_DELETE_FINISHED_AFTER_HOURS", "24"),
        ("AUTO_DELETE_FINISHED_STATUSES", "finished"),
        ("PENDING_DOWNLOADS_ENABLED", "true"),
        ("PENDING_DOWNLOADS_INTERVAL_SECONDS", "300"),
        ("PENDING_DOWNLOADS_TTL_HOURS", "24"),
        ("MOVIE_DISCOVERY_ENABLED", str(config.movie_discovery_enabled).lower()),
        ("RUTRACKER_USERNAME", config.rutracker_username),
        ("RUTRACKER_PASSWORD", config.rutracker_password),
        ("JACKETT_URL", config.jackett_url),
        ("JACKETT_API_KEY", config.jackett_api_key),
        ("JACKETT_INDEXERS", config.jackett_indexers or "all"),
        ("KINOPOISK_API_KEY", config.kinopoisk_api_key),
        ("TMDB_API_TOKEN", config.tmdb_api_token),
        ("PLEX_URL", config.plex_url),
        ("PLEX_TOKEN", config.plex_token),
        ("PLEX_MOVIE_SECTION", config.plex_movie_section),
        ("PLEX_DEEPLINK_BASE_URL", config.plex_deeplink_base_url),
        ("PLEX_AUTH_CLIENT_ID", config.plex_auth_client_id),
        ("OPENAI_API_KEY", config.openai_api_key),
        ("VOICE_SEARCH_ENABLED", str(bool(config.openai_api_key and config.voice_search_enabled)).lower()),
        ("GPT_ENABLED", str(bool(config.openai_api_key and config.gpt_enabled)).lower()),
    ]
    lines = [
        "# Generated by PlexLoader setup wizard.",
        "# Rerun the wizard to enable integrations without rebuilding the image.",
    ]
    lines.extend(f"{key}={format_env_value(value)}" for key, value in entries)
    generated_keys = {key for key, _ in entries}
    preserved = [
        (key, value)
        for key, value in (previous_env or {}).items()
        if key not in generated_keys
    ]
    if preserved:
        lines.append("")
        lines.append("# Preserved existing values not managed by the wizard.")
        lines.extend(f"{key}={format_env_value(value)}" for key, value in preserved)
    return "\n".join(lines) + "\n"


def write_env_file(path: Path, config: InstallerConfig, previous_env: dict[str, str] | None = None) -> None:
    path.write_text(render_env(config, previous_env), encoding="utf-8", newline="\n")


def _ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    return None if verify_ssl else ssl._create_unverified_context()


def _read_json_url(
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    verify_ssl: bool = True,
    timeout: int = 15,
) -> Any:
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method="POST" if data else "GET",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context(verify_ssl)
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        kind = "auth" if exc.code in {401, 403} else "http"
        raise ProbeError(f"HTTP {exc.code}", kind=kind) from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        kind = "ssl" if "CERTIFICATE_VERIFY_FAILED" in reason else "network"
        raise ProbeError(reason, kind=kind) from exc
    except ssl.SSLError as exc:
        raise ProbeError(str(exc), kind="ssl") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError("Сервис вернул не JSON-ответ", kind="parse") from exc
    return payload


def _read_xml_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    verify_ssl: bool = True,
    timeout: int = 15,
) -> ET.Element:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context(verify_ssl)
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        kind = "auth" if exc.code in {401, 403} else "http"
        raise ProbeError(f"HTTP {exc.code}", kind=kind) from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        kind = "ssl" if "CERTIFICATE_VERIFY_FAILED" in reason else "network"
        raise ProbeError(reason, kind=kind) from exc
    except ssl.SSLError as exc:
        raise ProbeError(str(exc), kind="ssl") from exc

    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ProbeError("Сервис вернул не XML-ответ", kind="parse") from exc


def telegram_api(token: str, method: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/{method}{query}"
    payload = _read_json_url(url)
    if not isinstance(payload, dict):
        raise ProbeError("Telegram API вернул неожиданный ответ", kind="parse")
    if not payload.get("ok"):
        description = payload.get("description") or "Telegram API вернул ошибку"
        raise ProbeError(str(description), kind="auth")
    return payload


def validate_bot_token(token: str) -> str:
    payload = telegram_api(token, "getMe")
    user = payload.get("result") or {}
    if not isinstance(user, dict) or not user.get("username"):
        raise ProbeError("Telegram token принят, но имя бота не найдено", kind="parse")
    return str(user["username"])


def url_with_query(url: str, params: dict[str, str]) -> str:
    separator = "&" if "?" in url else "?"
    return url + separator + urllib.parse.urlencode(params)


def destination_share_name(destination: str) -> str:
    value = destination.strip().strip("/")
    if not value:
        return ""
    parts = [part for part in value.split("/") if part]
    if len(parts) >= 2 and re.fullmatch(r"volume\d+", parts[0], flags=re.IGNORECASE):
        return parts[1]
    return parts[0]


def probe_download_destination(
    base: str,
    sid: str,
    destination: str,
    *,
    verify_ssl: bool,
) -> None:
    share_name = destination_share_name(destination)
    if not share_name:
        raise ProbeError("Папка назначения Download Station пустая", kind="destination")
    try:
        payload = _read_json_url(
            url_with_query(
                f"{base}/webapi/entry.cgi",
                {
                    "api": "SYNO.FileStation.List",
                    "version": "2",
                    "method": "list_share",
                    "_sid": sid,
                },
            ),
            verify_ssl=verify_ssl,
            timeout=10,
        )
    except ProbeError as exc:
        if exc.kind in {"auth", "http"}:
            return
        raise
    if not isinstance(payload, dict):
        return
    if not payload.get("success"):
        return
    shares = (payload.get("data") or {}).get("shares")
    if not isinstance(shares, list):
        return
    names = {
        str(share.get("name") or "").strip()
        for share in shares
        if isinstance(share, dict)
    }
    if names and share_name not in names:
        raise ProbeError(
            f"Папка назначения '{destination}' не найдена среди shared folders DSM",
            kind="destination",
        )


def probe_download_station(
    ds_url: str,
    account: str,
    password: str,
    destination: str,
    *,
    verify_ssl: bool,
) -> None:
    base = installer_probe_url(ds_url)
    login_payload = _read_json_url(
        f"{base}/webapi/auth.cgi",
        data={
            "api": "SYNO.API.Auth",
            "version": "6",
            "method": "login",
            "account": account,
            "passwd": password,
            "session": "DownloadStation",
            "format": "sid",
        },
        verify_ssl=verify_ssl,
    )
    if not isinstance(login_payload, dict):
        raise ProbeError("DSM вернул неожиданный ответ", kind="parse")
    if not login_payload.get("success"):
        code = (login_payload.get("error") or {}).get("code", "unknown")
        raise ProbeError(f"DSM не принял логин или пароль (код {code})", kind="auth")
    sid = str((login_payload.get("data") or {}).get("sid") or "")
    if not sid:
        raise ProbeError("DSM не вернул session id", kind="auth")

    try:
        tasks_url = (
            f"{base}/webapi/DownloadStation/task.cgi?"
            + urllib.parse.urlencode({
                "api": "SYNO.DownloadStation.Task",
                "version": "2",
                "method": "list",
                "limit": "1",
                "_sid": sid,
            })
        )
        tasks_payload = _read_json_url(tasks_url, verify_ssl=verify_ssl)
        if not isinstance(tasks_payload, dict):
            raise ProbeError("Download Station вернул неожиданный ответ", kind="parse")
        if not tasks_payload.get("success"):
            code = (tasks_payload.get("error") or {}).get("code", "unknown")
            raise ProbeError(
                f"Логин работает, но Download Station недоступен этому пользователю (код {code})",
                kind="auth",
            )
        probe_download_destination(base, sid, destination, verify_ssl=verify_ssl)
    finally:
        logout_url = (
            f"{base}/webapi/auth.cgi?"
            + urllib.parse.urlencode({
                "api": "SYNO.API.Auth",
                "version": "6",
                "method": "logout",
                "session": "DownloadStation",
                "_sid": sid,
            })
        )
        try:
            _read_json_url(logout_url, verify_ssl=verify_ssl, timeout=5)
        except ProbeError:
            pass


def resolve_download_station_ssl(
    ds_url: str,
    account: str,
    password: str,
    destination: str,
    console: Console,
) -> bool:
    try:
        probe_download_station(ds_url, account, password, destination, verify_ssl=True)
        console.write("Download Station доступен, SSL-сертификат принят.")
        return True
    except ProbeError as exc:
        if exc.kind != "ssl":
            raise
        console.write("DSM использует сертификат, которому эта система не доверяет.")
        console.write("Пробую безопасный для домашней сети fallback: DS_VERIFY_SSL=false.")
        probe_download_station(ds_url, account, password, destination, verify_ssl=False)
        console.write("Download Station доступен без проверки SSL.")
        return False


def probe_jackett(url: str, api_key: str) -> list[dict[str, str]]:
    payload = _read_json_url(
        url_with_query(
            f"{normalize_service_url(url)}/api/v2.0/indexers",
            {"apikey": api_key, "configured": "true"},
        ),
        timeout=10,
    )
    if not isinstance(payload, list):
        raise ProbeError("Jackett вернул неожиданный ответ", kind="parse")
    indexers: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("configured"):
            continue
        indexer_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or indexer_id).strip()
        if indexer_id:
            indexers.append({"id": indexer_id, "name": name})
    return indexers


def probe_plex(url: str, token: str) -> list[dict[str, str]]:
    base = normalize_service_url(url)
    headers = {"X-Plex-Token": token}
    _read_xml_url(f"{base}/identity", headers=headers, timeout=10)
    root = _read_xml_url(f"{base}/library/sections", headers=headers, timeout=10)
    sections: list[dict[str, str]] = []
    for directory in root.findall("Directory"):
        section_type = str(directory.get("type") or "").strip()
        if section_type not in {"movie", "show"}:
            continue
        key = str(directory.get("key") or "").strip()
        title = str(directory.get("title") or key).strip()
        if key:
            sections.append({"key": key, "title": title, "type": section_type})
    return sections


def plex_auth_client_id(env: dict[str, str]) -> str:
    existing = env.get("PLEX_AUTH_CLIENT_ID", "").strip()
    return existing or str(uuid.uuid4())


def plex_auth_fields(client_id: str) -> dict[str, str]:
    return {
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Client-Identifier": client_id,
    }


def build_plex_auth_url(client_id: str, code: str) -> str:
    return PLEX_AUTH_URL + urllib.parse.urlencode({
        "clientID": client_id,
        "code": code,
        "context[device][product]": PLEX_PRODUCT,
    })


def create_plex_pin(client_id: str) -> PlexPin:
    payload = _read_json_url(
        PLEX_PINS_URL,
        data={"strong": "true", **plex_auth_fields(client_id)},
        headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if not isinstance(payload, dict):
        raise ProbeError("Plex вернул неожиданный ответ при создании PIN", kind="parse")
    pin_id = str(payload.get("id") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not pin_id or not code:
        raise ProbeError("Plex не вернул PIN id/code", kind="parse")
    return PlexPin(pin_id=pin_id, code=code, auth_url=build_plex_auth_url(client_id, code))


def check_plex_pin(pin: PlexPin, client_id: str) -> str:
    payload = _read_json_url(
        url_with_query(
            f"{PLEX_PINS_URL}/{urllib.parse.quote(pin.pin_id)}",
            {"code": pin.code, "X-Plex-Client-Identifier": client_id},
        ),
        headers={"accept": "application/json", **plex_auth_fields(client_id)},
        timeout=10,
    )
    if not isinstance(payload, dict):
        raise ProbeError("Plex вернул неожиданный ответ при проверке PIN", kind="parse")
    return str(payload.get("authToken") or "").strip()


def poll_plex_pin(
    pin: PlexPin,
    client_id: str,
    console: Console,
    *,
    timeout_seconds: int = 90,
    interval_seconds: float = 2.0,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        token = check_plex_pin(pin, client_id)
        if token:
            return token
        if time.monotonic() >= deadline:
            raise ProbeError("Plex auth не завершён: token не появился до timeout", kind="timeout")
        console.write("Жду подтверждения Plex...")
        time.sleep(interval_seconds)


def run_plex_pin_auth(console: Console, client_id: str) -> str:
    pin = create_plex_pin(client_id)
    console.write("")
    console.write("Plex auth")
    console.write("Откройте ссылку в браузере, войдите в Plex и подтвердите доступ:")
    console.write(pin.auth_url)
    console.ask("После подтверждения нажмите Enter")
    return poll_plex_pin(pin, client_id, console)


def probe_plex_resources(token: str, client_id: str) -> list[dict[str, str]]:
    root = _read_xml_url(
        url_with_query(PLEX_RESOURCES_URL, {"includeHttps": "1", "includeRelay": "1"}),
        headers={"X-Plex-Token": token, **plex_auth_fields(client_id)},
        timeout=15,
    )
    resources: list[dict[str, str]] = []
    for device in root.findall("Device"):
        provides = str(device.get("provides") or "")
        if "server" not in {part.strip() for part in provides.split(",")}:
            continue
        server_token = str(device.get("accessToken") or token).strip()
        server_name = str(device.get("name") or device.get("clientIdentifier") or "Plex Server").strip()
        for connection in device.findall("Connection"):
            uri = str(connection.get("uri") or "").strip().rstrip("/")
            if not uri:
                continue
            resources.append({
                "name": server_name,
                "uri": uri,
                "token": server_token,
                "local": str(connection.get("local") or ""),
                "relay": str(connection.get("relay") or ""),
            })
    return resources


def probe_kinopoisk(api_key: str) -> None:
    _read_json_url(
        "https://kinopoiskapiunofficial.tech/api/v2.2/films/301",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        timeout=10,
    )


def probe_tmdb(api_token: str) -> None:
    _read_json_url(
        "https://api.themoviedb.org/3/configuration",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_token}"},
        timeout=10,
    )


def probe_openai(api_key: str) -> None:
    _read_json_url(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )


def choose_chat_id(token: str, bot_username: str, console: Console) -> str:
    console.write("")
    console.write("Теперь нужно определить ваш Telegram chat_id.")
    console.write(f"Откройте @{bot_username} в Telegram и отправьте ему /start.")
    console.ask("После отправки /start нажмите Enter")

    try:
        updates = telegram_api(token, "getUpdates", {"limit": "20", "timeout": "1"})
        candidates = extract_chat_candidates(updates)
    except ProbeError as exc:
        console.write(f"Не удалось прочитать getUpdates: {exc}")
        candidates = []

    if not candidates:
        console.write("Автоматически получить chat_id не удалось.")
        return console.ask_required("Введите ваш Telegram chat_id вручную")

    if len(candidates) == 1:
        candidate = candidates[0]
        console.write(f"Нашёл chat_id: {candidate.chat_id} ({candidate.label})")
        return str(candidate.chat_id)

    console.write("Нашёл несколько чатов:")
    for idx, candidate in enumerate(candidates, start=1):
        console.write(f"{idx}. {candidate.chat_id} — {candidate.label}")
    while True:
        answer = console.ask("Выберите номер чата", default="1")
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(candidates):
            return str(candidates[index - 1].chat_id)
        console.write("Введите номер из списка.")


def ask_optional_key(
    console: Console,
    prompt: str,
    *,
    default: str = "",
    secret: bool = True,
) -> str:
    return console.ask_required(prompt, default=default, secret=secret).strip()


def keep_or_retry(console: Console, service: str, exc: ProbeError) -> str:
    console.write(f"{service} не прошёл проверку: {exc}")
    if console.ask_yes_no("Сохранить настройки всё равно?", default=False):
        return "keep"
    if console.ask_yes_no("Ввести настройки заново?", default=True):
        return "retry"
    return "disable"


def configure_rutracker(console: Console, env: dict[str, str]) -> tuple[str, str]:
    write_hint(
        console,
        "Rutracker",
        why="прямой поиск, скачивание .torrent и fallback, если Jackett не отдал файл.",
        where="логин и пароль обычного аккаунта rutracker.org.",
        example="RUTRACKER_USERNAME=my_login",
        skip="да, но прямой Rutracker-поиск будет недоступен.",
    )
    username = console.ask_required("Rutracker login", default=env.get("RUTRACKER_USERNAME", ""))
    password = console.ask_required(
        "Rutracker password",
        default=env.get("RUTRACKER_PASSWORD", ""),
        secret=True,
    )
    console.write("Rutracker сохранён. Авторизацию бот проверит при первом запросе и в /admin.")
    return username, password


def configure_jackett(
    console: Console,
    env: dict[str, str],
    *,
    skip_checks: bool,
) -> tuple[str, str, str]:
    write_hint(
        console,
        "Jackett",
        why="широкий поиск по вашим индексерам и fallback, когда один трекер недоступен.",
        where="Jackett → Dashboard: URL в адресной строке, API Key вверху страницы.",
        example="JACKETT_URL=http://192.168.1.10:9117",
        skip="да, если хотите использовать только Rutracker или magnet/torrent вручную.",
    )
    while True:
        url = normalize_service_url(
            console.ask_required("Jackett URL", default=env.get("JACKETT_URL", DEFAULT_JACKETT_URL))
        )
        api_key = ask_optional_key(console, "Jackett API key", default=env.get("JACKETT_API_KEY", ""))
        indexers_default = env.get("JACKETT_INDEXERS", "all") or "all"
        if skip_checks:
            indexers = console.ask_required("Jackett indexers", default=indexers_default)
            return url, api_key, indexers
        try:
            indexers_list = probe_jackett(url, api_key)
            console.write(f"Jackett подключен, настроено индексеров: {len(indexers_list)}.")
            if indexers_list:
                preview = ", ".join(i["id"] for i in indexers_list[:8])
                if len(indexers_list) > 8:
                    preview += f", +{len(indexers_list) - 8}"
                console.write(f"Доступные indexer id: {preview}")
            indexers = console.ask_required("Jackett indexers", default=indexers_default)
            return url, api_key, indexers
        except ProbeError as exc:
            action = keep_or_retry(console, "Jackett", exc)
            if action == "keep":
                indexers = console.ask_required("Jackett indexers", default=indexers_default)
                return url, api_key, indexers
            if action == "disable":
                return "", "", "all"


def choose_plex_movie_section(
    console: Console,
    sections: list[dict[str, str]],
) -> str:
    movie_sections = [s for s in sections if s["type"] == "movie"]
    if not movie_sections:
        console.write("Plex подключен, но movie-секция не найдена. Оставляю автоопределение пустым.")
        return ""
    if len(movie_sections) == 1:
        section = movie_sections[0]
        console.write(f"Plex movie-секция найдена: {section['title']} ({section['key']}).")
        return section["key"]
    console.write("Найдено несколько movie-секций:")
    for idx, section in enumerate(movie_sections, start=1):
        console.write(f"{idx}. {section['title']} ({section['key']})")
    while True:
        answer = console.ask("Выберите номер movie-секции", default="1")
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(movie_sections):
            return movie_sections[index - 1]["key"]
        console.write("Введите номер из списка.")


def resolve_plex_from_account(
    console: Console,
    token: str,
    client_id: str,
) -> tuple[str, str, str] | None:
    try:
        resources = probe_plex_resources(token, client_id)
    except ProbeError as exc:
        console.write(f"Не удалось получить список Plex-серверов из аккаунта: {exc}")
        return None
    if not resources:
        console.write("Plex account не вернул доступных серверов.")
        return None

    reachable: list[tuple[dict[str, str], list[dict[str, str]]]] = []
    for resource in resources:
        try:
            sections = probe_plex(resource["uri"], resource["token"])
            reachable.append((resource, sections))
        except ProbeError:
            continue
    if not reachable:
        console.write("Не нашёл Plex URL из аккаунта, доступный из мастера. Попрошу URL вручную.")
        return None

    selected_resource: dict[str, str]
    selected_sections: list[dict[str, str]]
    if len(reachable) == 1:
        selected_resource, selected_sections = reachable[0]
        console.write(
            f"Нашёл доступный Plex server: {selected_resource['name']} — {selected_resource['uri']}"
        )
        if not console.ask_yes_no("Использовать этот Plex URL?", default=True):
            return None
    else:
        console.write("Найдено несколько доступных Plex URL:")
        for idx, (resource, _sections) in enumerate(reachable, start=1):
            flags = []
            if resource.get("local") == "1":
                flags.append("local")
            if resource.get("relay") == "1":
                flags.append("relay")
            suffix = f" ({', '.join(flags)})" if flags else ""
            console.write(f"{idx}. {resource['name']} — {resource['uri']}{suffix}")
        while True:
            answer = console.ask("Выберите Plex URL", default="1")
            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(reachable):
                selected_resource, selected_sections = reachable[index - 1]
                break
            console.write("Введите номер из списка.")

    section = choose_plex_movie_section(console, selected_sections)
    return selected_resource["uri"], selected_resource["token"], section


def configure_plex_manual(
    console: Console,
    env: dict[str, str],
    *,
    token_default: str = "",
    skip_checks: bool,
) -> tuple[str, str, str, str]:
    while True:
        url = normalize_service_url(
            console.ask_required("Plex URL", default=env.get("PLEX_URL", DEFAULT_PLEX_URL))
        )
        token = ask_optional_key(
            console,
            "Plex token",
            default=token_default or env.get("PLEX_TOKEN", ""),
        )
        deeplink = console.ask(
            "PLEX_DEEPLINK_BASE_URL для мобильного redirect",
            default=env.get("PLEX_DEEPLINK_BASE_URL", ""),
        )
        if skip_checks:
            section = console.ask("PLEX_MOVIE_SECTION", default=env.get("PLEX_MOVIE_SECTION", ""))
            return url, token, section, deeplink
        try:
            sections = probe_plex(url, token)
            section = choose_plex_movie_section(console, sections)
            return url, token, section, deeplink
        except ProbeError as exc:
            action = keep_or_retry(console, "Plex", exc)
            if action == "keep":
                section = console.ask("PLEX_MOVIE_SECTION", default=env.get("PLEX_MOVIE_SECTION", ""))
                return url, token, section, deeplink
            if action == "disable":
                return "", "", "", ""


def configure_plex(
    console: Console,
    env: dict[str, str],
    *,
    skip_checks: bool,
) -> tuple[str, str, str, str, str]:
    write_hint(
        console,
        "Plex",
        why="проверка дублей, ожидание появления файла в библиотеке и кнопка «Смотреть в Plex».",
        where="лучший путь — вход через Plex в браузере; fallback — Plex Web → Get Info → View XML → X-Plex-Token.",
        example="PLEX_URL=http://192.168.1.10:32400",
        skip="да, бот продолжит скачивать, но без Plex-проверок и кнопки просмотра.",
    )
    client_id = plex_auth_client_id(env)
    if skip_checks:
        url, token, section, deeplink = configure_plex_manual(
            console, env, skip_checks=True
        )
        return url, token, section, deeplink, client_id

    token_default = env.get("PLEX_TOKEN", "")
    use_auth = console.ask_yes_no(
        "Получить Plex token через браузерный вход?",
        default=not bool(token_default.strip()),
    )
    if use_auth:
        try:
            token = run_plex_pin_auth(console, client_id)
        except ProbeError as exc:
            console.write(f"Plex auth не прошёл: {exc}")
            if not console.ask_yes_no("Вставить Plex token вручную?", default=True):
                return "", "", "", "", client_id
            return (*configure_plex_manual(console, env, skip_checks=False), client_id)

        resolved = resolve_plex_from_account(console, token, client_id)
        if resolved is not None:
            url, resolved_token, section = resolved
            deeplink = console.ask(
                "PLEX_DEEPLINK_BASE_URL для мобильного redirect",
                default=env.get("PLEX_DEEPLINK_BASE_URL", ""),
            )
            return url, resolved_token, section, deeplink, client_id
        url, manual_token, section, deeplink = configure_plex_manual(
            console,
            env,
            token_default=token,
            skip_checks=False,
        )
        return url, manual_token, section, deeplink, client_id

    return (*configure_plex_manual(console, env, skip_checks=False), client_id)


def configure_simple_api_key(
    console: Console,
    env: dict[str, str],
    *,
    name: str,
    env_key: str,
    why: str,
    where: str,
    example: str,
    skip: str,
    probe,
    skip_checks: bool,
) -> str:
    write_hint(console, name, why=why, where=where, example=example, skip=skip)
    while True:
        value = ask_optional_key(console, env_key, default=env.get(env_key, ""))
        if skip_checks:
            return value
        try:
            probe(value)
            console.write(f"{name}: ключ принят.")
            return value
        except ProbeError as exc:
            action = keep_or_retry(console, name, exc)
            if action == "keep":
                return value
            if action == "disable":
                return ""


def run_interactive(
    install_dir: Path,
    *,
    skip_checks: bool = False,
    console: Console | None = None,
) -> int:
    own_console = console is None
    console = console or Console()
    try:
        console.write("PlexLoader setup wizard")
        console.write("Сначала выберем возможности, потом мастер спросит только нужные данные.")
        console.write("")

        env_path = install_dir / ".env"
        previous_env = read_env_file(env_path)
        if previous_env:
            console.write(f"Найден существующий .env: {env_path}")
            if not console.ask_yes_no("Запустить мастер обновления настроек?", default=True):
                console.write("Оставляю существующий .env без изменений.")
                return 0

        features = ask_optional_features(console, previous_env)

        console.write("1. Telegram")
        write_hint(
            console,
            "Telegram BOT_TOKEN",
            why="бот получает сообщения и отправляет ответы в Telegram.",
            where="@BotFather → /newbot → скопируйте token после создания бота.",
            example="123456789:AAExampleToken",
            skip="нет, без token бот не запустится.",
        )
        bot_token = console.ask_required("Вставьте BOT_TOKEN", default=previous_env.get("BOT_TOKEN", ""), secret=True)
        if skip_checks:
            bot_username = "your_bot"
        else:
            while True:
                try:
                    bot_username = validate_bot_token(bot_token)
                    console.write(f"Telegram token принят: @{bot_username}")
                    break
                except ProbeError as exc:
                    console.write(f"Telegram token не прошёл проверку: {exc}")
                    if not console.ask_yes_no("Попробовать ввести token ещё раз?", default=True):
                        return 1
                    bot_token = console.ask_required("Вставьте BOT_TOKEN", secret=True)

        chat_id = (
            console.ask_required(
                "Введите ваш Telegram chat_id",
                default=previous_env.get("ALLOWED_CHAT_IDS", ""),
            )
            if skip_checks
            else choose_chat_id(bot_token, bot_username, console)
        )
        if not parse_chat_ids(chat_id):
            raise WizardError("chat_id должен быть числом или списком чисел через запятую")
        admin_chat_ids = previous_env.get("ADMIN_CHAT_IDS", "").strip() or chat_id

        console.write("")
        console.write("2. Download Station")
        write_hint(
            console,
            "DSM / Download Station",
            why="бот добавляет magnet и .torrent в Synology Download Station.",
            where="DSM URL — адрес NAS; account/password — DSM-пользователь с правами Download Station.",
            example="DS_DESTINATION=video",
            skip="нет, это базовая функция PlexLoader.",
        )
        ds_url = normalize_ds_url(console.ask_required("DSM URL", default=previous_env.get("DS_URL", DEFAULT_DS_URL)))
        ds_account = console.ask_required("DSM account", default=previous_env.get("DS_ACCOUNT", ""))
        ds_password = console.ask_required("DSM password", default=previous_env.get("DS_PASSWORD", ""), secret=True)
        ds_destination = console.ask_required(
            "Папка назначения Download Station",
            default=previous_env.get("DS_DESTINATION", DEFAULT_DS_DESTINATION),
        )

        ds_verify_ssl = False
        if skip_checks:
            ds_verify_ssl = False
        else:
            while True:
                try:
                    ds_verify_ssl = resolve_download_station_ssl(
                        ds_url, ds_account, ds_password, ds_destination, console
                    )
                    break
                except ProbeError as exc:
                    console.write(f"Download Station не прошёл проверку: {exc}")
                    if console.ask_yes_no("Сохранить .env всё равно?", default=False):
                        ds_verify_ssl = exc.kind != "ssl"
                        break
                    if not console.ask_yes_no("Ввести DSM-настройки заново?", default=True):
                        return 1
                    ds_url = normalize_ds_url(console.ask_required("DSM URL", default=ds_url))
                    ds_account = console.ask_required("DSM account", default=ds_account)
                    ds_password = console.ask_required("DSM password", secret=True)
                    ds_destination = console.ask_required(
                        "Папка назначения Download Station", default=ds_destination
                    )

        timezone = detect_timezone()
        rutracker_username = rutracker_password = ""
        if features["rutracker"]:
            rutracker_username, rutracker_password = configure_rutracker(console, previous_env)

        jackett_url = jackett_api_key = ""
        jackett_indexers = previous_env.get("JACKETT_INDEXERS", "all") or "all"
        if features["jackett"]:
            jackett_url, jackett_api_key, jackett_indexers = configure_jackett(
                console, previous_env, skip_checks=skip_checks
            )

        plex_url = plex_token = plex_movie_section = plex_deeplink_base_url = plex_auth_client_id = ""
        if features["plex"]:
            (
                plex_url,
                plex_token,
                plex_movie_section,
                plex_deeplink_base_url,
                plex_auth_client_id,
            ) = configure_plex(
                console, previous_env, skip_checks=skip_checks
            )

        kinopoisk_api_key = ""
        if features["kinopoisk"]:
            kinopoisk_api_key = configure_simple_api_key(
                console,
                previous_env,
                name="Кинопоиск API",
                env_key="KINOPOISK_API_KEY",
                why="поиск по ссылкам Кинопоиска и обогащение карточек /new.",
                where="kinopoiskapiunofficial.tech → личный кабинет → API key.",
                example="KINOPOISK_API_KEY=01234567-89ab-cdef-0123-456789abcdef",
                skip="да, ссылки KP и часть обогащения будут недоступны.",
                probe=probe_kinopoisk,
                skip_checks=skip_checks,
            )

        tmdb_api_token = ""
        if features["tmdb"]:
            tmdb_api_token = configure_simple_api_key(
                console,
                previous_env,
                name="TMDB API",
                env_key="TMDB_API_TOKEN",
                why="точное число эпизодов сезона для /continue.",
                where="themoviedb.org → Settings → API → API Read Access Token.",
                example="TMDB_API_TOKEN=eyJhbGciOi...",
                skip="да, /continue будет работать менее уверенно.",
                probe=probe_tmdb,
                skip_checks=skip_checks,
            )

        openai_api_key = ""
        if features["openai"]:
            openai_api_key = configure_simple_api_key(
                console,
                previous_env,
                name="OpenAI",
                env_key="OPENAI_API_KEY",
                why="голосовой поиск, did-you-mean, пояснения и GPT-подсказки.",
                where="platform.openai.com → API keys; лимит расходов — Settings → Limits.",
                example="OPENAI_API_KEY=sk-...",
                skip="да, voice/GPT функции будут выключены.",
                probe=probe_openai,
                skip_checks=skip_checks,
            )

        movie_discovery_enabled = features["movie_discovery"]
        if movie_discovery_enabled and not (rutracker_username or jackett_url):
            console.write("/new отключён: после проверки не осталось ни Rutracker, ни Jackett.")
            movie_discovery_enabled = False

        config = InstallerConfig(
            bot_token=bot_token,
            allowed_chat_ids=chat_id,
            admin_chat_ids=admin_chat_ids,
            ds_url=ds_url,
            ds_account=ds_account,
            ds_password=ds_password,
            ds_destination=ds_destination,
            ds_verify_ssl=ds_verify_ssl,
            timezone=timezone,
            rutracker_username=rutracker_username,
            rutracker_password=rutracker_password,
            jackett_url=jackett_url,
            jackett_api_key=jackett_api_key,
            jackett_indexers=jackett_indexers,
            kinopoisk_api_key=kinopoisk_api_key,
            tmdb_api_token=tmdb_api_token,
            movie_discovery_enabled=movie_discovery_enabled,
            plex_url=plex_url,
            plex_token=plex_token,
            plex_movie_section=plex_movie_section,
            plex_deeplink_base_url=plex_deeplink_base_url,
            plex_auth_client_id=plex_auth_client_id,
            openai_api_key=openai_api_key,
            voice_search_enabled=bool(openai_api_key),
            gpt_enabled=bool(openai_api_key),
        )
        write_env_file(env_path, config, previous_env)
        console.write("")
        console.write(f".env создан: {env_path}")
        console.write("Конфигурация готова.")
        return 0
    finally:
        if own_console:
            console.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PlexLoader setup wizard")
    parser.add_argument(
        "--install-dir",
        default=os.environ.get("PLEXLOADER_INSTALL_DIR", DEFAULT_INSTALL_DIR),
        help="Directory containing compose.yaml and generated .env",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Collect values without Telegram/DSM network checks",
    )
    args = parser.parse_args(argv)
    try:
        install_dir = Path(args.install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)
        return run_interactive(install_dir, skip_checks=args.skip_checks)
    except KeyboardInterrupt:
        print("\nУстановка прервана пользователем.", file=sys.stderr)
        return 130
    except WizardError as exc:
        print(f"Ошибка установки: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
