import unittest
from dataclasses import dataclass

from jackett_guard import (
    STATE_DEGRADED,
    STATE_MANUAL_REQUIRED,
    STATE_OK,
    STATE_QUARANTINED,
    due_indexer_ids,
    next_due_delay,
    record_batch_failure,
    record_failure,
    record_statuses,
    record_success,
    unready_indexer_ids,
    unready_summary,
)


@dataclass
class _Status:
    indexer_id: str
    name: str
    status: int
    results: int
    error: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == 0 or self.results > 0


class JackettGuardTests(unittest.TestCase):
    def test_transient_failures_progress_to_quarantine_and_recover(self) -> None:
        state = {}
        state, _ = record_failure(state, "kinozal", error_kind="timeout", error="timeout", now=1000)
        self.assertEqual(state["indexers"]["kinozal"]["state"], STATE_DEGRADED)
        self.assertEqual(state["indexers"]["kinozal"]["fail_streak"], 1)

        state, _ = record_failure(state, "kinozal", error_kind="timeout", error="timeout", now=1060)
        self.assertEqual(state["indexers"]["kinozal"]["state"], STATE_DEGRADED)
        self.assertEqual(state["indexers"]["kinozal"]["fail_streak"], 2)

        state, _ = record_failure(state, "kinozal", error_kind="timeout", error="timeout", now=1240)
        self.assertEqual(state["indexers"]["kinozal"]["state"], STATE_QUARANTINED)
        self.assertEqual(state["indexers"]["kinozal"]["fail_streak"], 3)
        self.assertEqual(unready_indexer_ids(state), {"kinozal"})

        state, event = record_success(state, "kinozal", name="Kinozal", results=12, now=2000)
        self.assertEqual(state["indexers"]["kinozal"]["state"], STATE_OK)
        self.assertEqual(state["indexers"]["kinozal"]["fail_streak"], 0)
        self.assertEqual(event["kind"], "recovered")

    def test_manual_required_uses_slow_recheck(self) -> None:
        state, _ = record_failure(
            {},
            "noname-club",
            error_kind="status_1",
            error="Cloudflare protected",
            now=1000,
        )

        entry = state["indexers"]["noname-club"]
        self.assertEqual(entry["state"], STATE_MANUAL_REQUIRED)
        self.assertEqual(entry["next_retry_ts"], 1000 + 12 * 3600)
        self.assertEqual(due_indexer_ids(state, now=2000), [])

    def test_unready_summary_splits_enabled_and_disabled(self) -> None:
        state, _ = record_batch_failure(
            {},
            ["rutracker", "noname-club"],
            error_kind="timeout",
            error="timeout",
            source="movie_discovery",
            query="2026 1080p",
            now=1000,
        )

        summary = unready_summary(state, {"rutracker"})

        self.assertEqual(summary["enabled"], ["rutracker"])
        self.assertEqual(summary["disabled"], ["noname-club"])

    def test_record_statuses_recovers_only_bad_indexer(self) -> None:
        state, _ = record_failure({}, "kinozal", error_kind="timeout", error="timeout", now=1000)
        state, events = record_statuses(
            state,
            [
                _Status("kinozal", "Kinozal", status=0, results=5),
                _Status("rutracker", "RuTracker", status=1, results=0, error="timeout"),
            ],
            source="warmup",
            query="1080p",
            now=1100,
        )

        self.assertEqual(state["indexers"]["kinozal"]["state"], STATE_OK)
        self.assertEqual(state["indexers"]["rutracker"]["state"], STATE_DEGRADED)
        self.assertEqual([event["indexer_id"] for event in events], ["kinozal", "rutracker"])

    def test_due_indexers_and_delay_follow_next_retry(self) -> None:
        state, _ = record_failure({}, "kinozal", error_kind="timeout", error="timeout", now=1000)

        self.assertEqual(due_indexer_ids(state, now=1050), [])
        self.assertEqual(due_indexer_ids(state, now=1060), ["kinozal"])
        self.assertEqual(next_due_delay(state, default=900, now=1050), 10)
