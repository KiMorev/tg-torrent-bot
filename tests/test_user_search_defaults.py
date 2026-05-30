import tempfile
import unittest
from pathlib import Path

from state_store import JsonStateStore


def _make_store(tmp_dir: str) -> JsonStateStore:
    root = Path(tmp_dir)
    return JsonStateStore(
        approved_chat_ids_file=root / "approved.json",
        tracker_processed_file=root / "tracker.json",
        task_owners_file=root / "owners.json",
        notified_tasks_file=root / "notified.json",
        auto_delete_tasks_file=root / "auto_delete.json",
        user_search_defaults_file=root / "user_search_defaults.json",
    )


class UserSearchDefaultsStoreTests(unittest.TestCase):
    def test_save_and_load_user_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            store.save_user_search_defaults(100, {
                "quality": "4K",
                "audio": True,
                "subs": False,
                "preferred_voices": ["LostFilm", "NewStudio", "Extra"],
            })

            loaded = store.load_user_search_defaults(100)
            self.assertEqual(loaded["quality"], "4K")
            self.assertTrue(loaded["audio"])
            self.assertFalse(loaded["subs"])
            self.assertEqual(loaded["preferred_voices"], ["LostFilm", "NewStudio"])

    def test_reset_only_current_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_user_search_defaults(100, {"quality": "4K"})
            store.save_user_search_defaults(200, {"quality": "720p"})

            store.reset_user_search_defaults(100)

            self.assertIsNone(store.load_user_search_defaults(100))
            self.assertEqual(store.load_user_search_defaults(200)["quality"], "720p")

    def test_malformed_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "user_search_defaults.json").write_text("{broken", encoding="utf-8")
            store = _make_store(tmp)

            self.assertIsNone(store.load_user_search_defaults(100))


if __name__ == "__main__":
    unittest.main()
