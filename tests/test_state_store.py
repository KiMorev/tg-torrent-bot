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

        self.store.forget_task_state(["tid1"])

        self.assertNotIn("tid1", self.store.load_task_owners())
        self.assertNotIn("tid1", self.store.load_tracker_processed_ids())
        self.assertNotIn("tid1", self.store.load_notified_tasks())
        self.assertNotIn("tid1", self.store.load_auto_delete_tasks())

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


if __name__ == "__main__":
    unittest.main()
