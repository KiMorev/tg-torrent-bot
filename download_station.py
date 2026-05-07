import json
import logging
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

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _raise_api_error(self, result: dict) -> None:
        error = result.get("error", {})
        code = error.get("code", "unknown")
        message = f"DSM API вернул ошибку {code}"

        if error.get("message"):
            message = f"{message}: {error['message']}"
        elif DS_ERROR_HINTS.get(code):
            message = f"{message}: {DS_ERROR_HINTS[code]}"

        raise DownloadStationError(message)

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
            raise DownloadStationError(f"DSM API недоступен после {self.retry_attempts} попыток: {last_error}") from last_error

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
