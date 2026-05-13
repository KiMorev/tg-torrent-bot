"""Jackett torrent indexer client — Torznab API.

All methods are synchronous; call via asyncio.to_thread() from bot handlers.
"""
from __future__ import annotations

import logging
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

logger = logging.getLogger("tg_torrent_drop")
_STARTUP_ERROR = "Jackett ещё запускается — подождите ~1 мин."
_STARTUP_DIAGNOSTIC_ERROR = "Jackett ещё запускается — подождите ~1 мин и повторите /searchstatus."
_APIKEY_QUERY_RE = re.compile(r"(?i)(apikey=)[^&\s]+")

# Characters forbidden in XML 1.0 (except tab \x09, LF \x0A, CR \x0D).
# Some torrent titles or descriptions contain these, causing ET.ParseError.
# Use a regular (non-raw) string so ￾ / ￿ are proper Unicode escapes.
_INVALID_XML_RE = re.compile(
    "[\x00-\x08\x0B\x0C\x0E-\x1F\x7F￾￿\uD800-\uDFFF]"
)


def _strip_invalid_xml(text: str) -> str:
    """Remove characters that are illegal in XML 1.0."""
    return _INVALID_XML_RE.sub("", text)


def _sanitize_error_text(value: object, api_key: str = "") -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, "***")
    return _APIKEY_QUERY_RE.sub(r"\1***", text)


class JackettError(RuntimeError):
    pass


class JackettMagnetRedirect(JackettError):
    """Raised when Jackett's download proxy redirects to a magnet: URI.

    This means the tracker has no downloadable .torrent file for this result
    and only provides a magnet link.  The caller should use ``magnet_url``
    from the search result instead.
    """
    def __init__(self, magnet_url: str) -> None:
        super().__init__(f"Torrent не доступен — трекер редиректит на magnet ({magnet_url[:80]})")
        self.magnet_url = magnet_url


@dataclass
class JackettResult:
    title: str
    size: str           # human-readable ("4.1 GB")
    seeders: int
    tracker: str        # indexer name ("rutracker", "kinozal", …)
    topic_url: str      # URL of the topic on the original tracker
    magnet_url: str | None
    torrent_url: str | None  # Jackett proxy download URL
    published_at: str = ""


def _synchronized(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _fmt_size(size_bytes: int) -> str:
    val = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} PB"


def _classify_response(resp: requests.Response) -> str:
    """Classify a Jackett response to distinguish healthy JSON/XML from HTML.

    Returns one of three values:
    - ``""``        — response is JSON/XML (healthy, continue processing).
    - ``"login"``   — HTML came from a redirect to a login/auth page; the API
                      key was rejected.
    - ``"loading"`` — HTML without a login redirect; Jackett is still starting
                      up or temporarily returned an error page.

    Detection order (most to least reliable):
    1. Content-Type header.
    2. Redirect history + final URL path (login redirect detection).
    3. Body prefix as a last-resort fallback.
    """
    ct = resp.headers.get("Content-Type", "").lower()

    # Known-good types → healthy response.
    if any(t in ct for t in ("json", "xml", "rss", "atom")):
        return ""

    is_html = "html" in ct
    if not is_html:
        # No useful Content-Type — peek at the body.
        try:
            snippet = resp.text.strip()[:15].lower()
            is_html = snippet.startswith("<!") or snippet.startswith("<html")
        except (AttributeError, TypeError):
            pass

    if not is_html:
        return ""

    # HTML confirmed — distinguish login redirect from startup/loading page.
    if resp.history:
        final_path = resp.url.lower()
        if any(seg in final_path for seg in ("/login", "/auth", "/signin")):
            return "login"

    return "loading"


class JackettClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        max_results: int = 10,
        indexers: str = "all",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_results = max_results
        self._indexers = indexers or "all"
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "tg-torrent-bot/1.0"})

    def _reset_session(self) -> None:
        """Close and recreate the connection pool.

        Call when a connection error may have left the session in a bad state,
        e.g. stale keep-alive connections after Docker network re-initialisation.
        A fresh session ensures the next request gets a clean TCP connection.
        """
        try:
            self._session.close()
        except Exception:
            pass
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "tg-torrent-bot/1.0"})

    @_synchronized
    def get_indexers(self) -> list[dict]:
        """Return list of configured indexers as [{"id": ..., "name": ...}].
        Raises JackettError on failure.
        """
        url = f"{self._base_url}/api/v2.0/indexers"
        try:
            for attempt in range(2):
                resp = self._session.get(
                    url,
                    params={"apikey": self._api_key, "configured": "true"},
                    timeout=10,
                )
                resp.raise_for_status()
                html_kind = _classify_response(resp)
                if html_kind == "login":
                    raise JackettError(
                        "Jackett вернул страницу входа — API ключ не принят "
                        "(проверьте JACKETT_API_KEY)"
                    )
                if html_kind == "loading":
                    if attempt == 0:
                        logger.debug("Jackett indexers probe returned startup response; retrying once")
                        continue
                    raise JackettError(_STARTUP_ERROR)
                try:
                    data = resp.json()
                except ValueError:
                    if attempt == 0:
                        logger.debug("Jackett indexers probe returned non-JSON response; retrying once")
                        continue
                    raise JackettError(_STARTUP_ERROR)
                if isinstance(data, list):
                    return [
                        {"id": i.get("id", ""), "name": i.get("name") or i.get("id", "")}
                        for i in data
                        if i.get("configured") and i.get("id")
                    ]
            return []
        except JackettError:
            raise
        except requests.RequestException as e:
            raise JackettError(f"Ошибка получения индексеров: {_sanitize_error_text(e, self._api_key)}") from e

    @_synchronized
    def search(
        self,
        query: str,
        indexers: list[str] | None = None,
        fetch_limit: int | None = None,
        categories: str = "2000,5000,5070",
    ) -> list[JackettResult]:
        """Search Jackett via the JSON API with Tracker[] filtering.

        Uses /api/v2.0/indexers/all/results (JSON endpoint) which correctly
        respects Tracker[] query parameters for indexer filtering.
        The Torznab (/torznab) endpoint ignores Tracker[] and always searches
        all configured indexers via AggregateSearch.

        Jackett's ResultsController treats the {indexerId} path segment as a single
        atomic string — comma-separated IDs are NOT supported. The correct approach
        is to always use the "all" path and pass specific indexers as repeated
        Tracker[] query parameters.

        If categories yields 0 results the call is retried without the category filter.
        """
        limit = fetch_limit if fetch_limit is not None else self._max_results
        # JSON endpoint — supports Tracker[] filtering.
        url = f"{self._base_url}/api/v2.0/indexers/all/results"

        # Determine effective tracker filter.
        # indexers param takes priority; fall back to self._indexers config if set.
        effective: list[str] | None = None
        if indexers is not None:
            effective = [i for i in indexers if i.lower() != "all"]
        elif self._indexers.lower() != "all":
            effective = [s.strip() for s in self._indexers.split(",")
                         if s.strip() and s.strip().lower() != "all"]

        # Build params as list of tuples to support repeated Tracker[] keys.
        base: list[tuple[str, str]] = [
            ("apikey", self._api_key),
            ("Query", query),
        ]
        if categories:
            base.append(("Category[]", categories))
        if effective:
            for tid in effective:
                base.append(("Tracker[]", tid))

        def _do_search(params: list[tuple[str, str]]) -> list[JackettResult]:
            try:
                resp = self._session.get(url, params=params, timeout=(10, 40))
                resp.raise_for_status()
            except requests.ConnectionError as e:
                self._reset_session()
                raise JackettError(f"Ошибка подключения к Jackett: {_sanitize_error_text(e, self._api_key)}") from e
            except requests.RequestException as e:
                raise JackettError(f"Ошибка подключения к Jackett: {_sanitize_error_text(e, self._api_key)}") from e
            return self._parse_json_results(resp.text, limit)

        results = _do_search(base)
        # Fallback: retry without category filter if nothing found
        if not results and categories:
            base_no_cat = [(k, v) for k, v in base if k != "Category[]"]
            results = _do_search(base_no_cat)

        return results

    @_synchronized
    def download_torrent(self, torrent_url: str) -> bytes:
        """Download a .torrent file via Jackett's proxy URL.

        Raises JackettMagnetRedirect when Jackett's proxy redirects to a
        magnet: URI (tracker has no .torrent file, only a magnet link).
        Raises JackettError for all other failures.
        """
        try:
            # Disable auto-redirect so we can intercept magnet: redirects
            # before requests raises InvalidSchema trying to follow them.
            resp = self._session.get(torrent_url, timeout=30, allow_redirects=False)

            # Follow HTTP/HTTPS redirects manually; stop at magnet:
            visited: set[str] = {torrent_url}
            while resp.is_redirect:
                location = resp.headers.get("Location", "")
                if location.startswith("magnet:"):
                    raise JackettMagnetRedirect(location)
                if not location or location in visited or len(visited) > 5:
                    break  # guard against redirect loops
                visited.add(location)
                resp = self._session.get(location, timeout=30, allow_redirects=False)

            resp.raise_for_status()

        except JackettMagnetRedirect:
            raise
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            body_hint = ""
            if e.response is not None:
                raw = e.response.text[:300].strip().replace("\n", " ")
                body_hint = f" | body: {raw!r}"
            sanitized = _sanitize_error_text(e, self._api_key)
            logger.debug("download_torrent HTTP %s%s", status, body_hint)
            raise JackettError(
                f"Не удалось скачать torrent через Jackett: HTTP {status} — {sanitized}"
            ) from e
        except requests.RequestException as e:
            raise JackettError(
                f"Не удалось скачать torrent через Jackett: {_sanitize_error_text(e, self._api_key)}"
            ) from e

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            html_hint = resp.text[:200].strip().replace("\n", " ")
            logger.debug("download_torrent returned HTML (session issue?): %r", html_hint)
            raise JackettError(
                "Jackett вернул HTML вместо torrent-файла — вероятно, сессия трекера устарела."
            )
        if len(resp.content) < 20 or not resp.content.startswith(b"d"):
            logger.debug(
                "download_torrent: unexpected content type=%r, first bytes=%r",
                content_type, resp.content[:40],
            )
            raise JackettError("Полученный файл не является torrent-файлом.")
        return resp.content

    @_synchronized
    def test_connection(self) -> dict:
        """Probe Jackett and return a structured diagnostics dict."""
        result: dict = {
            "reachable": False,
            "http_status": None,
            "api_ok": False,
            "indexers": [],
            "error": "",
        }
        url = f"{self._base_url}/api/v2.0/indexers"
        try:
            for attempt in range(2):
                resp = self._session.get(
                    url,
                    params={"apikey": self._api_key, "configured": "true"},
                    timeout=10,
                )
                result["reachable"] = True
                result["http_status"] = resp.status_code
                if resp.status_code == 401:
                    result["error"] = "Неверный API-ключ Jackett."
                    return result
                if resp.status_code not in (200, 404):
                    result["error"] = f"Jackett вернул HTTP {resp.status_code}."
                    return result
                if resp.status_code == 200:
                    # Classify the response before attempting JSON parse so we can
                    # give an accurate error instead of a misleading one.
                    html_kind = _classify_response(resp)
                    if html_kind == "login":
                        result["error"] = (
                            "Jackett вернул страницу входа — API ключ не принят "
                            "(проверьте JACKETT_API_KEY)"
                        )
                        return result
                    if html_kind == "loading":
                        result["error"] = _STARTUP_DIAGNOSTIC_ERROR
                        if attempt == 0:
                            logger.debug("Jackett diagnostics returned startup response; retrying once")
                            continue
                        return result
                    try:
                        data = resp.json()
                    except ValueError:
                        # Body is not valid JSON and wasn't detected as HTML above —
                        # most likely Jackett is still initialising (empty body).
                        result["error"] = _STARTUP_DIAGNOSTIC_ERROR
                        if attempt == 0:
                            logger.debug("Jackett diagnostics returned non-JSON response; retrying once")
                            continue
                        return result
                    if isinstance(data, list):
                        result["api_ok"] = True
                        result["indexers"] = [
                            {"id": i.get("id", ""), "name": i.get("name") or i.get("id", "")}
                            for i in data
                            if i.get("configured")
                        ]
                return result
        except requests.ConnectionError:
            self._reset_session()
            result["error"] = f"Нет соединения с Jackett ({self._base_url})."
        except requests.Timeout:
            result["error"] = "Таймаут подключения к Jackett (>10 сек)."
        except requests.RequestException as e:
            result["error"] = f"Сетевая ошибка: {_sanitize_error_text(e, self._api_key)}"
        except KeyError as e:
            result["error"] = f"Ошибка разбора ответа: {e}"
        return result

    def _parse_json_results(self, json_text: str, limit: int | None = None) -> list[JackettResult]:
        """Parse Jackett JSON API response (/api/v2.0/indexers/all/results)."""
        try:
            data = __import__("json").loads(json_text)
        except ValueError as e:
            raise JackettError(f"Не удалось разобрать ответ Jackett (JSON): {e}") from e

        items = data.get("Results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise JackettError("Неожиданный формат ответа Jackett (нет поля Results)")

        results: list[JackettResult] = []
        for item in items:
            try:
                title = (item.get("Title") or "").strip()
                if not title:
                    continue

                size_bytes = int(item.get("Size") or 0)
                seeders = int(item.get("Seeders") or 0)
                tracker = (item.get("TrackerId") or item.get("Tracker") or "").strip()
                topic_url = (item.get("Details") or item.get("Guid") or "").strip()
                if not topic_url.startswith("http"):
                    topic_url = ""
                torrent_url = (item.get("Link") or "").strip() or None
                magnet_url = (item.get("MagnetUri") or "").strip() or None
                published_at = str(item.get("PublishDate") or item.get("FirstSeen") or "").strip()

                results.append(JackettResult(
                    title=title,
                    size=_fmt_size(size_bytes),
                    seeders=seeders,
                    tracker=tracker,
                    topic_url=topic_url,
                    magnet_url=magnet_url,
                    torrent_url=torrent_url,
                    published_at=published_at,
                ))

                if limit is not None and len(results) >= limit:
                    break
            except (AttributeError, TypeError, ValueError):
                logger.debug("Failed to parse Jackett JSON result item", exc_info=True)
                continue

        return results

    def _parse_results(self, xml_text: str, limit: int | None = None) -> list[JackettResult]:
        xml_text = _strip_invalid_xml(xml_text)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            # Log the exact position and character to aid diagnosis.
            line_no, col_no = getattr(e, "position", (0, 0))
            xml_lines = xml_text.splitlines()
            bad_char: str | None = None
            context_snippet = ""
            if 0 < line_no <= len(xml_lines):
                bad_line = xml_lines[line_no - 1]
                if 0 < col_no <= len(bad_line):
                    bad_char = bad_line[col_no - 1]
                context_snippet = bad_line[max(0, col_no - 30): col_no + 30]
            logger.error(
                "XML parse error at line=%d col=%d bad_char=%r (U+%04X) context=%r",
                line_no, col_no,
                bad_char, ord(bad_char) if bad_char else 0,
                context_snippet,
            )
            raise JackettError(f"Не удалось разобрать ответ Jackett: {e}") from e

        channel = root.find("channel")
        if channel is None:
            return []

        results: list[JackettResult] = []
        for item in channel.findall("item"):
            try:
                title = (item.findtext("title") or "").strip()
                if not title:
                    continue

                try:
                    size_bytes = int(item.findtext("size") or "0")
                except ValueError:
                    size_bytes = 0

                topic_url = (item.findtext("comments") or "").strip()
                if not topic_url.startswith("http"):
                    topic_url = (item.findtext("guid") or "").strip()
                if not topic_url.startswith("http"):
                    topic_url = ""

                torrent_url = (item.findtext("link") or "").strip() or None
                published_at = (item.findtext("pubDate") or "").strip()

                # torznab:attr elements — namespace-agnostic
                attrs: dict[str, str] = {}
                for child in item:
                    local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if local == "attr":
                        name = child.get("name", "")
                        value = child.get("value", "")
                        if name and value:
                            attrs[name] = value

                try:
                    seeders = int(attrs.get("seeders", "0") or "0")
                except ValueError:
                    seeders = 0

                magnet_url = attrs.get("magneturl") or None
                tracker = attrs.get("tracker") or attrs.get("indexer") or attrs.get("trackerId") or ""

                results.append(JackettResult(
                    title=title,
                    size=_fmt_size(size_bytes),
                    seeders=seeders,
                    tracker=tracker,
                    topic_url=topic_url,
                    magnet_url=magnet_url,
                    torrent_url=torrent_url,
                    published_at=published_at,
                ))

                effective_limit = limit if limit is not None else self._max_results
                if len(results) >= effective_limit:
                    break
            except (AttributeError, TypeError, ValueError):
                logger.debug("Failed to parse Jackett result item", exc_info=True)
                continue

        return results
