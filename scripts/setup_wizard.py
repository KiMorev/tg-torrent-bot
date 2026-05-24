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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_INSTALL_DIR = "/volume1/docker/plexloader"
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_DS_URL = "https://host.docker.internal:5001"
DEFAULT_DS_DESTINATION = "video"

SAFE_ENV_RE = re.compile(r"^[A-Za-z0-9_./:@,+-]*$")


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
        suffix = f" [{default}]" if default else ""
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


def normalize_ds_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url
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


def format_env_value(value: object) -> str:
    text = str(value)
    if text == "":
        return ""
    if SAFE_ENV_RE.fullmatch(text):
        return text
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_env(config: InstallerConfig) -> str:
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
        ("MOVIE_DISCOVERY_ENABLED", "false"),
        ("RUTRACKER_USERNAME", ""),
        ("RUTRACKER_PASSWORD", ""),
        ("JACKETT_URL", ""),
        ("JACKETT_API_KEY", ""),
        ("KINOPOISK_API_KEY", ""),
        ("PLEX_URL", ""),
        ("PLEX_TOKEN", ""),
        ("PLEX_MOVIE_SECTION", ""),
        ("PLEX_DEEPLINK_BASE_URL", ""),
        ("OPENAI_API_KEY", ""),
        ("VOICE_SEARCH_ENABLED", "false"),
        ("GPT_ENABLED", "false"),
    ]
    lines = [
        "# Generated by PlexLoader setup wizard.",
        "# Optional integrations can be enabled later by rerunning the wizard or editing this file.",
    ]
    lines.extend(f"{key}={format_env_value(value)}" for key, value in entries)
    return "\n".join(lines) + "\n"


def write_env_file(path: Path, config: InstallerConfig) -> None:
    path.write_text(render_env(config), encoding="utf-8", newline="\n")


def _ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    return None if verify_ssl else ssl._create_unverified_context()


def _read_json_url(
    url: str,
    *,
    data: dict[str, str] | None = None,
    verify_ssl: bool = True,
    timeout: int = 15,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context(verify_ssl)
        ) as response:
            raw = response.read().decode("utf-8")
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
    if not isinstance(payload, dict):
        raise ProbeError("Сервис вернул неожиданный ответ", kind="parse")
    return payload


def telegram_api(token: str, method: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/{method}{query}"
    payload = _read_json_url(url)
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


def probe_download_station(
    ds_url: str,
    account: str,
    password: str,
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
        if not tasks_payload.get("success"):
            code = (tasks_payload.get("error") or {}).get("code", "unknown")
            raise ProbeError(
                f"Логин работает, но Download Station недоступен этому пользователю (код {code})",
                kind="auth",
            )
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
    console: Console,
) -> bool:
    try:
        probe_download_station(ds_url, account, password, verify_ssl=True)
        console.write("Download Station доступен, SSL-сертификат принят.")
        return True
    except ProbeError as exc:
        if exc.kind != "ssl":
            raise
        console.write("DSM использует сертификат, которому эта система не доверяет.")
        console.write("Пробую безопасный для домашней сети fallback: DS_VERIFY_SSL=false.")
        probe_download_station(ds_url, account, password, verify_ssl=False)
        console.write("Download Station доступен без проверки SSL.")
        return False


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


def run_interactive(install_dir: Path, *, skip_checks: bool = False) -> int:
    console = Console()
    try:
        console.write("PlexLoader setup wizard")
        console.write("Первая версия настраивает ядро: Telegram + Synology Download Station.")
        console.write("Rutracker, Jackett, Plex, /new и OpenAI можно будет включить следующим шагом.")
        console.write("")

        env_path = install_dir / ".env"
        if env_path.exists() and not console.ask_yes_no(
            f"{env_path} уже существует. Перезаписать?", default=False
        ):
            console.write("Оставляю существующий .env без изменений.")
            return 0

        console.write("1. Telegram")
        console.write("Откройте @BotFather, создайте бота командой /newbot и скопируйте BOT_TOKEN.")
        bot_token = console.ask_required("Вставьте BOT_TOKEN")
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
                    bot_token = console.ask_required("Вставьте BOT_TOKEN")

        chat_id = (
            console.ask_required("Введите ваш Telegram chat_id")
            if skip_checks
            else choose_chat_id(bot_token, bot_username, console)
        )
        if not parse_chat_ids(chat_id):
            raise WizardError("chat_id должен быть числом или списком чисел через запятую")

        console.write("")
        console.write("2. Download Station")
        console.write("Нужен DSM-пользователь с доступом к Download Station и папке загрузки.")
        ds_url = normalize_ds_url(console.ask_required("DSM URL", default=DEFAULT_DS_URL))
        ds_account = console.ask_required("DSM account")
        ds_password = console.ask_required("DSM password", secret=True)
        ds_destination = console.ask_required(
            "Папка назначения Download Station", default=DEFAULT_DS_DESTINATION
        )

        ds_verify_ssl = False
        if skip_checks:
            ds_verify_ssl = False
        else:
            while True:
                try:
                    ds_verify_ssl = resolve_download_station_ssl(
                        ds_url, ds_account, ds_password, console
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
        config = InstallerConfig(
            bot_token=bot_token,
            allowed_chat_ids=chat_id,
            admin_chat_ids=chat_id,
            ds_url=ds_url,
            ds_account=ds_account,
            ds_password=ds_password,
            ds_destination=ds_destination,
            ds_verify_ssl=ds_verify_ssl,
            timezone=timezone,
        )
        write_env_file(env_path, config)
        console.write("")
        console.write(f".env создан: {env_path}")
        console.write("Базовая конфигурация готова.")
        return 0
    finally:
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
