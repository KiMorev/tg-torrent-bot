import logging
import threading
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
    def __init__(self, username: str, password: str, max_results: int = 10) -> None:
        self._username = username
        self._password = password
        self._max_results = max_results
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._logged_in = False

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
            raise RutrackerError(f"Не удалось подключиться к rutracker: {e}") from e

        if "bb_session" not in self._session.cookies:
            if self._has_captcha(resp.text):
                raise RutrackerError(
                    "Rutracker требует решения капчи.\n"
                    "Войдите на rutracker.org в браузере с этого IP, "
                    "решите капчу вручную, затем попробуйте снова."
                )
            raise RutrackerError(
                "Авторизация не удалась. Проверьте логин/пароль в настройках бота."
            )

        self._logged_in = True
        logger.info("Rutracker login successful")

    def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            self.login()

    def _is_login_page(self, html: str) -> bool:
        return "login_username" in html.lower()

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

        # Step 1 — can we reach the server at all?
        try:
            idx = self._session.get(_INDEX_URL, timeout=10, allow_redirects=True)
            result["reachable"] = True
            result["http_status"] = idx.status_code
            result["page_type"] = self._page_type(idx.text, idx.status_code)
        except requests.ConnectionError as e:
            result["error"] = f"Нет соединения с rutracker.org: {e}"
            return result
        except requests.Timeout:
            result["error"] = "Таймаут при подключении к rutracker.org (>10 сек)"
            return result
        except requests.RequestException as e:
            result["error"] = f"Сетевая ошибка: {e}"
            return result

        if result["http_status"] in (403, 503):
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
            result["error"] = f"Ошибка при отправке запроса авторизации: {e}"
            return result

        login_html = login_resp.text

        if "bb_session" in self._session.cookies:
            result["login_ok"] = True
            self._logged_in = True
            return result

        # Login failed — figure out why
        if self._has_captcha(login_html):
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
    def search(self, query: str) -> list[RutrackerResult]:
        self._ensure_logged_in()

        try:
            resp = self._session.get(
                _SEARCH_URL,
                params={"nm": query},
                timeout=20,
            )
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException as e:
            raise RutrackerError(f"Ошибка поиска на rutracker: {e}") from e

        if self._is_login_page(html):
            logger.info("Rutracker session expired, re-logging in")
            self._logged_in = False
            self.login()
            try:
                resp = self._session.get(_SEARCH_URL, params={"nm": query}, timeout=20)
                resp.raise_for_status()
                html = resp.text
            except requests.RequestException as e:
                raise RutrackerError(f"Ошибка поиска на rutracker: {e}") from e

        return self._parse_search_results(html)

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

            except Exception:
                logger.debug("Failed to parse rutracker result row", exc_info=True)
                continue

        return results

    @_synchronized
    def download_torrent(self, topic_id: str) -> bytes:
        self._ensure_logged_in()

        try:
            resp = self._session.get(
                _DOWNLOAD_URL,
                params={"t": topic_id},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RutrackerError(f"Не удалось скачать torrent-файл: {e}") from e

        if self._is_login_page(resp.text[:500] if resp.content[:3] != b"d8:" else ""):
            self._logged_in = False
            self.login()
            try:
                resp = self._session.get(_DOWNLOAD_URL, params={"t": topic_id}, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise RutrackerError(f"Не удалось скачать torrent-файл: {e}") from e

        if len(resp.content) < 20 or not resp.content.startswith(b"d"):
            raise RutrackerError("Полученный файл не является torrent-файлом.")

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
            raise RutrackerError(f"Не удалось получить страницу темы: {e}") from e

        if self._is_login_page(html):
            self._logged_in = False
            self.login()
            try:
                resp = self._session.get(_VIEWTOPIC_URL, params={"t": topic_id}, timeout=15)
                resp.raise_for_status()
                html = resp.text
            except requests.RequestException as e:
                raise RutrackerError(f"Не удалось получить страницу темы: {e}") from e

        soup = BeautifulSoup(html, "html.parser")
        tag = soup.select_one("h1.maintitle a") or soup.select_one("h1.maintitle")
        if tag:
            return tag.get_text(strip=True)

        raise RutrackerError(f"Не удалось найти заголовок темы {topic_id}")
