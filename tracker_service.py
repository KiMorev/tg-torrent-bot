from __future__ import annotations

import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from download_station import DownloadStationError


DISABLED_TRACKER_MODES = {"0", "false", "no", "off", "disabled"}


@dataclass(frozen=True)
class TrackerConfig:
    mode: str
    url: str
    max_count: int
    cache_ttl_hours: int
    cache_file: Path
    background_enabled: bool


@dataclass
class TrackerApplyResult:
    added_count: int = 0
    available_count: int = 0
    cache_time: float | None = None
    skipped_reason: str = ""


def public_trackers_enabled(config: TrackerConfig) -> bool:
    return config.mode not in DISABLED_TRACKER_MODES and config.max_count > 0


def tracker_background_enabled(config: TrackerConfig) -> bool:
    return public_trackers_enabled(config) and config.background_enabled


def tracker_key(tracker: str) -> str:
    return tracker.strip().rstrip("/").lower()


def parse_trackers_text(text: str) -> list[str]:
    trackers = []
    seen = set()

    for raw_line in text.splitlines():
        tracker = raw_line.strip().lstrip("\ufeff")
        if not tracker or tracker.startswith("#"):
            continue
        if not tracker.lower().startswith(("udp://", "http://", "https://")):
            continue

        key = tracker_key(tracker)
        if key in seen:
            continue

        seen.add(key)
        trackers.append(tracker)

    return trackers


def is_tracker_task_candidate(task: dict, processed_ids: set[str]) -> bool:
    task_id = task.get("id")
    if not task_id or task_id in processed_ids:
        return False

    if (task.get("type") or "").lower() != "bt":
        return False

    status = (task.get("status") or "").lower()
    return status != "finished"


def tracker_attempt_is_final(result: TrackerApplyResult) -> bool:
    if result.added_count:
        return True
    if result.available_count and not result.skipped_reason:
        return True
    if result.skipped_reason.startswith("приватный torrent"):
        return True
    return False


def tracker_button_visible(
    task_id: str,
    status: str,
    task_type: str,
    *,
    background_enabled: bool,
    processed_ids: set[str],
) -> bool:
    if not task_id or not background_enabled:
        return False
    if (task_type or "").lower() != "bt":
        return False
    if (status or "").lower() == "finished":
        return False

    return task_id not in processed_ids


class TrackerService:
    def __init__(self, config: TrackerConfig, ds_client, logger: logging.Logger) -> None:
        self.config = config
        self.ds_client = ds_client
        self.logger = logger

    def public_trackers_enabled(self) -> bool:
        return public_trackers_enabled(self.config)

    def background_enabled(self) -> bool:
        return tracker_background_enabled(self.config)

    def read_cache(self, require_fresh: bool = True) -> tuple[list[str], float | None]:
        try:
            cache_time = self.config.cache_file.stat().st_mtime
        except OSError:
            return [], None

        max_age_seconds = self.config.cache_ttl_hours * 3600
        if require_fresh and time.time() - cache_time > max_age_seconds:
            return [], cache_time

        try:
            text = self.config.cache_file.read_text(encoding="utf-8")
        except OSError:
            return [], cache_time

        return parse_trackers_text(text)[:self.config.max_count], cache_time

    def write_cache(self, text: str) -> None:
        self.config.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.cache_file.write_text(text, encoding="utf-8")

    def load_public_trackers(self) -> tuple[list[str], float | None]:
        if not self.public_trackers_enabled():
            return [], None

        cached_trackers, cache_time = self.read_cache(require_fresh=True)
        if cached_trackers:
            return cached_trackers, cache_time

        try:
            request = urllib.request.Request(
                self.config.url,
                headers={"User-Agent": "tg-torrent-drop/1.0"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8")
            trackers = parse_trackers_text(text)[:self.config.max_count]
            if trackers:
                self.write_cache("\n".join(trackers) + "\n")
                return trackers, self.config.cache_file.stat().st_mtime
        except Exception:
            self.logger.warning("Failed to update public trackers list", exc_info=True)

        stale_trackers, stale_time = self.read_cache(require_fresh=False)
        return stale_trackers, stale_time

    def add_public_trackers_to_download_task(self, task_id: str) -> TrackerApplyResult:
        result = TrackerApplyResult()
        trackers, cache_time = self.load_public_trackers()
        result.available_count = len(trackers)
        result.cache_time = cache_time

        if not self.public_trackers_enabled():
            return result
        if not task_id:
            result.skipped_reason = "ID задачи пока не найден"
            return result
        if not trackers:
            result.skipped_reason = "список недоступен"
            return result

        try:
            existing = {tracker_key(tracker) for tracker in self.ds_client.list_task_trackers(task_id)}
            additions = [tracker for tracker in trackers if tracker_key(tracker) not in existing]
            if not additions:
                return result

            self.ds_client.add_task_trackers(task_id, additions)

            confirmed = set()
            for attempt in range(3):
                if attempt:
                    time.sleep(1)

                confirmed = {tracker_key(tracker) for tracker in self.ds_client.list_task_trackers(task_id)}
                if any(tracker_key(tracker) in confirmed for tracker in additions):
                    break

            result.added_count = sum(1 for tracker in additions if tracker_key(tracker) in confirmed)
            if not result.added_count:
                result.skipped_reason = "добавление не подтвердилось"
        except DownloadStationError:
            self.logger.warning("Failed to add public trackers to task %s", task_id, exc_info=True)
            result.skipped_reason = "Download Station API не принял список"

        return result


def format_tracker_cache_time(cache_time: float | None, display_timezone: tzinfo) -> str:
    if cache_time is None:
        return ""

    return datetime.fromtimestamp(cache_time, display_timezone).strftime("%d.%m.%Y %H:%M")


def tracker_result_lines(
    result: TrackerApplyResult | None,
    *,
    enabled: bool,
    display_timezone: tzinfo,
) -> list[str]:
    if not result or not enabled:
        return []

    if result.added_count:
        lines = [f"Public-трекеры: добавлено {result.added_count}"]
    elif result.skipped_reason:
        lines = [f"Public-трекеры: {result.skipped_reason}"]
    elif result.available_count:
        lines = ["Public-трекеры: новых нет"]
    else:
        lines = ["Public-трекеры: список недоступен"]

    cache_time = format_tracker_cache_time(result.cache_time, display_timezone)
    if cache_time:
        lines.append(f"Список трекеров: {cache_time}")

    return lines
