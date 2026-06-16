import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from state_store import JsonStateStore


def _make_store(tmp_dir: str) -> JsonStateStore:
    d = Path(tmp_dir)
    return JsonStateStore(
        approved_chat_ids_file=d / "approved.json",
        tracker_processed_file=d / "tracker.json",
        task_owners_file=d / "owners.json",
        notified_tasks_file=d / "notified.json",
        auto_delete_tasks_file=d / "auto_delete.json",
        topic_subscriptions_file=d / "subscriptions.json",
        task_meta_file=d / "task_meta.json",
        pending_downloads_file=d / "pending_downloads.json",
        series_bulk_jobs_file=d / "series_bulk_jobs.json",
        series_continue_totals_file=d / "series_continue_totals.json",
        series_continue_hidden_file=d / "series_continue_hidden.json",
        download_history_file=d / "download_history.jsonl",
        jackett_guard_file=d / "jackett_guard.json",
    )


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_approved_chat_ids_roundtrip(self) -> None:
        ids = {100, 200, -300}
        self.store.save_approved_chat_ids(ids)
        self.assertEqual(self.store.load_approved_chat_ids(), ids)

    def test_task_owner_remember_and_lookup(self) -> None:
        self.store.remember_task_owner("task1", 111)
        self.store.remember_task_owner("task2", 222)
        owners = self.store.load_task_owners()
        self.assertEqual(owners["task1"], 111)
        self.assertEqual(owners["task2"], 222)

    def test_forget_task_state_cleans_all_stores(self) -> None:
        self.store.remember_task_owner("tid1", 1)
        self.store.add_tracker_processed_ids({"tid1"})
        self.store.save_notified_tasks({"tid1": "finished"})
        self.store.save_auto_delete_tasks({"tid1": 1234.0})
        self.store.remember_task_meta("tid1", {"kind": "movie", "title": "X"})

        self.store.forget_task_state(["tid1"])

        self.assertNotIn("tid1", self.store.load_task_owners())
        self.assertNotIn("tid1", self.store.load_tracker_processed_ids())
        self.assertNotIn("tid1", self.store.load_notified_tasks())
        self.assertNotIn("tid1", self.store.load_auto_delete_tasks())
        self.assertNotIn("tid1", self.store.load_task_meta())

    def test_pending_downloads_roundtrip(self) -> None:
        entries = {
            "abc123": {
                "chat_id": 100,
                "added_at": "2026-05-17T10:00:00+03:00",
                "title": "Test Movie",
                "topic_url": "https://rutracker.org/forum/viewtopic.php?t=12345",
                "torrent_url": "http://jackett:9117/dl/rutracker/?path=Q",
                "magnet_url": None,
                "tracker": "rutracker",
                "source": "jackett",
                "subscribe": False,
                "attempts": 2,
                "last_attempt_at": "2026-05-17T11:00:00+03:00",
                "last_error": "HTTP 404",
            },
            "def456": {
                "chat_id": 200,
                "added_at": "2026-05-17T09:00:00+03:00",
                "title": "Another",
                "topic_url": "",
                "torrent_url": "",
                "magnet_url": "magnet:?xt=urn:btih:deadbeef",
                "tracker": "public",
                "source": "jackett",
                "subscribe": False,
                "attempts": 0,
                "last_attempt_at": None,
                "last_error": "",
            },
        }
        self.store.save_pending_downloads(entries)
        loaded = self.store.load_pending_downloads()
        self.assertEqual(loaded, entries)

    def test_pending_downloads_empty_when_missing(self) -> None:
        self.assertEqual(self.store.load_pending_downloads(), {})

    def test_jackett_guard_roundtrip(self) -> None:
        payload = {
            "version": 1,
            "indexers": {
                "kinozal": {"state": "degraded", "fail_streak": 2},
            },
        }

        self.store.save_jackett_guard(payload)

        self.assertEqual(self.store.load_jackett_guard(), payload)

    def test_pending_downloads_ignores_non_dict_entries(self) -> None:
        # Save raw bad payload, then verify load skips it.
        self.store.save_json_file(
            self.store.pending_downloads_file,
            {"good": {"chat_id": 1}, "bad": "string entry", "alsobad": 42},
            "test",
        )
        loaded = self.store.load_pending_downloads()
        self.assertIn("good", loaded)
        self.assertNotIn("bad", loaded)
        self.assertNotIn("alsobad", loaded)

    def test_series_bulk_jobs_roundtrip(self) -> None:
        jobs = {
            "bulk_b": {
                "id": "bulk_b",
                "chat_id": 100,
                "series_title": "Клиника",
                "status": "planned",
                "profile": {"quality": "1080p", "require_original": True},
                "seasons": {
                    "1": {
                        "status": "selected",
                        "task_id": "task_1",
                        "result": {"title": "Клиника / Сезон: 1"},
                    },
                    "2": {
                        "status": "needs_decision",
                        "resolved": "пропущен",
                    },
                },
            },
            "bulk_a": {
                "id": "bulk_a",
                "chat_id": 200,
                "series_title": "Фарго",
                "status": "running",
                "seasons": {},
            },
        }

        self.store.save_series_bulk_jobs(jobs)
        loaded = self.store.load_series_bulk_jobs()

        self.assertEqual(loaded, jobs)

    def test_series_bulk_jobs_empty_when_missing(self) -> None:
        self.assertEqual(self.store.load_series_bulk_jobs(), {})

    def test_series_bulk_jobs_ignores_non_dict_entries(self) -> None:
        self.store.save_json_file(
            self.store.series_bulk_jobs_file,
            {"good": {"series_title": "Клиника"}, "bad": "string", "alsobad": 42},
            "test",
        )

        loaded = self.store.load_series_bulk_jobs()

        self.assertEqual(loaded, {"good": {"series_title": "Клиника"}})

    def test_series_continue_totals_roundtrip(self) -> None:
        totals = {"show-key": {"5": 8}}

        self.store.save_series_continue_totals(totals)

        self.assertEqual(self.store.load_series_continue_totals(), totals)

    def test_series_continue_hidden_roundtrip(self) -> None:
        hidden = {"100": ["show-1:S05", "show-2:S01"], "200": ["show-3:S02"]}

        self.store.save_series_continue_hidden(hidden)

        self.assertEqual(self.store.load_series_continue_hidden(), hidden)

    def test_download_history_append_load_filter_and_limit(self) -> None:
        self.store.append_download_history({"event": "download_added", "chat_id": 100, "title": "A"})
        self.store.append_download_history({"event": "plex_found", "chat_ids": [200, 300], "title": "B"})
        self.store.append_download_history({"event": "download_added", "chat_id": 100, "title": "C"})

        self.assertEqual([e["title"] for e in self.store.load_download_history()], ["A", "B", "C"])
        self.assertEqual([e["title"] for e in self.store.load_download_history(chat_id=200)], ["B"])
        self.assertEqual([e["title"] for e in self.store.load_download_history(chat_id=100, limit=1)], ["C"])

    def test_download_history_skips_malformed_lines(self) -> None:
        self.store.append_download_history({"event": "download_added", "chat_id": 100, "title": "A"})
        with self.store.download_history_file.open("a", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write("[]\n")
        self.store.append_download_history({"event": "download_completed", "chat_id": 100, "title": "B"})

        with self.assertLogs("tg_torrent_drop", level="WARNING"):
            loaded = self.store.load_download_history(chat_id=100)

        self.assertEqual([e["title"] for e in loaded], ["A", "B"])

    def test_find_latest_download_history_matches_user_and_series(self) -> None:
        self.store.append_download_history({
            "event": "download_added",
            "chat_id": 100,
            "kind": "series",
            "series_query": "Show",
            "title": "old",
        })
        self.store.append_download_history({
            "event": "download_added",
            "chat_id": 200,
            "kind": "series",
            "series_query": "Show",
            "title": "other-user",
        })
        self.store.append_download_history({
            "event": "download_added",
            "chat_id": 100,
            "kind": "series",
            "series_query": "Show",
            "title": "latest",
        })

        found = self.store.find_latest_download_history(
            100,
            kind="series",
            series_query="show",
        )

        self.assertEqual(found["title"], "latest")

    def test_task_meta_roundtrip(self) -> None:
        self.store.remember_task_meta("tid1", {
            "kind": "series", "title": "Клиника", "year": 2001,
            "quality": "1080", "series_query": "Клиника", "season_num": 3,
            "source": "search",
        })
        self.store.remember_task_meta("tid2", {
            "kind": "movie", "title": "Dune", "year": 2024,
            "quality": "4k", "source": "magnet",
        })
        meta = self.store.load_task_meta()
        self.assertEqual(meta["tid1"]["kind"], "series")
        self.assertEqual(meta["tid1"]["season_num"], 3)
        self.assertEqual(meta["tid2"]["kind"], "movie")
        self.assertEqual(meta["tid2"]["year"], 2024)

    def test_remember_task_meta_skips_duplicate_entries(self) -> None:
        """Repeated remember_task_meta with the same entry should not rewrite the file."""
        entry = {"kind": "movie", "title": "Dune", "year": 2024, "quality": "1080", "source": "search"}
        self.store.remember_task_meta("tid1", entry)
        with patch.object(self.store, "save_task_meta") as save_mock:
            self.store.remember_task_meta("tid1", entry)
        save_mock.assert_not_called()

    def test_remember_task_meta_ignores_empty_inputs(self) -> None:
        self.store.remember_task_meta("", {"kind": "movie"})  # empty task_id
        self.store.remember_task_meta("tid", None)  # None entry
        self.assertEqual(self.store.load_task_meta(), {})

    def test_prune_stale_task_state_drops_stale_task_meta(self) -> None:
        self.store.remember_task_meta("active1", {"kind": "movie", "title": "A"})
        self.store.remember_task_meta("stale1", {"kind": "series", "title": "B"})
        self.store.prune_stale_task_state({"active1"})
        meta = self.store.load_task_meta()
        self.assertIn("active1", meta)
        self.assertNotIn("stale1", meta)

    def test_prune_stale_task_state_removes_only_inactive(self) -> None:
        self.store.remember_task_owner("active1", 1)
        self.store.remember_task_owner("stale1", 2)
        self.store.add_tracker_processed_ids({"active1", "stale1"})
        self.store.save_notified_tasks({"active1": "finished", "stale1": "error"})
        self.store.save_auto_delete_tasks({"active1": 1.0, "stale1": 2.0})

        self.store.prune_stale_task_state({"active1"})

        self.assertIn("active1", self.store.load_task_owners())
        self.assertNotIn("stale1", self.store.load_task_owners())
        self.assertIn("active1", self.store.load_tracker_processed_ids())
        self.assertNotIn("stale1", self.store.load_tracker_processed_ids())
        self.assertIn("active1", self.store.load_notified_tasks())
        self.assertNotIn("stale1", self.store.load_notified_tasks())
        self.assertIn("active1", self.store.load_auto_delete_tasks())
        self.assertNotIn("stale1", self.store.load_auto_delete_tasks())

    def test_atomic_write_preserves_old_file_on_failure(self) -> None:
        self.store.save_approved_chat_ids({42})
        original_content = self.store.load_approved_chat_ids()

        with patch("os.replace", side_effect=OSError("disk full")):
            self.store.save_approved_chat_ids({99})

        self.assertEqual(self.store.load_approved_chat_ids(), original_content)

    def test_load_json_file_missing_is_silent_default(self) -> None:
        missing = Path(self._tmp.name) / "missing.json"
        default = {"fallback": True}

        with self.assertNoLogs("tg_torrent_drop", level="WARNING"):
            result = self.store.load_json_file(missing, default)

        self.assertEqual(result, default)

    def test_load_json_file_malformed_logs_warning_and_returns_default(self) -> None:
        broken = Path(self._tmp.name) / "broken.json"
        broken.write_text("{not valid json", encoding="utf-8")
        default = {"fallback": True}

        with self.assertLogs("tg_torrent_drop", level="WARNING") as captured:
            result = self.store.load_json_file(broken, default)

        self.assertEqual(result, default)
        joined = "\n".join(captured.output)
        self.assertIn("Malformed JSON", joined)
        self.assertIn("broken.json", joined)

    def test_save_json_file_serialization_error_preserves_old_file(self) -> None:
        path = Path(self._tmp.name) / "custom.json"
        self.store.save_json_file(path, {"ok": True}, "custom state")
        original_text = path.read_text(encoding="utf-8")

        with self.assertLogs("tg_torrent_drop", level="WARNING") as captured:
            self.store.save_json_file(path, {"bad": object()}, "custom state")

        self.assertEqual(path.read_text(encoding="utf-8"), original_text)
        self.assertIn("Failed to save custom state", "\n".join(captured.output))

    def test_add_tracker_processed_ids_deduplication(self) -> None:
        self.store.add_tracker_processed_ids({"t1", "t2"})
        self.store.add_tracker_processed_ids({"t2", "t3"})
        result = self.store.load_tracker_processed_ids()
        self.assertEqual(result, {"t1", "t2", "t3"})

    # --- approved users ---

    def test_add_approved_user_stores_name_and_date(self) -> None:
        self.store.add_approved_user(555, "John Doe @johndoe")
        users = self.store.load_approved_users()
        self.assertIn(555, users)
        self.assertEqual(users[555]["name"], "John Doe @johndoe")
        self.assertTrue(users[555]["added_at"])  # дата заполнена

    def test_remove_approved_user(self) -> None:
        self.store.add_approved_user(555, "Alice")
        self.store.add_approved_user(666, "Bob")
        self.store.remove_approved_user(555)
        self.assertNotIn(555, self.store.load_approved_users())
        self.assertIn(666, self.store.load_approved_users())

    def test_load_approved_chat_ids_reflects_approved_users(self) -> None:
        self.store.add_approved_user(100, "User A")
        self.store.add_approved_user(200, "User B")
        self.assertEqual(self.store.load_approved_chat_ids(), {100, 200})

    def test_backward_compat_old_list_format(self) -> None:
        """Старый формат [id, id] должен корректно загружаться."""
        import json
        self.store.approved_chat_ids_file.parent.mkdir(parents=True, exist_ok=True)
        self.store.approved_chat_ids_file.write_text(json.dumps([111, 222, 333]), encoding="utf-8")
        self.assertEqual(self.store.load_approved_chat_ids(), {111, 222, 333})
        users = self.store.load_approved_users()
        self.assertEqual(set(users.keys()), {111, 222, 333})
        self.assertEqual(users[111]["name"], "")  # имя пустое — старый формат

    def test_save_approved_chat_ids_preserves_existing_names(self) -> None:
        self.store.add_approved_user(100, "Alice")
        self.store.add_approved_user(200, "Bob")
        # убираем 200 и добавляем 300 через старый API
        self.store.save_approved_chat_ids({100, 300})
        users = self.store.load_approved_users()
        self.assertEqual(users[100]["name"], "Alice")  # имя сохранилось
        self.assertEqual(users[300]["name"], "")       # новый без имени
        self.assertNotIn(200, users)

    # --- size caps ---

    def test_notified_tasks_trimmed_to_max(self) -> None:
        """Saving more than _MAX_NOTIFIED_TASKS entries keeps only the most recent ones."""
        from state_store import _MAX_NOTIFIED_TASKS

        n = _MAX_NOTIFIED_TASKS + 50
        # Build dict in insertion order t0..t(n-1)
        tasks = {f"t{i}": "finished" for i in range(n)}
        self.store.save_notified_tasks(tasks)
        loaded = self.store.load_notified_tasks()

        self.assertEqual(len(loaded), _MAX_NOTIFIED_TASKS)
        # Most recent entries must survive
        self.assertIn(f"t{n - 1}", loaded)
        # Oldest entries must be dropped
        self.assertNotIn("t0", loaded)

    def test_tracker_processed_trimmed_to_max(self) -> None:
        """Saving more than _MAX_TRACKER_PROCESSED IDs trims to the cap."""
        from state_store import _MAX_TRACKER_PROCESSED

        ids = {f"task_{i:06d}" for i in range(_MAX_TRACKER_PROCESSED + 100)}
        self.store.save_tracker_processed_ids(ids)
        loaded = self.store.load_tracker_processed_ids()

        self.assertEqual(len(loaded), _MAX_TRACKER_PROCESSED)

    def test_notified_tasks_under_max_not_trimmed(self) -> None:
        """When count is at or below the cap, nothing is removed."""
        from state_store import _MAX_NOTIFIED_TASKS

        tasks = {f"t{i}": "finished" for i in range(_MAX_NOTIFIED_TASKS)}
        self.store.save_notified_tasks(tasks)
        loaded = self.store.load_notified_tasks()

        self.assertEqual(len(loaded), _MAX_NOTIFIED_TASKS)

    def test_notified_tasks_preserve_per_recipient_state(self) -> None:
        self.store.save_notified_tasks({
            "tid1": {
                "status": "done",
                "sent": ["100"],
                "failures": {"999": 2},
            }
        })

        self.assertEqual(
            self.store.load_notified_tasks(),
            {
                "tid1": {
                    "status": "done",
                    "sent": ["100"],
                    "failures": {"999": 2},
                }
            },
        )


    def test_notified_tasks_preserve_subscribers(self) -> None:
        """Subscriber lists survive a save/load round-trip."""
        self.store.save_notified_tasks({
            "tid2": {
                "status": "",
                "sent": [],
                "failures": {},
                "subscribers": ["888", "777"],
            }
        })
        loaded = self.store.load_notified_tasks()
        self.assertIn("tid2", loaded)
        entry = loaded["tid2"]
        self.assertEqual(sorted(entry["subscribers"]), ["777", "888"])
        self.assertEqual(entry["status"], "")

    def test_notified_tasks_preserve_plex_poll_delivery_state(self) -> None:
        self.store.save_notified_tasks({
            "tid3": {
                "status": "",
                "sent": [],
                "failures": {},
                "plex_poll": {"found": ["100"], "timeout": ["200"]},
            }
        })
        loaded = self.store.load_notified_tasks()
        self.assertEqual(
            loaded["tid3"]["plex_poll"],
            {"found": ["100"], "timeout": ["200"]},
        )

    def test_notified_tasks_subscriber_only_entry_not_dropped(self) -> None:
        """An entry with no status but with subscribers must not be silently discarded."""
        self.store.save_notified_tasks({
            "tid3": {"status": "", "sent": [], "failures": {}, "subscribers": ["999"]},
        })
        loaded = self.store.load_notified_tasks()
        self.assertIn("tid3", loaded)

    def test_notified_tasks_empty_entry_is_dropped(self) -> None:
        """An entry with no status AND no subscribers is pruned on load."""
        self.store.save_notified_tasks({
            "tid4": {"status": "", "sent": [], "failures": {}, "subscribers": []},
        })
        loaded = self.store.load_notified_tasks()
        self.assertNotIn("tid4", loaded)


class MovieDiscoverySettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.store = JsonStateStore(
            approved_chat_ids_file=d / "approved.json",
            tracker_processed_file=d / "tracker.json",
            task_owners_file=d / "owners.json",
            notified_tasks_file=d / "notified.json",
            auto_delete_tasks_file=d / "auto_delete.json",
            movie_discovery_settings_file=d / "md_settings.json",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_returns_empty_dict_when_no_file(self) -> None:
        result = self.store.load_movie_discovery_settings()
        self.assertEqual(result, {})

    def test_roundtrip_settings(self) -> None:
        settings = {
            "jackett_trackers_enabled": ["kinozal", "rutracker"],
            "jackett_trackers_known": ["kinozal", "rutracker", "torrenty"],
        }
        self.store.save_movie_discovery_settings(settings)
        loaded = self.store.load_movie_discovery_settings()
        self.assertEqual(loaded["jackett_trackers_enabled"], ["kinozal", "rutracker"])
        self.assertEqual(loaded["jackett_trackers_known"], ["kinozal", "rutracker", "torrenty"])

    def test_save_none_enabled_roundtrips(self) -> None:
        settings = {"jackett_trackers_enabled": None, "jackett_trackers_known": ["kinozal"]}
        self.store.save_movie_discovery_settings(settings)
        loaded = self.store.load_movie_discovery_settings()
        self.assertIsNone(loaded["jackett_trackers_enabled"])

    def test_load_returns_empty_when_no_file_configured(self) -> None:
        store_no_file = JsonStateStore(
            approved_chat_ids_file=Path(self._tmp.name) / "a.json",
            tracker_processed_file=Path(self._tmp.name) / "b.json",
            task_owners_file=Path(self._tmp.name) / "c.json",
            notified_tasks_file=Path(self._tmp.name) / "d.json",
            auto_delete_tasks_file=Path(self._tmp.name) / "e.json",
        )
        self.assertEqual(store_no_file.load_movie_discovery_settings(), {})


if __name__ == "__main__":
    unittest.main()
