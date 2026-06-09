import tempfile
import unittest
from pathlib import Path

from filename_normalizer import (
    apply_rename_plan,
    build_arc_episode_rename_plan,
    has_arc_episode_filenames,
)


class FilenameNormalizerTests(unittest.TestCase):
    def test_arc_episode_plan_uses_sequential_plex_episode_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Тайны следствия"
            root.mkdir()
            names = [
                "1. Мягкая лапа смерти (1 сер.) - hdtv1080p.mkv",
                "1. Мягкая лапа смерти (2 сер.) - hdtv1080p.mkv",
                "1. Мягкая лапа смерти (3 сер.) - hdtv1080p.mkv",
                "1. Мягкая лапа смерти (4 сер.) - hdtv1080p.mkv",
                "2. Гроб на две персоны (1 сер.) - hdtv1080p.mkv",
                "2. Гроб на две персоны (2 сер.) - hdtv1080p.mkv",
                "2. Гроб на две персоны (3 сер.) - hdtv1080p.mkv",
                "2. Гроб на две персоны (4 сер.) - hdtv1080p.mkv",
                "3. Странности Алисы (1 сер.) - hdtv1080p.mkv",
                "3. Странности Алисы (2 сер.) - hdtv1080p.mkv",
                "4. Женские слёзы (1 сер.) - hdtv1080p.mkv",
                "4. Женские слёзы (2 сер.) - hdtv1080p.mkv",
                "5. Чужой крест (1 сер.) - hdtv1080p.mkv",
                "5. Чужой крест (2 сер.) - hdtv1080p.mkv",
            ]
            files = []
            for name in names:
                path = root / name
                path.write_bytes(b"")
                files.append(path)

            plan = build_arc_episode_rename_plan(
                show_title="Тайны следствия",
                season=1,
                files=files,
                source_root=root,
            )

            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(len(plan.items), 14)
            self.assertEqual(
                plan.items[0].target_path.name,
                "Тайны следствия - S01E01 - Мягкая лапа смерти.mkv",
            )
            self.assertEqual(
                plan.items[4].target_path.name,
                "Тайны следствия - S01E05 - Гроб на две персоны.mkv",
            )
            self.assertEqual(
                plan.items[-1].target_path.name,
                "Тайны следствия - S01E14 - Чужой крест.mkv",
            )

            apply_rename_plan(plan)

            self.assertFalse(files[0].exists())
            self.assertTrue((root / "Season 01" / "Тайны следствия - S01E01 - Мягкая лапа смерти.mkv").exists())

    def test_arc_episode_plan_rejects_missing_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = []
            for name in [
                "1. Дело (1 сер.) - hdtv1080p.mkv",
                "1. Дело (3 сер.) - hdtv1080p.mkv",
            ]:
                path = root / name
                path.write_bytes(b"")
                files.append(path)

            plan = build_arc_episode_rename_plan(
                show_title="Show",
                season=1,
                files=files,
                source_root=root,
            )

            self.assertIsNone(plan)
            self.assertTrue(has_arc_episode_filenames(files))

    def test_arc_detector_ignores_already_plex_compatible_files(self):
        files = [
            Path("Show - S01E01 - Pilot.mkv"),
            Path("Show - S01E02 - Next.mkv"),
        ]

        self.assertFalse(has_arc_episode_filenames(files))


if __name__ == "__main__":
    unittest.main()
