import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


logger = logging.getLogger("tg_torrent_drop")


DS_ERROR_HINTS = {
    400: "загрузка файла не удалась",
    401: "достигнут лимит задач",
    402: "нет доступа к папке назначения",
    403: "папка назначения не существует",
    406: "не задана папка загрузки по умолчанию; проверьте DS_DESTINATION",
    407: "не удалось установить папку назначения",
}
_SENSITIVE_QUERY_RE = re.compile(r"(?i)((?:passwd|password|_sid|sid|token|apikey)=)[^&\s]+")


def _sanitize_secret_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _SENSITIVE_QUERY_RE.sub(r"\1***", text)


class DownloadStationError(RuntimeError):
    pass


def _synchronized(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class DownloadStationClient:
    def __init__(
        self,
        base_url: str,
        account: str,
        password: str,
        destination: str,
        verify_ssl: bool = True,
        retry_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self.base_url = base_url
        self.account = account
        self.password = password
        self.destination = destination
        self.ssl_context = None if verify_ssl else ssl._create_unverified_context()
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay = max(0.0, retry_delay)
        self._lock = threading.RLock()
        # Volume-info cache: avoid hammering DSM with 5 calls back-to-back when
        # the user taps a few buttons in quick succession. 60 s is short enough
        # that a freshly-downloaded 30 GB rip will reflect in the next confirm.
        self._volume_cache: tuple[float, dict] | None = None
        self._volume_cache_ttl = 60.0

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _raise_api_error(self, result: dict) -> None:
        error = result.get("error", {})
        code = error.get("code", "unknown")
        message = f"DSM API вернул ошибку {code}"

        if error.get("message"):
            message = f"{message}: {self._sanitize_error(error['message'])}"
        elif DS_ERROR_HINTS.get(code):
            message = f"{message}: {DS_ERROR_HINTS[code]}"

        raise DownloadStationError(message)

    def _sanitize_error(self, value: object) -> str:
        return _sanitize_secret_text(value, (self.password,))

    def _read_json_response(self, request: urllib.request.Request, timeout: int = 30) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            if attempt:
                logger.warning(
                    "DSM API недоступен, повтор %d/%d через %.0fs...",
                    attempt,
                    self.retry_attempts - 1,
                    self.retry_delay,
                )
                time.sleep(self.retry_delay)
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                    payload = response.read().decode("utf-8")
                break
            except urllib.error.URLError as e:
                last_error = e
        else:
            safe_error = self._sanitize_error(last_error)
            raise DownloadStationError(f"DSM API недоступен после {self.retry_attempts} попыток: {safe_error}") from last_error

        try:
            result = json.loads(payload)
        except json.JSONDecodeError as e:
            raise DownloadStationError("DSM API вернул не JSON-ответ") from e

        if not result.get("success"):
            self._raise_api_error(result)

        return result

    def _request(self, path: str, params: dict[str, str], method: str = "GET") -> dict:
        data = None
        url = self._url(path)

        if method == "GET":
            url = f"{url}?{urllib.parse.urlencode(params)}"
        else:
            data = urllib.parse.urlencode(params).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        return self._read_json_response(request)

    def _multipart_request(
        self,
        path: str,
        sid: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
        filename: str,
    ) -> dict:
        boundary = f"----tg-torrent-drop-{int(time.time() * 1000)}"
        body = bytearray()

        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                "Content-Type: application/x-bittorrent\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        request = urllib.request.Request(
            f"{self._url(path)}?{urllib.parse.urlencode({'_sid': sid})}",
            data=bytes(body),
            method="POST",
            headers={
                "Accept": "*/*",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Cookie": f"id={sid}",
            },
        )
        return self._read_json_response(request, timeout=60)

    def _login(self) -> str:
        result = self._request(
            "/webapi/auth.cgi",
            {
                "api": "SYNO.API.Auth",
                "version": "6",
                "method": "login",
                "account": self.account,
                "passwd": self.password,
                "session": "DownloadStation",
                "format": "sid",
            },
            method="POST",
        )
        return result["data"]["sid"]

    def _logout(self, sid: str) -> None:
        try:
            self._request(
                "/webapi/auth.cgi",
                {
                    "api": "SYNO.API.Auth",
                    "version": "6",
                    "method": "logout",
                    "session": "DownloadStation",
                    "_sid": sid,
                },
            )
        except DownloadStationError:
            logger.warning("Download Station logout failed", exc_info=True)

    @_synchronized
    def create_magnet(self, magnet_uri: str) -> str:
        sid = self._login()
        try:
            result = self._request(
                "/webapi/DownloadStation/task.cgi",
                {
                    "api": "SYNO.DownloadStation.Task",
                    "version": "3",
                    "method": "create",
                    "uri": magnet_uri,
                    "destination": self.destination,
                    "_sid": sid,
                },
                method="POST",
            )
            task_ids = result.get("data", {}).get("task_id") or []
            task_id = task_ids[0] if task_ids else ""
            if task_id:
                self.resume_task(task_id, sid=sid, raise_on_error=False)
            return task_id
        finally:
            self._logout(sid)

    @_synchronized
    def create_torrent_file(self, file_path: Path, filename: str) -> str:
        sid = self._login()
        try:
            result = self._multipart_request(
                "/webapi/entry.cgi",
                sid,
                {
                    "api": "SYNO.DownloadStation2.Task",
                    "version": "2",
                    "method": "create",
                    "type": json.dumps("file"),
                    "file": json.dumps(["torrent"]),
                    "destination": json.dumps(self.destination),
                    "create_list": "false",
                },
                "torrent",
                file_path,
                filename,
            )

            task_ids = result.get("data", {}).get("task_id") or []
            task_id = task_ids[0] if task_ids else ""
            if task_id:
                self.resume_task(task_id, sid=sid, raise_on_error=False)

            return task_id
        finally:
            self._logout(sid)

    @_synchronized
    def resume_task(self, task_id: str, sid: str | None = None, raise_on_error: bool = True) -> None:
        self._task_action("resume", task_id, sid=sid, raise_on_error=raise_on_error)

    @_synchronized
    def pause_task(self, task_id: str, sid: str | None = None, raise_on_error: bool = True) -> None:
        self._task_action("pause", task_id, sid=sid, raise_on_error=raise_on_error)

    @_synchronized
    def delete_task(self, task_id: str) -> None:
        self._task_action("delete", task_id)

    @_synchronized
    def delete_tasks(self, task_ids: list[str]) -> None:
        self._tasks_action("delete", task_ids)

    @_synchronized
    def list_task_trackers(self, task_id: str) -> list[str]:
        sid = self._login()
        try:
            result = self._request(
                "/webapi/entry.cgi",
                {
                    "api": "SYNO.DownloadStation2.Task.BT.Tracker",
                    "version": "2",
                    "method": "list",
                    "task_id": task_id,
                    "limit": "-1",
                    "_sid": sid,
                },
            )
        finally:
            self._logout(sid)

        items = result.get("data", {}).get("items", [])
        trackers = []
        for item in items:
            if isinstance(item, dict) and item.get("url"):
                trackers.append(str(item["url"]))

        return trackers

    @_synchronized
    def add_task_trackers(self, task_id: str, trackers: list[str]) -> None:
        if not trackers:
            return

        sid = self._login()
        try:
            self._request(
                "/webapi/entry.cgi",
                {
                    "api": "SYNO.DownloadStation2.Task.BT.Tracker",
                    "version": "2",
                    "method": "add",
                    "task_id": task_id,
                    "tracker": json.dumps(trackers),
                    "_sid": sid,
                },
                method="POST",
            )
        finally:
            self._logout(sid)

    def _task_action(
        self,
        method: str,
        task_id: str,
        sid: str | None = None,
        raise_on_error: bool = True,
    ) -> None:
        self._tasks_action(method, [task_id], sid=sid, raise_on_error=raise_on_error)

    def _tasks_action(
        self,
        method: str,
        task_ids: list[str],
        sid: str | None = None,
        raise_on_error: bool = True,
    ) -> None:
        if not task_ids:
            return

        own_sid = sid is None
        if own_sid:
            sid = self._login()

        try:
            self._request(
                "/webapi/entry.cgi",
                {
                    "api": "SYNO.DownloadStation2.Task",
                    "version": "2",
                    "method": method,
                    "id": json.dumps(task_ids),
                    "_sid": sid,
                },
                method="POST",
            )
        except DownloadStationError:
            logger.warning("Failed to %s Download Station tasks %s", method, task_ids, exc_info=True)
            if raise_on_error:
                raise
        finally:
            if own_sid:
                self._logout(sid)

    @_synchronized
    def get_volume_info(self, *, use_cache: bool = True) -> dict | None:
        """Return free/total bytes for the volume hosting DS_DESTINATION.

        Returns ``{"total_bytes": int, "free_bytes": int, "used_pct": float,
        "mount_point": str}`` on success, or ``None`` if DSM doesn't support
        the API / destination doesn't map to a known volume / network failed.

        Graceful — caller treats None as «диск-чек недоступен, не блокируем».

        Cached for ``self._volume_cache_ttl`` seconds. Pass ``use_cache=False``
        to force a fresh query (e.g. from diagnostics page).
        """
        if use_cache and self._volume_cache is not None:
            ts, cached = self._volume_cache
            if time.monotonic() - ts < self._volume_cache_ttl:
                return cached

        try:
            sid = self._login()
        except DownloadStationError:
            logger.warning("Disk-space check: login failed", exc_info=True)
            return None

        try:
            try:
                result = self._request(
                    "/webapi/entry.cgi",
                    {
                        "api": "SYNO.Core.Storage.Volume",
                        "version": "1",
                        "method": "list",
                        "limit": "-1",
                        "offset": "0",
                        "_sid": sid,
                    },
                )
            except DownloadStationError as exc:
                # API not available on this DSM (older version, restricted user).
                logger.info("Disk-space check: Volume.list unavailable: %s", exc)
                return None

            volumes = result.get("data", {}).get("volumes") or []
            if not volumes:
                logger.info("Disk-space check: no volumes returned by DSM")
                return None

            # Match the volume whose `volume_path` is a prefix of our destination
            # path, longest match wins. Destination may be relative ("video")
            # or absolute ("/volume1/video") — try both.
            dest = self.destination or ""
            dest_abs = dest if dest.startswith("/") else f"/{dest}"

            best: dict | None = None
            best_len = -1
            for vol in volumes:
                if not isinstance(vol, dict):
                    continue
                vp = str(vol.get("volume_path") or "")
                if not vp:
                    continue
                if dest_abs.startswith(vp.rstrip("/") + "/") or dest_abs == vp:
                    if len(vp) > best_len:
                        best = vol
                        best_len = len(vp)

            # Fallback: if our destination doesn't match by prefix (relative
            # path with unknown volume), pick the largest single volume — most
            # Synologies have just one, so this is usually correct.
            if best is None and len(volumes) == 1:
                best = volumes[0] if isinstance(volumes[0], dict) else None

            if best is None:
                logger.info(
                    "Disk-space check: destination %r doesn't match any volume "
                    "(volumes=%s)",
                    dest, [v.get("volume_path") for v in volumes if isinstance(v, dict)],
                )
                return None

            try:
                total = int(best.get("size", {}).get("total") or 0)
                free = int(best.get("size", {}).get("free_inode") or
                           best.get("size", {}).get("free") or 0)
            except (TypeError, ValueError, AttributeError):
                logger.warning("Disk-space check: unparseable size in %r", best)
                return None

            if total <= 0:
                return None

            used_pct = max(0.0, min(100.0, 100.0 * (total - free) / total))
            info = {
                "total_bytes": total,
                "free_bytes": max(0, free),
                "used_pct": round(used_pct, 1),
                "mount_point": str(best.get("volume_path") or ""),
            }
            self._volume_cache = (time.monotonic(), info)
            return info
        finally:
            self._logout(sid)

    @_synchronized
    def list_tasks(self) -> list[dict]:
        sid = self._login()
        try:
            result = self._request(
                "/webapi/DownloadStation/task.cgi",
                {
                    "api": "SYNO.DownloadStation.Task",
                    "version": "2",
                    "method": "list",
                    "additional": "detail,transfer",
                    "_sid": sid,
                },
            )
            return result.get("data", {}).get("tasks", [])
        finally:
            self._logout(sid)
