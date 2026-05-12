import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("tg_torrent_drop")

_BASE_URL = "https://rutracker.org/forum"
_INDEX_URL = f"{_BASE_URL}/index.php"
_LOGIN_URL = f"{_BASE_URL}/login.php"
_SEARCH_URL = f"{_BASE_URL}/tracker.php"
_DOWNLOAD_URL = f"{_BASE_URL}/dl.php"
_VIEWTOPIC_URL = f"{_BASE_URL}/viewtopic.php"

PageType = Literal["normal", "login", "captcha", "blocked", "unknown"]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class RutrackerError(RuntimeError):
    pass


class RutrackerTopicUnavailable(RutrackerError):
    pass


@dataclass
class RutrackerResult:
    topic_id: str
    title: str
    category: str
    size: str
    seeders: int


def _synchronized(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class RutrackerClient:
    def __init__(
        self,
        username: str,
        password: str,
        max_results: int = 10,
        backoff_base_seconds: float = 60.0,
        backoff_max_seconds: float = 900.0,
    ) -> None:
        self._username = username
        self._password = password
        self._max_results = max_results
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._logged_in = False
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._backoff_current_seconds = 0.0
        self._cooldown_until = 0.0
        self._cooldown_reason = ""

    # ------------------------------------------------------------------
    # Page classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_captcha(html: str) -> bool:
        """Return True when the page requires solving a CAPTCHA."""
        markers = ("cap_code", "cap_sid", "captcha", "recaptcha")
        low = html.lower()
        return any(m in low for m in markers)

    @staticmethod
    def _page_type(html: str, http_status: int = 200) -> PageType:
        """Classify a Rutracker response page."""
        if http_status in (403, 503):
            return "blocked"
        low = html.lower()
        if any(m in low for m in ("cap_code", "cap_sid", "captcha", "recaptcha")):
            return "captcha"
        if "login_username" in low:
            return "login"
        if "bb_session" in html or "logout" in low or "tracker.php" in low:
            return "normal"
        return "unknown"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @_synchronized
    def login(self) -> None:
        self._check_cooldown()
        try:
            resp = self._session.post(
                _LOGIN_URL,
                data={
                    "login_username": self._username,
                    "login_password": self._password,
                    "login": "Вход",
                },
                timeout=15,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            status_code = self._http_status_from_exception(e)
            if status_code in (403, 503):
                self._raise_with_backoff(
                    f"Rutracker вернул HTTP {status_code} при авторизации.",
                    f"HTTP {status_code}",
                )
            self._raise_with_backoff(f"Не удалось подключиться к rutracker: {e}", "сетевая ошибка")

        if "bb_session" not in self._session.cookies:
            if self._has_captcha(resp.text):
                self._raise_with_backoff(
                    "Rutracker требует решения капчи.\n"
                    "Войдите на rutracker.org в браузере с этого IP, "
                    "решите капчу вручную, затем попробуйте снова.",
                    "капча",
                )
            raise RutrackerError(
                "Авторизация не удалась. Проверьте логин/пароль в настройках бота."
            )

        self._logged_in = True
        self._clear_backoff()
        logger.info("Rutracker login successful")

    def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            self.login()

    def _is_login_page(self, html: str) -> bool:
        return "login_username" in html.lower()

    def _is_topic_unavailable_page(self, html: str) -> bool:
        low = html.lower()
        markers = (
            "такой темы не существует",
            "запрошенной темы не существует",
            "тема не найдена",
            "тема удалена",
            "сообщение не существует",
            "topic not found",
            "topic does not exist",
        )
        return any(marker in low for marker in markers)

    def _cooldown_remaining_seconds(self) -> int:
        remaining = self._cooldown_until - time.monotonic()
        return max(0, int(remaining + 0.999))

    def _check_cooldown(self) -> None:
        remaining = self._cooldown_remaining_seconds()
        if remaining <= 0:
            return

        reason = f" Причина: {self._cooldown_reason}." if self._cooldown_reason else ""
        raise RutrackerError(
            f"Rutracker временно на паузе после недавней ошибки.{reason} "
            f"Повторите запрос примерно через {remaining} сек."
        )

    def _clear_backoff(self) -> None:
        self._backoff_current_seconds = 0.0
        self._cooldown_until = 0.0
        self._cooldown_reason = ""

    def _set_backoff(self, reason: str) -> int:
        if self._backoff_current_seconds <= 0:
            delay = self._backoff_base_seconds
        else:
            delay = min(self._backoff_current_seconds * 2, self._backoff_max_seconds)

        self._backoff_current_seconds = delay
        self._cooldown_until = time.monotonic() + delay
        self._cooldown_reason = reason
        return int(delay + 0.999)

    def _raise_with_backoff(self, user_message: str, reason: str) -> None:
        delay = self._set_backoff(reason)
        raise RutrackerError(f"{user_message}\nПовтор к Rutracker отложен примерно на {delay} сек.")

    @staticmethod
    def _http_status_from_exception(error: requests.RequestException) -> int | None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code if isinstance(status_code, int) else None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @_synchronized
    def diagnose(self) -> dict:
        """Probe Rutracker step by step and return a structured report.

        Keys in the returned dict:
        - reachable (bool)       — server responded (even with 4xx/5xx)
        - http_status (int|None) — HTTP status of the index page
        - page_type (str)        — classification of the index page
        - login_ok (bool)        — True if bb_session cookie was set
        - captcha (bool)         — captcha detected on login response
        - error (str)            — human-readable error description (empty if OK)
        """
        result: dict = {
            "reachable": False,
            "http_status": None,
            "page_type": "unknown",
            "login_ok": False,
            "captcha": False,
            "error": "",
        }

        cooldown_remaining = self._cooldown_remaining_seconds()
        if cooldown_remaining > 0:
            result["error"] = (
                "Rutracker временно на паузе после недавней ошибки"
                f" ({self._cooldown_reason}). Повтор примерно через {cooldown_remaining} сек."
            )
            return result

        # Step 1 — can we reach the server at all?
        try:
            idx = self._session.get(_INDEX_URL, timeout=10, allow_redirects=True)
            result["reachable"] = True
            result["http_status"] = idx.status_code
            result["page_type"] = self._page_type(idx.text, idx.status_code)
        except requests.ConnectionError as e:
            self._set_backoff("нет соединения")
            result["error"] = f"Нет соединения с rutracker.org: {e}"
            return result
        except requests.Timeout:
            self._set_backoff("таймаут")
            result["error"] = "Таймаут при подключении к rutracker.org (>10 сек)"
            return result
        except requests.RequestException as e:
            self._set_backoff("сетевая ошибка")
            result["error"] = f"Сетевая ошибка: {e}"
            return result

        if result["http_status"] in (403, 503):
            self._set_backoff(f"HTTP {result['http_status']}")
            result["error"] = (
                f"Rutracker вернул HTTP {result['http_status']} — "
                "возможно, IP сервера заблокирован или сработала защита Cloudflare."
            )
            return result

        # Step 2 — try to log in (always fresh attempt)
        self._logged_in = False
        try:
            login_resp = self._session.post(
                _LOGIN_URL,
                data={
                    "login_username": self._username,
                    "login_password": self._password,
                    "login": "Вход",
                },
                timeout=15,
                allow_redirects=True,
            )
            login_resp.raise_for_status()
        except requests.RequestException as e:
            self._set_backoff("ошибка авторизации")
            result["error"] = f"Ошибка при отправке запроса авторизации: {e}"
            return result

        login_html = login_resp.text

        if "bb_session" in self._session.cookies:
            result["login_ok"] = True
            self._logged_in = True
            self._clear_backoff()
            return result

        # Login failed — figure out why
        if self._has_captcha(login_html):
            self._set_backoff("капча")
            result["captcha"] = True
            result["error"] = (
                "Rutracker требует решения капчи. "
                "Войдите на rutracker.org вручную с этого IP, "
                "решите капчу в браузере, затем попробуйте снова."
            )
        else:
            result["error"] = (
                "Авторизация не удалась. "
                "Проверьте RUTRACKER_USERNAME и RUTRACKER_PASSWORD в настройках бота."
            )

        return result

    @_synchronized
    def search(self, query: str, torrent_age_days: int | None = None) -> list[RutrackerResult]:
        self._check_cooldown()
        self._ensure_logged_in()
        params = {"nm": query}
        if torrent_age_days in {-1, 1, 3, 7, 14, 32}:
            params["tm"] = str(torrent_age_days)

        try:
            resp = self._session.get(
                _SEARCH_URL,
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException as e:
            status_code = self._http_status_from_exception(e)
            reason = f"HTTP {status_code}" if status_code in (403, 503) else "ошибка поиска"
            self._raise_with_backoff(f"Ошибка поиска на rutracker: {e}", reason)

        page_type = self._page_type(html, resp.status_code)
        if page_type in ("captcha", "blocked"):
            reason = "капча" if page_type == "captcha" else f"HTTP {resp.status_code}"
            self._raise_with_backoff("Rutracker временно не готов выполнять поиск.", reason)

        if self._is_login_page(html):
            logger.info("Rutracker session expired, re-logging in")
            self._logged_in = False
            self.login()
            try:
                resp = self._session.get(_SEARCH_URL, params=params, timeout=20)
                resp.raise_for_status()
                html = resp.text
            except requests.RequestException as e:
                status_code = self._http_status_from_exception(e)
                reason = f"HTTP {status_code}" if status_code in (403, 503) else "ошибка поиска"
                self._raise_with_backoff(f"Ошибка поиска на rutracker: {e}", reason)

            page_type = self._page_type(html, resp.status_code)
            if page_type in ("captcha", "blocked"):
                reason = "капча" if page_type == "captcha" else f"HTTP {resp.status_code}"
                self._raise_with_backoff("Rutracker временно не готов выполнять поиск.", reason)

        results = self._parse_search_results(html)
        self._clear_backoff()
        return results

    def _parse_search_results(self, html: str) -> list[RutrackerResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[RutrackerResult] = []

        for row in soup.select("tr.tCenter.hl-tr"):
            try:
                link = row.select_one("a.tLink")
                if not link:
                    continue

                href = link.get("href", "")
                topic_id = ""
                if "t=" in href:
                    topic_id = href.split("t=")[-1].split("&")[0].strip()
                if not topic_id:
                    continue

                title = link.get_text(strip=True)

                category_tag = row.select_one("td.f-name a")
                category = category_tag.get_text(strip=True) if category_tag else ""

                size_tag = row.select_one("td.tor-size")
                size = size_tag.get_text(" ", strip=True) if size_tag else "?"

                seeders_tag = row.select_one("b.seedmed") or row.select_one("td.seedmed")
                try:
                    seeders = int(seeders_tag.get_text(strip=True)) if seeders_tag else 0
                except ValueError:
                    seeders = 0

                results.append(RutrackerResult(
                    topic_id=topic_id,
                    title=title,
                    category=category,
                    size=size,
                    seeders=seeders,
                ))

                if len(results) >= self._max_results:
                    break

        # _max_results is a hard cap; Rutracker shows ≤50 per page naturally

            except (AttributeError, TypeError, ValueError):
                logger.debug("Failed to parse rutracker result row", exc_info=True)
                continue

        return results

    @_synchronized
    def download_torrent(self, topic_id: str) -> bytes:
        self._check_cooldown()
        self._ensure_logged_in()

        try:
            resp = self._session.get(
                _DOWNLOAD_URL,
                params={"t": topic_id},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            status_code = self._http_status_from_exception(e)
            reason = f"HTTP {status_code}" if status_code in (403, 503) else "ошибка скачивания"
            self._raise_with_backoff(f"Не удалось скачать torrent-файл: {e}", reason)

        page_type = self._page_type(resp.text[:1000] if resp.content[:3] != b"d8:" else "", resp.status_code)
        if page_type in ("captcha", "blocked"):
            reason = "капча" if page_type == "captcha" else f"HTTP {resp.status_code}"
            self._raise_with_backoff("Rutracker временно не готов отдавать torrent-файл.", reason)

        if self._is_login_page(resp.text[:500] if resp.content[:3] != b"d8:" else ""):
            self._logged_in = False
            self.login()
            try:
                resp = self._session.get(_DOWNLOAD_URL, params={"t": topic_id}, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                status_code = self._http_status_from_exception(e)
                reason = f"HTTP {status_code}" if status_code in (403, 503) else "ошибка скачивания"
                self._raise_with_backoff(f"Не удалось скачать torrent-файл: {e}", reason)

            page_type = self._page_type(resp.text[:1000] if resp.content[:3] != b"d8:" else "", resp.status_code)
            if page_type in ("captcha", "blocked"):
                reason = "капча" if page_type == "captcha" else f"HTTP {resp.status_code}"
                self._raise_with_backoff("Rutracker временно не готов отдавать torrent-файл.", reason)

        if len(resp.content) < 20 or not resp.content.startswith(b"d"):
            raise RutrackerError("Полученный файл не является torrent-файлом.")

        self._clear_backoff()
        return resp.content

    @_synchronized
    def get_topic_image_url(self, topic_id: str) -> str | None:
        """Extract the cover/poster image URL from the first post of a topic.

        Returns None on any failure — callers must treat this as best-effort.
        """
        self._ensure_logged_in()

        try:
            resp = self._session.get(
                _VIEWTOPIC_URL,
                params={"t": topic_id},
                timeout=10,
            )
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException:
            return None

        if self._is_login_page(html):
            return None

        soup = BeautifulSoup(html, "html.parser")

        # Scope to first post body only
        first_post = soup.select_one("div.post_body") or soup

        # Primary: <var class="postImg"> — Rutracker's custom image tag
        # The actual URL is stored in the `title` attribute
        for var in first_post.select("var.postImg"):
            url = var.get("title") or var.get("data-src") or ""
            if url.startswith("http"):
                return url

        # Fallback: first <img> that looks like a poster (skip smileys/flags)
        for img in first_post.select("img"):
            src = img.get("src") or ""
            if (
                src.startswith("http")
                and "smiles" not in src
                and "flags" not in src
                and "tracker" not in src
            ):
                return src

        return None

    @_synchronized
    def get_topic_title(self, topic_id: str) -> str:
        """Fetch the current title of a topic page (for subscription update checks)."""
        self._check_cooldown()
        self._ensure_logged_in()

        try:
            resp = self._session.get(
                _VIEWTOPIC_URL,
                params={"t": topic_id},
                timeout=15,
            )
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException as e:
            status_code = self._http_status_from_exception(e)
            if status_code in (404, 410):
                raise RutrackerTopicUnavailable(f"Тема Rutracker {topic_id} недоступна: HTTP {status_code}") from e
            reason = f"HTTP {status_code}" if status_code in (403, 503) else "ошибка страницы темы"
            self._raise_with_backoff(f"Не удалось получить страницу темы: {e}", reason)

        page_type = self._page_type(html, resp.status_code)
        if page_type in ("captcha", "blocked"):
            reason = "капча" if page_type == "captcha" else f"HTTP {resp.status_code}"
            self._raise_with_backoff("Rutracker временно не готов отдавать страницу темы.", reason)

        if self._is_login_page(html):
            self._logged_in = False
            self.login()
            try:
                resp = self._session.get(_VIEWTOPIC_URL, params={"t": topic_id}, timeout=15)
                resp.raise_for_status()
                html = resp.text
            except requests.RequestException as e:
                status_code = self._http_status_from_exception(e)
                if status_code in (404, 410):
                    raise RutrackerTopicUnavailable(f"Тема Rutracker {topic_id} недоступна: HTTP {status_code}") from e
                reason = f"HTTP {status_code}" if status_code in (403, 503) else "ошибка страницы темы"
                self._raise_with_backoff(f"Не удалось получить страницу темы: {e}", reason)

            page_type = self._page_type(html, resp.status_code)
            if page_type in ("captcha", "blocked"):
                reason = "капча" if page_type == "captcha" else f"HTTP {resp.status_code}"
                self._raise_with_backoff("Rutracker временно не готов отдавать страницу темы.", reason)

        soup = BeautifulSoup(html, "html.parser")
        tag = soup.select_one("h1.maintitle a") or soup.select_one("h1.maintitle")
        if tag:
            self._clear_backoff()
            return tag.get_text(strip=True)

        if self._is_topic_unavailable_page(html):
            raise RutrackerTopicUnavailable(f"Тема Rutracker {topic_id} недоступна или удалена")

        raise RutrackerError(f"Не удалось найти заголовок темы {topic_id}")
