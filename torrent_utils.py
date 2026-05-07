import json
import logging
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any


logger = logging.getLogger("tg_torrent_drop")

SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")
MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"]+", re.IGNORECASE)


class RawBencode:
    def __init__(self, data: bytes) -> None:
        self.data = data


def safe_filename(name: str) -> str:
    name = (name or "download.torrent").strip()
    name = SAFE_NAME.sub("_", name)

    if not name.lower().endswith(".torrent"):
        name += ".torrent"

    return name[:200]


def temp_path(tmp_dir: Path, filename: str) -> Path:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    return tmp_dir / f"{int(time.time())}_{token}_{filename}"


def looks_like_torrent(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            header = file.read(16)
    except OSError:
        return False

    return header.startswith(b"d")


def find_magnet(text: str) -> str | None:
    match = MAGNET_RE.search(text or "")
    if not match:
        return None

    return match.group(0).rstrip(".,)")


def bdecode_value(data: bytes, index: int = 0) -> tuple[Any, int]:
    if index >= len(data):
        raise ValueError("unexpected end of bencode data")

    marker = data[index:index + 1]
    if marker == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if marker == b"l":
        index += 1
        values = []
        while data[index:index + 1] != b"e":
            value, index = bdecode_value(data, index)
            values.append(value)
        return values, index + 1
    if marker == b"d":
        index += 1
        values = {}
        while data[index:index + 1] != b"e":
            key, index = bdecode_value(data, index)
            value, index = bdecode_value(data, index)
            values[key] = value
        return values, index + 1
    if marker.isdigit():
        separator = data.index(b":", index)
        length = int(data[index:separator])
        start = separator + 1
        end = start + length
        return data[start:end], end

    raise ValueError(f"unexpected bencode marker {marker!r}")


def bdecode_torrent(data: bytes) -> tuple[dict[bytes, Any], dict[bytes, Any] | None]:
    if not data.startswith(b"d"):
        raise ValueError("torrent root must be a dictionary")

    index = 1
    root: dict[bytes, Any] = {}
    parsed_info = None

    while index < len(data) and data[index:index + 1] != b"e":
        key, index = bdecode_value(data, index)
        if not isinstance(key, bytes):
            raise ValueError("torrent dictionary key must be bytes")

        value_start = index
        value, index = bdecode_value(data, index)
        if key == b"info":
            parsed_info = value if isinstance(value, dict) else None
            root[key] = RawBencode(data[value_start:index])
        else:
            root[key] = value

    if index >= len(data) or data[index:index + 1] != b"e":
        raise ValueError("unterminated torrent dictionary")

    return root, parsed_info


def torrent_is_private(parsed_info: dict[bytes, Any] | None) -> bool:
    return bool(isinstance(parsed_info, dict) and parsed_info.get(b"private") == 1)


def torrent_file_is_private(path: Path) -> bool:
    try:
        _, parsed_info = bdecode_torrent(path.read_bytes())
    except Exception:
        logger.warning("Failed to parse torrent privacy flag: %s", path, exc_info=True)
        return False

    return torrent_is_private(parsed_info)


def magnet_info_hash(magnet_uri: str) -> str:
    parsed = urllib.parse.urlparse(magnet_uri)
    query = urllib.parse.parse_qs(parsed.query)
    for value in query.get("xt", []):
        prefix = "urn:btih:"
        if value.lower().startswith(prefix):
            return value[len(prefix):].lower()

    return ""


def task_matches_magnet(task: dict, magnet_uri: str) -> bool:
    info_hash = magnet_info_hash(magnet_uri)
    if not info_hash:
        return False

    task_text = json.dumps(task, ensure_ascii=False).lower()
    return info_hash in task_text


def find_magnet_task_id(tasks: list[dict], magnet_uri: str, known_task_ids: set[str]) -> str:
    for task in tasks:
        task_id = task.get("id")
        if task_id and task_id not in known_task_ids and task_matches_magnet(task, magnet_uri):
            return task_id

    for task in tasks:
        task_id = task.get("id")
        if task_id and task_matches_magnet(task, magnet_uri):
            return task_id

    if known_task_ids:
        for task in tasks:
            task_id = task.get("id")
            if task_id and task_id not in known_task_ids:
                return task_id

    return ""
