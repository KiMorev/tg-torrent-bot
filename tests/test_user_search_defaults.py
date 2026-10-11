import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def test_migration_moves_all_qualities_and_preserves_other_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            for chat_id, quality in enumerate(("1080p", "720p", "any", "4K"), start=100):
                store.save_user_search_defaults(chat_id, {
                    "quality": quality, "audio": True, "subs": True,
                    "preferred_voices": ["LostFilm"],
                })
            before = json.loads(store.user_search_defaults_file.read_text(encoding="utf-8"))
            self.assertEqual(store.migrate_user_search_quality_to_2160(), 3)
            for chat_id, original in before.items():
                migrated = store.load_json_file(store.user_search_defaults_file, {})[chat_id]
                self.assertEqual(migrated, {**original, "quality": "4K"})
            store.save_user_search_defaults(100, {"quality": "1080p"})
            self.assertEqual(_make_store(tmp).migrate_user_search_quality_to_2160(), 0)
            self.assertEqual(store.load_user_search_defaults(100)["quality"], "1080p")

    def test_migration_of_empty_store_does_not_reset_future_user_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            self.assertEqual(store.migrate_user_search_quality_to_2160(), 0)
            store.save_user_search_defaults(100, {"quality": "720p"})
            _make_store(tmp).migrate_user_search_quality_to_2160()
            self.assertEqual(store.load_user_search_defaults(100)["quality"], "720p")

    def test_migration_does_not_overwrite_malformed_file_or_record_failed_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.user_search_defaults_file.write_text("{broken", encoding="utf-8")
            self.assertEqual(store.migrate_user_search_quality_to_2160(), 0)
            self.assertEqual(store.user_search_defaults_file.read_text(encoding="utf-8"), "{broken")
            store.user_search_defaults_file.write_text('{"100": {"quality": "1080p"}}', encoding="utf-8")
            with patch.object(store, "save_json_file"):
                self.assertEqual(store.migrate_user_search_quality_to_2160(), 0)
            self.assertEqual(store.load_user_search_defaults(100)["quality"], "1080p")
            self.assertEqual(store.migrate_user_search_quality_to_2160(), 1)

    def test_missing_or_invalid_quality_uses_4k(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_user_search_defaults(100, {"quality": "invalid"})
            self.assertEqual(store.load_user_search_defaults(100)["quality"], "4K")
            store.user_search_defaults_file.write_text('{"100": {"audio": true}}', encoding="utf-8")
            self.assertEqual(store.load_user_search_defaults(100)["quality"], "4K")

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
