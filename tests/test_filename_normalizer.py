import json
import os
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from filename_normalizer import (
    NAMING_MIXED,
    NAMING_PLEX_READY,
    NAMING_RENAMABLE_ARC,
    NAMING_UNSAFE_ARC,
    NAMING_UNKNOWN_NON_PLEX,
    RenamePlanError,
    apply_rename_plan,
    build_arc_episode_rename_plan,
    has_arc_episode_filenames,
    infer_series_season_from_filenames,
    inspect_series_filenames,
    normalize_dotted_episode_files,
    normalize_dotted_episode_name,
    restore_dotted_episode_torrent_paths,
)


class FilenameNormalizerTests(unittest.TestCase):
    def test_dotted_episode_name_changes_only_marker_separator(self):
        for source, target in [
            ("Oldschool.S02.E01.2026.WEB-DL.1080p.ExKinoRay.mkv", "Oldschool.S02E01.2026.WEB-DL.1080p.ExKinoRay.mkv"),
            ("Шоу.s2.e103.1080p.MKV", "Шоу.s2e103.1080p.MKV"),
            ("Show.S02E01.mkv", "Show.S02E01.mkv"),
            ("Show.S02.2026.mkv", "Show.S02.2026.mkv"),
        ]:
            with self.subTest(source=source):
                self.assertEqual(normalize_dotted_episode_name(source), target)

    def test_dotted_episode_files_keep_torrent_paths_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Oldschool.S02.E13.2026.mkv"
            source.write_bytes(b"episode 13")
            ready = root / "Oldschool.S02E01.2026.mkv"
            ready.write_bytes(b"episode 1")
            subtitle = root / "Oldschool.S02.E13.2026.srt"
            subtitle.write_text("subtitle")
            target = root / "Oldschool.S02E13.2026.mkv"

            aliases = normalize_dotted_episode_files([source, ready, subtitle])

            self.assertEqual(aliases, {source: target})
            self.assertTrue(source.samefile(target))
            self.assertEqual(target.read_bytes(), b"episode 13")
            self.assertTrue(subtitle.exists())
            self.assertEqual(normalize_dotted_episode_files([source, target, ready, subtitle]), {})
            ignore = (root / ".plexignore").read_text()
            self.assertIn(source.name, ignore.splitlines())
            self.assertNotIn(target.name, ignore.splitlines())
            self.assertNotIn(subtitle.name, ignore.splitlines())

    def test_dotted_episode_collision_checks_entire_batch_before_linking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "Show.S02.E01.mkv"
            second = root / "Show.S02.E02.mkv"
            existing = root / "Show.S02E02.mkv"
            for path, content in [(first, b"first"), (second, b"second"), (existing, b"existing")]:
                path.write_bytes(content)

            with self.assertRaises(RenamePlanError):
                normalize_dotted_episode_files([first, second])

            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertFalse((root / "Show.S02E01.mkv").exists())
            self.assertFalse((root / ".plexignore").exists())

    def test_dotted_episode_files_report_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            source.write_bytes(b"episode")
            with patch("filename_normalizer.os.link", side_effect=PermissionError("read only")):
                with self.assertRaises(PermissionError):
                    normalize_dotted_episode_files([source])
            self.assertEqual(source.read_bytes(), b"episode")
            self.assertFalse(source.with_name("Show.S02E01.mkv").exists())

    def test_existing_samefile_alias_is_accepted_and_added_to_ignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            target = Path(tmp) / "Show.S02E01.mkv"
            source.write_bytes(b"episode")
            os.link(source, target)

            self.assertEqual(normalize_dotted_episode_files([source, target]), {})
            self.assertIn(source.name, (Path(tmp) / ".plexignore").read_text().splitlines())

    def test_unmanaged_equal_content_is_still_a_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            target = Path(tmp) / "Show.S02E01.mkv"
            source.write_bytes(b"episode")
            target.write_bytes(b"episode")

            with self.assertRaises(RenamePlanError):
                normalize_dotted_episode_files([source])
            self.assertFalse(source.samefile(target))

    def test_changed_content_is_visible_through_existing_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            target = Path(tmp) / "Show.S02E01.mkv"
            source.write_bytes(b"old")
            normalize_dotted_episode_files([source])

            source.write_bytes(b"new content")

            self.assertEqual(normalize_dotted_episode_files([source, target]), {})
            self.assertEqual(target.read_bytes(), b"new content")

    def test_replaced_torrent_file_retargets_managed_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            target = Path(tmp) / "Show.S02E01.mkv"
            source.write_bytes(b"old")
            normalize_dotted_episode_files([source])
            replacement = Path(tmp) / "replacement"
            replacement.write_bytes(b"new")
            os.replace(replacement, source)
            self.assertEqual(target.read_bytes(), b"old")

            self.assertEqual(normalize_dotted_episode_files([source, target]), {source: target})

            self.assertTrue(source.samefile(target))
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(normalize_dotted_episode_files([source]), {})

    def test_failed_retarget_keeps_original_and_previous_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            target = Path(tmp) / "Show.S02E01.mkv"
            source.write_bytes(b"old")
            normalize_dotted_episode_files([source])
            source.unlink()
            source.write_bytes(b"new")

            with patch("filename_normalizer.os.link", side_effect=OSError("hardlinks unsupported")):
                with self.assertRaises(OSError):
                    normalize_dotted_episode_files([source])

            self.assertEqual(source.read_bytes(), b"new")
            self.assertEqual(target.read_bytes(), b"old")

    def test_replaced_managed_target_is_preserved_as_user_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Show.S02.E01.mkv"
            target = root / "Show.S02E01.mkv"
            source.write_bytes(b"torrent")
            normalize_dotted_episode_files([source])
            replacement = root / "manual"
            replacement.write_bytes(b"user replacement")
            os.replace(replacement, target)
            ignore_before = (root / ".plexignore").read_bytes()
            another = root / "Show.S02.E02.mkv"
            another.write_bytes(b"another torrent")

            with self.assertRaises(RenamePlanError):
                normalize_dotted_episode_files([another, source])

            self.assertEqual(target.read_bytes(), b"user replacement")
            self.assertEqual(source.read_bytes(), b"torrent")
            self.assertFalse(another.with_name("Show.S02E02.mkv").exists())
            self.assertEqual((root / ".plexignore").read_bytes(), ignore_before)

    def test_missing_or_zero_inode_proof_prevents_retarget(self):
        for ledger in [{}, {"Show.S02.E01.mkv": 0}, {"Show.S02.E01.mkv": None}]:
            with self.subTest(ledger=ledger), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "Show.S02.E01.mkv"
                target = root / "Show.S02E01.mkv"
                source.write_bytes(b"new")
                target.write_bytes(b"old")
                (root / ".plexignore").write_text(
                    "# PlexLoader torrent paths: begin\n" + source.name + "\n"
                    "# PlexLoader alias inodes: " + json.dumps(ledger) + "\n"
                    "# PlexLoader torrent paths: end\n"
                )
                with self.assertRaises(RenamePlanError):
                    normalize_dotted_episode_files([source])
                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(source.read_bytes(), b"new")

    def test_concurrent_normalization_keeps_both_ignore_entries(self):
        import filename_normalizer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / f"Show.S02.E{episode:02d}.mkv" for episode in [1, 2]]
            for source in sources:
                source.write_bytes(b"episode")
            write_ignore = filename_normalizer._write_dotted_ignore

            def delayed_write(*args):
                time.sleep(0.05)
                write_ignore(*args)

            with patch("filename_normalizer._write_dotted_ignore", side_effect=delayed_write):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda source: normalize_dotted_episode_files([source]), sources))

            self.assertEqual(len(results), 2)
            lines = (root / ".plexignore").read_text().splitlines()
            for source in sources:
                self.assertIn(source.name, lines)
                self.assertTrue(source.samefile(source.with_name(normalize_dotted_episode_name(source.name))))
            ledger_line = next(line for line in lines if line.startswith("# PlexLoader alias inodes: "))
            self.assertEqual(set(json.loads(ledger_line.split(": ", 1)[1])), {source.name for source in sources})

    @unittest.skipIf(os.name == "nt", "POSIX permissions are enforced on the NAS and Linux CI")
    def test_plexignore_is_readable_and_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Show.S02.E01.mkv"
            source.write_bytes(b"first")
            normalize_dotted_episode_files([source])
            ignore = root / ".plexignore"
            self.assertEqual(stat.S_IMODE(ignore.stat().st_mode), 0o644)
            ignore.chmod(0o640)
            second = root / "Show.S02.E02.mkv"
            second.write_bytes(b"second")

            normalize_dotted_episode_files([second])

            self.assertEqual(stat.S_IMODE(ignore.stat().st_mode), 0o640)

    def test_plexignore_preserves_user_rules_and_updates_managed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ignore = root / ".plexignore"
            user_rules = "# Keep my rules\r\ntrailers/*\r\n*.sample.mkv\r\n"
            ignore.write_bytes(user_rules.encode())
            source = root / "Show.S02.E01.mkv"
            source.write_bytes(b"first")
            normalize_dotted_episode_files([source])
            with ignore.open(encoding="utf-8", newline="") as stream:
                initial = stream.read()
            self.assertTrue(initial.startswith(user_rules))
            with ignore.open("a", encoding="utf-8", newline="") as stream:
                stream.write("# After managed block\r\nextras/*\r\n")
            second = root / "Show.S02.E02.mkv"
            second.write_bytes(b"second")

            normalize_dotted_episode_files([source, second])

            with ignore.open(encoding="utf-8", newline="") as stream:
                updated = stream.read()
            self.assertTrue(updated.startswith(user_rules))
            self.assertTrue(updated.endswith("# After managed block\r\nextras/*\r\n"))
            self.assertEqual(updated.count("# PlexLoader torrent paths: begin"), 1)
            self.assertEqual(updated.splitlines().count(source.name), 1)
            self.assertEqual(updated.splitlines().count(second.name), 1)

    def test_ignore_write_failure_preserves_original_data_and_user_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Show.S02.E01.mkv"
            source.write_bytes(b"episode")
            ignore = root / ".plexignore"
            ignore.write_text("# user rule\nextras/*\n")
            with patch("filename_normalizer.os.replace", side_effect=PermissionError("read only")):
                with self.assertRaises(PermissionError):
                    normalize_dotted_episode_files([source])
            self.assertEqual(source.read_bytes(), b"episode")
            self.assertEqual(ignore.read_text(), "# user rule\nextras/*\n")

    def test_malformed_managed_block_prevents_file_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show.S02.E01.mkv"
            source.write_bytes(b"episode")
            (Path(tmp) / ".plexignore").write_text("# PlexLoader torrent paths: begin\n")
            with self.assertRaises(RenamePlanError):
                normalize_dotted_episode_files([source])
            self.assertFalse(source.with_name("Show.S02E01.mkv").exists())

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

    def test_arc_episode_plan_supports_combined_episode_parts(self):
        files = [
            Path("1. Истинные ценности (1,2 сер.) - hdtv1080p.mkv"),
            Path("2. Операция Доктор (1,2 сер.) - hdtv1080p.mkv"),
            Path("3. Работа над ошибками (1,2 сер.) - hdtv1080p.mkv"),
        ]

        inspection = inspect_series_filenames(files)
        plan = build_arc_episode_rename_plan(
            show_title="Тайны следствия",
            season=4,
            files=files,
            source_root=Path("."),
        )

        self.assertEqual(inspection.status, NAMING_RENAMABLE_ARC)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            [item.target_path.name for item in plan.items],
            [
                "Тайны следствия - S04E01-E02 - Истинные ценности.mkv",
                "Тайны следствия - S04E03-E04 - Операция Доктор.mkv",
                "Тайны следствия - S04E05-E06 - Работа над ошибками.mkv",
            ],
        )
        self.assertEqual(plan.items[0].episode_numbers, (1, 2))

    def test_series_context_plan_supports_film_part_files(self):
        files = [
            Path("Тайны следствия-6.Фильм 1.Личный состав_часть 1.avi"),
            Path("Тайны следствия-6.Фильм 1.Личный состав_часть 2.avi"),
            Path("Тайны следствия-6.Фильм 2.Веселый слоник_часть 1.avi"),
            Path("Тайны следствия-6.Фильм 2.Веселый слоник_часть 2.avi"),
        ]

        inspection = inspect_series_filenames(
            files,
            series_context=True,
            show_title="Тайны следствия",
        )
        plan = build_arc_episode_rename_plan(
            show_title="Тайны следствия",
            season=6,
            files=files,
            source_root=Path("."),
            series_context=True,
        )

        self.assertFalse(has_arc_episode_filenames(files))
        self.assertTrue(has_arc_episode_filenames(files, series_context=True, show_title="Тайны следствия"))
        self.assertEqual(infer_series_season_from_filenames(files, "Тайны следствия"), 6)
        self.assertEqual(inspection.status, NAMING_RENAMABLE_ARC)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            [item.target_path.name for item in plan.items],
            [
                "Тайны следствия - S06E01 - Личный состав.avi",
                "Тайны следствия - S06E02 - Личный состав.avi",
                "Тайны следствия - S06E03 - Веселый слоник.avi",
                "Тайны следствия - S06E04 - Веселый слоник.avi",
            ],
        )

    def test_series_context_plan_supports_numbered_episode_files(self):
        files = [Path("01.avi"), Path("02.avi"), Path("03.avi")]

        inspection = inspect_series_filenames(
            files,
            series_context=True,
            show_title="Тайны следствия",
        )
        plan = build_arc_episode_rename_plan(
            show_title="Тайны следствия",
            season=8,
            files=files,
            source_root=Path("."),
            series_context=True,
        )

        self.assertFalse(has_arc_episode_filenames(files))
        self.assertTrue(has_arc_episode_filenames(files, series_context=True, show_title="Тайны следствия"))
        self.assertEqual(inspection.status, NAMING_RENAMABLE_ARC)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            [item.target_path.name for item in plan.items],
            [
                "Тайны следствия - S08E01.avi",
                "Тайны следствия - S08E02.avi",
                "Тайны следствия - S08E03.avi",
            ],
        )

    def test_numbered_episode_files_reject_gaps(self):
        files = [Path("01.avi"), Path("03.avi")]

        inspection = inspect_series_filenames(
            files,
            series_context=True,
            show_title="Тайны следствия",
        )
        plan = build_arc_episode_rename_plan(
            show_title="Тайны следствия",
            season=8,
            files=files,
            source_root=Path("."),
            series_context=True,
        )

        self.assertEqual(inspection.status, NAMING_UNSAFE_ARC)
        self.assertIsNone(plan)

    def test_inspection_marks_missing_arc_parts_unsafe(self):
        files = [
            Path("1. Дело (1 сер.) - hdtv1080p.mkv"),
            Path("1. Дело (3 сер.) - hdtv1080p.mkv"),
        ]

        inspection = inspect_series_filenames(files)

        self.assertEqual(inspection.status, NAMING_UNSAFE_ARC)

    def test_inspection_marks_mixed_plex_and_arc_files(self):
        files = [
            Path("Show - S01E01 - Pilot.mkv"),
            Path("1. Case (2 сер.) - hdtv1080p.mkv"),
        ]

        inspection = inspect_series_filenames(files)

        self.assertEqual(inspection.status, NAMING_MIXED)

    def test_inspection_marks_single_arc_file_unsafe(self):
        files = [Path("1. Case (1 сер.) - hdtv1080p.mkv")]

        inspection = inspect_series_filenames(files)

        self.assertEqual(inspection.status, NAMING_UNSAFE_ARC)

    def test_inspection_marks_unknown_episode_like_names(self):
        for name in [
            "Episode 01 - Pilot.mkv",
            "01 серия - Pilot.mkv",
            "01.mkv",
        ]:
            with self.subTest(name=name):
                inspection = inspect_series_filenames([Path(name)])

                self.assertEqual(inspection.status, NAMING_UNKNOWN_NON_PLEX)

    def test_arc_detector_ignores_already_plex_compatible_files(self):
        files = [
            Path("Show - S01E01 - Pilot.mkv"),
            Path("Show - S01E02 - Next.mkv"),
        ]

        self.assertFalse(has_arc_episode_filenames(files))

    def test_inspection_accepts_dotted_season_episode_marker(self):
        files = [
            Path("Бригада.2002.S01.E01.1080i.BDRemux.mkv"),
            Path("Бригада.2002.S01.E02.1080i.BDRemux.mkv"),
        ]

        inspection = inspect_series_filenames(files)

        self.assertEqual(inspection.status, NAMING_PLEX_READY)
        self.assertFalse(has_arc_episode_filenames(files))


class TorrentPathRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.torrent = self.root / "update.torrent"

    def write_torrent(self, name, paths=None):
        def encode(value):
            if isinstance(value, bytes):
                return str(len(value)).encode() + b":" + value
            if isinstance(value, int):
                return b"i" + str(value).encode() + b"e"
            if isinstance(value, list):
                return b"l" + b"".join(encode(item) for item in value) + b"e"
            return b"d" + b"".join(encode(key) + encode(item) for key, item in sorted(value.items())) + b"e"

        info = {b"name": name.encode()}
        if paths is None:
            info[b"length"] = 3
        else:
            info[b"files"] = [
                {b"length": 3, b"path": [part.encode() for part in path]}
                for path in paths
            ]
        self.torrent.write_bytes(encode({b"info": info}))

    def test_restores_exact_torrent_paths_without_moving_plex_files(self):
        folder = self.storage / "Show" / "Season 02"
        folder.mkdir(parents=True)
        target = folder / "Show.S02E01.mkv"
        target.write_bytes(b"legacy content")
        raw = folder / "Show.S02.E01.mkv"
        self.write_torrent("Show", [["Season 02", raw.name], ["Season 02", "Show.S02.E02.mkv"]])

        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 1)

        self.assertTrue(raw.samefile(target))
        self.assertEqual(target.read_bytes(), b"legacy content")
        self.assertIn(raw.name, (folder / ".plexignore").read_text().splitlines())
        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 0)
        self.assertFalse((folder / "Show.S02.E02.mkv").exists())

    def test_existing_original_path_is_untouched_even_if_content_changed(self):
        folder = self.storage / "Show"
        folder.mkdir()
        raw = folder / "Show.S02.E01.mkv"
        target = folder / "Show.S02E01.mkv"
        raw.write_bytes(b"new")
        target.write_bytes(b"old")
        self.write_torrent("Show", [[raw.name]])

        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 0)

        self.assertEqual(raw.read_bytes(), b"new")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse((folder / ".plexignore").exists())

    def test_does_not_search_numbered_sibling_or_restore_subtitles(self):
        other = self.storage / "Show (2)"
        other.mkdir()
        target = other / "Show.S02E01.mkv"
        target.write_bytes(b"other folder")
        folder = self.storage / "Show"
        folder.mkdir()
        (folder / "Show.S02E01.srt").write_text("subtitle")
        self.write_torrent("Show", [["Show.S02.E01.mkv"], ["Show.S02.E01.srt"]])

        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 0)
        self.assertFalse((folder / "Show.S02.E01.mkv").exists())
        self.assertFalse((folder / "Show.S02.E01.srt").exists())

    def test_restores_single_file_torrent(self):
        target = self.storage / "Show.S02E01.mkv"
        target.write_bytes(b"legacy")
        self.write_torrent("Show.S02.E01.mkv")

        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 1)
        self.assertTrue((self.storage / "Show.S02.E01.mkv").samefile(target))

    def test_rejects_path_traversal_before_restoring_any_file(self):
        folder = self.storage / "Show"
        folder.mkdir()
        (folder / "Show.S02E01.mkv").write_bytes(b"legacy")
        for name, paths in [
            ("../Show", [["Show.S02.E01.mkv"]]),
            ("Show", [["Show.S02.E01.mkv"], ["..", "outside.S02.E01.mkv"]]),
            ("Show", [["/outside.S02.E01.mkv"]]),
            ("Show", [["C:\\outside.S02.E01.mkv"]]),
        ]:
            with self.subTest(name=name, paths=paths):
                self.write_torrent(name, paths)
                with self.assertRaises(RenamePlanError):
                    restore_dotted_episode_torrent_paths(self.torrent, self.storage)
                self.assertFalse((folder / "Show.S02.E01.mkv").exists())

    def test_restore_link_failure_does_not_move_existing_plex_file(self):
        target = self.storage / "Show.S02E01.mkv"
        target.write_bytes(b"legacy")
        self.write_torrent("Show.S02.E01.mkv")
        with patch("filename_normalizer.os.link", side_effect=OSError("unsupported")):
            with self.assertRaises(OSError):
                restore_dotted_episode_torrent_paths(self.torrent, self.storage)
        self.assertEqual(target.read_bytes(), b"legacy")
        self.assertFalse((self.storage / "Show.S02.E01.mkv").exists())

    def test_retry_repairs_ignore_after_restored_link_write_failure(self):
        target = self.storage / "Show.S02E01.mkv"
        target.write_bytes(b"legacy")
        raw = self.storage / "Show.S02.E01.mkv"
        ignore = self.storage / ".plexignore"
        ignore.write_text("# user rules\nextras/*\n")
        self.write_torrent(raw.name)
        with patch("filename_normalizer.os.replace", side_effect=PermissionError("ignore write failed")):
            with self.assertRaises(PermissionError):
                restore_dotted_episode_torrent_paths(self.torrent, self.storage)
        self.assertTrue(raw.samefile(target))
        self.assertEqual(ignore.read_text(), "# user rules\nextras/*\n")

        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 0)

        lines = ignore.read_text().splitlines()
        self.assertIn(raw.name, lines)
        ledger_line = next(line for line in lines if line.startswith("# PlexLoader alias inodes: "))
        self.assertEqual(json.loads(ledger_line.split(": ", 1)[1]), {raw.name: target.stat().st_ino})
        replacement = self.storage / "replacement"
        replacement.write_bytes(b"new")
        os.replace(replacement, raw)
        self.assertEqual(normalize_dotted_episode_files([raw]), {raw: target})
        self.assertEqual(target.read_bytes(), b"new")

    def test_invalid_torrent_is_reported_without_changes(self):
        self.torrent.write_bytes(b"not a torrent")
        with self.assertRaises(RenamePlanError):
            restore_dotted_episode_torrent_paths(self.torrent, self.storage)
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_missing_storage_mount_leaves_subscription_download_unchanged(self):
        self.torrent.write_bytes(b"not inspected without mounted storage")
        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.root / "missing"), 0)


if __name__ == "__main__":
    unittest.main()
