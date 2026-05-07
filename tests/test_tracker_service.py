import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from download_station import DownloadStationError
from tracker_service import (
    TrackerApplyResult,
    TrackerConfig,
    TrackerService,
    is_tracker_task_candidate,
    parse_trackers_text,
    tracker_attempt_is_final,
    tracker_button_visible,
    tracker_key,
    tracker_result_lines,
)


class FakeDownloadStation:
    def __init__(self, existing: list[str] | None = None, fail: bool = False) -> None:
        self.trackers = list(existing or [])
        self.fail = fail
        self.added: list[str] = []

    def list_task_trackers(self, task_id: str) -> list[str]:
        if self.fail:
            raise DownloadStationError("boom")
        return list(self.trackers)

    def add_task_trackers(self, task_id: str, trackers: list[str]) -> None:
        if self.fail:
            raise DownloadStationError("boom")
        self.added.extend(trackers)
        self.trackers.extend(trackers)


def _config(cache_file: Path, *, max_count: int = 20) -> TrackerConfig:
    return TrackerConfig(
        mode="auto",
        url="http://trackers.local/list.txt",
        max_count=max_count,
        cache_ttl_hours=24,
        cache_file=cache_file,
        background_enabled=True,
    )


class TrackerServiceTests(unittest.TestCase):
    def test_parse_trackers_text_filters_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_trackers_text(
                "\ufeffudp://tracker.one/announce\n"
                "# comment\n"
                "not-a-tracker\n"
                "UDP://tracker.one/announce/\n"
                "https://tracker.two/announce\n"
            ),
            ["udp://tracker.one/announce", "https://tracker.two/announce"],
        )
        self.assertEqual(tracker_key(" UDP://tracker.one/announce/ "), "udp://tracker.one/announce")

    def test_add_public_trackers_only_sends_new_trackers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "trackers.txt"
            cache_file.write_text(
                "udp://tracker.one/announce\n"
                "udp://tracker.two/announce\n",
                encoding="utf-8",
            )
            ds = FakeDownloadStation(existing=["udp://tracker.one/announce/"])
            service = TrackerService(_config(cache_file), ds, logging.getLogger("test"))

            result = service.add_public_trackers_to_download_task("tid1")

        self.assertEqual(result.available_count, 2)
        self.assertEqual(result.added_count, 1)
        self.assertEqual(ds.added, ["udp://tracker.two/announce"])

    def test_add_public_trackers_reports_download_station_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "trackers.txt"
            cache_file.write_text("udp://tracker.one/announce\n", encoding="utf-8")
            service = TrackerService(
                _config(cache_file),
                FakeDownloadStation(fail=True),
                MagicMock(),
            )

            result = service.add_public_trackers_to_download_task("tid1")

        self.assertEqual(result.skipped_reason, "Download Station API не принял список")

    def test_candidate_final_and_button_rules(self) -> None:
        self.assertTrue(is_tracker_task_candidate({"id": "tid1", "type": "bt", "status": "downloading"}, set()))
        self.assertFalse(is_tracker_task_candidate({"id": "tid1", "type": "http", "status": "downloading"}, set()))
        self.assertFalse(is_tracker_task_candidate({"id": "tid1", "type": "bt", "status": "finished"}, set()))
        self.assertFalse(is_tracker_task_candidate({"id": "tid1", "type": "bt", "status": "downloading"}, {"tid1"}))

        self.assertTrue(tracker_attempt_is_final(TrackerApplyResult(added_count=1)))
        self.assertTrue(tracker_attempt_is_final(TrackerApplyResult(available_count=1)))
        self.assertTrue(tracker_attempt_is_final(TrackerApplyResult(skipped_reason="приватный torrent, не добавляю")))
        self.assertFalse(tracker_attempt_is_final(TrackerApplyResult(skipped_reason="список недоступен")))

        self.assertTrue(
            tracker_button_visible(
                "tid1",
                "downloading",
                "bt",
                background_enabled=True,
                processed_ids=set(),
            )
        )
        self.assertFalse(
            tracker_button_visible(
                "tid1",
                "finished",
                "bt",
                background_enabled=True,
                processed_ids=set(),
            )
        )

    def test_tracker_result_lines_respect_disabled_mode(self) -> None:
        result = TrackerApplyResult(added_count=2, available_count=2, cache_time=0)

        self.assertEqual(
            tracker_result_lines(result, enabled=False, display_timezone=None),
            [],
        )


if __name__ == "__main__":
    unittest.main()
