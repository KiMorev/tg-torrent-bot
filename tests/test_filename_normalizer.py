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
    episode_assignment_from_filename,
    prepare_episode_match_hints,
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

    def test_episode_assignment_accepts_tokens_and_rejects_ambiguity(self):
        for name, expected in [
            ("Show.S02.E01.mkv", (2, (1,))),
            ("Show.S02E01-E02.mkv", (2, (1, 2))),
            ("Show.S02.E01-E03.mkv", (2, (1, 2, 3))),
            ("Show.s2_e103.MKV", (2, (103,))),
            ("Show.S02 E01.mkv", (2, (1,))),
            ("Show.S02-E01.mkv", (2, (1,))),
            ("Show.S00E01.mkv", (0, (1,))),
            ("Show.S02.E00.mkv", None),
            ("Show.S02.E03-E01.mkv", None),
            ("Show.S02.E01.E02.mkv", None),
            ("Show.S02.E01-E02-E03.mkv", None),
            ("Show.S02.E01.S03.E02.mkv", None),
            ("Show.S02.E01-02.mkv", None),
        ]:
            with self.subTest(name=name):
                self.assertEqual(episode_assignment_from_filename(Path(name)), expected)

    def _legacy_ignore(self, folder, raw, target, *, names=None, recorded=True):
        ledger = {raw.name: target.stat().st_ino} if recorded else {}
        (folder / ".plexignore").write_text(
            "# user rules\nextras/*\n"
            "# PlexLoader torrent paths: begin\n"
            + "\n".join(names if names is not None else [raw.name]) + "\n"
            "# PlexLoader alias inodes: " + json.dumps(ledger) + "\n"
            "# PlexLoader torrent paths: end\n"
            "# after owned block\n"
        )

    def test_dotted_hints_keep_originals_subtitles_and_create_no_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Oldschool.S02.E13.2026.mkv"
            standard = root / "Oldschool.S02E01.2026.mkv"
            subtitle = root / "Oldschool.S02.E13.2026.eng.srt"
            raw.write_bytes(b"episode 13")
            standard.write_bytes(b"episode 1")
            subtitle.write_text("subtitle")
            with patch("filename_normalizer.os.link", side_effect=AssertionError("no new aliases")):
                visible, changed = prepare_episode_match_hints([raw, standard, subtitle])
                repeat, unchanged = prepare_episode_match_hints([raw, standard, subtitle])
            self.assertCountEqual(visible, [raw, standard])
            self.assertCountEqual(repeat, [raw, standard])
            self.assertEqual((changed, unchanged), (1, 0))
            self.assertEqual(raw.read_bytes(), b"episode 13")
            self.assertEqual(subtitle.read_text(), "subtitle")
            self.assertFalse(raw.with_name(raw.name.replace("S02.E", "S02E")).exists())
            self.assertFalse((root / ".plexignore").exists())
            lines = (root / ".plexmatch").read_text().splitlines()
            self.assertIn(f"ep: S02E13: {raw.name}", lines)
            self.assertFalse(any(standard.name in line or subtitle.name in line for line in lines))

    def test_standard_files_are_returned_without_sidecars_or_movie_name_restrictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / " Movie.mkv", root / "Show.S02_E01.mkv"]
            for path in paths:
                path.write_bytes(b"video")
            visible, changed = prepare_episode_match_hints(paths)
            self.assertCountEqual(visible, paths)
            self.assertEqual(changed, 0)
            self.assertFalse((root / ".plexmatch").exists())
            self.assertFalse((root / ".plexignore").exists())

    def test_hint_range_and_user_rules_are_preserved_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01-E02.mkv"
            raw.write_bytes(b"combined")
            sidecar = root / ".plexmatch"
            user_rules = "# My rules\r\ntitle: My Show\r\npattern: Specials/* {sp}.mkv\r\n"
            sidecar.write_bytes(user_rules.encode())
            self.assertEqual(prepare_episode_match_hints([raw]), ([raw], 1))
            with sidecar.open("a", encoding="utf-8", newline="") as stream:
                stream.write("# User suffix\r\n")
            second = root / "Show.S02.E03.mkv"
            second.write_bytes(b"third")
            self.assertEqual(prepare_episode_match_hints([raw, second])[1], 1)
            with sidecar.open(encoding="utf-8", newline="") as stream:
                updated = stream.read()
            self.assertTrue(updated.startswith(user_rules))
            self.assertTrue(updated.endswith("# User suffix\r\n"))
            self.assertIn(f"ep: S02E01-E02: {raw.name}", updated.splitlines())
            self.assertEqual(updated.count("# PlexLoader episode hints: begin"), 1)
            self.assertEqual(prepare_episode_match_hints([raw, second])[1], 0)

    def test_conflicting_user_hint_aborts_all_folder_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folders = [root / "a", root / "b"]
            sources = []
            for folder in folders:
                folder.mkdir()
                raw = folder / "Show.S02.E01.mkv"
                raw.write_bytes(b"episode")
                sources.append(raw)
            conflicting = folders[1] / ".plexmatch"
            user_rule = f"episode: S01E01: {sources[1].name}\n"
            conflicting.write_text(user_rule)
            with self.assertRaises(RenamePlanError):
                prepare_episode_match_hints(sources)
            self.assertFalse((folders[0] / ".plexmatch").exists())
            self.assertEqual(conflicting.read_text(), user_rule)
            self.assertTrue(all(path.read_bytes() == b"episode" for path in sources))

    def test_matching_user_hint_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            raw.write_bytes(b"episode")
            text = f"Episode: s2e1: {raw.name}\n"
            (root / ".plexmatch").write_text(text)
            self.assertEqual(prepare_episode_match_hints([raw])[1], 1)
            self.assertTrue((root / ".plexmatch").read_text().startswith(text))
            self.assertEqual(prepare_episode_match_hints([raw])[1], 0)

    def test_malformed_blocks_and_non_utf8_fail_before_writes(self):
        for filename, content in [
            (".plexmatch", b"# PlexLoader episode hints: begin\n"),
            (".plexmatch", b"# PlexLoader episode hints: end\n# PlexLoader episode hints: begin\n"),
            (".plexmatch", b"# PlexLoader episode hints: begin\ngarbage\n# PlexLoader episode hints: end\n"),
            (".plexmatch", b"\xff\xfe# user rules"),
            (".plexignore", b"# PlexLoader torrent paths: begin\n"),
            (".plexignore", b"\xff\xfe# user rules"),
        ]:
            with self.subTest(filename=filename, content=content), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                raw = root / "Show.S02.E01.mkv"
                raw.write_bytes(b"episode")
                sidecar = root / filename
                sidecar.write_bytes(content)
                with self.assertRaises(RenamePlanError):
                    prepare_episode_match_hints([raw])
                self.assertEqual(sidecar.read_bytes(), content)
                self.assertEqual(raw.read_bytes(), b"episode")
                self.assertFalse(raw.with_name("Show.S02E01.mkv").exists())
                if filename == ".plexignore":
                    self.assertFalse((root / ".plexmatch").exists())

    def test_ambiguous_dotted_batch_creates_no_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "Show.S02.E01.mkv", root / "Show.S02.E02.E03.mkv"]
            for path in paths:
                path.write_bytes(b"episode")
            with self.assertRaises(RenamePlanError):
                prepare_episode_match_hints(paths)
            self.assertFalse((root / ".plexmatch").exists())
            self.assertTrue(all(path.exists() for path in paths))

    def test_newline_filename_is_rejected_before_access(self):
        with self.assertRaises(RenamePlanError):
            prepare_episode_match_hints([Path("Show.S02.E01\nOther.mkv")])

    def test_atomic_match_write_failure_keeps_user_text_and_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            raw.write_bytes(b"episode")
            sidecar = root / ".plexmatch"
            text = "# user rules\ntitle: Show\n"
            sidecar.write_text(text)
            with patch("filename_normalizer.os.replace", side_effect=PermissionError("read only")):
                with self.assertRaises(PermissionError):
                    prepare_episode_match_hints([raw])
            self.assertEqual(raw.read_bytes(), b"episode")
            self.assertEqual(sidecar.read_text(), text)
            self.assertEqual(set(path.name for path in root.iterdir()), {raw.name, ".plexmatch"})

    def test_legacy_samefile_alias_is_hidden_but_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            target = root / "Show.S02E01.mkv"
            raw.write_bytes(b"episode")
            os.link(raw, target)
            self._legacy_ignore(root, raw, target)
            with patch("filename_normalizer.os.link", side_effect=AssertionError("no new aliases")):
                visible, changed = prepare_episode_match_hints([raw, target])
            self.assertEqual((visible, changed), ([raw], 1))
            self.assertTrue(raw.samefile(target))
            lines = (root / ".plexignore").read_text().splitlines()
            self.assertNotIn(raw.name, lines)
            self.assertIn(target.name, lines)
            self.assertIn("extras/*", lines)
            self.assertIn("# after owned block", lines)
            self.assertEqual(prepare_episode_match_hints([raw, target]), ([raw], 0))

    def test_replaced_raw_uses_hints_without_modifying_proven_old_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            target = root / "Show.S02E01.mkv"
            raw.write_bytes(b"old")
            os.link(raw, target)
            self._legacy_ignore(root, raw, target)
            raw.unlink()
            raw.write_bytes(b"new")
            target_inode = target.stat().st_ino
            visible, changed = prepare_episode_match_hints([raw, target])
            self.assertEqual((visible, changed), ([raw], 1))
            self.assertEqual(raw.read_bytes(), b"new")
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(target.stat().st_ino, target_inode)
            self.assertFalse(raw.samefile(target))

    def test_unproven_foreign_target_is_visible_and_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            target = root / "Show.S02E01.mkv"
            raw.write_bytes(b"source")
            target.write_bytes(b"user file")
            self._legacy_ignore(root, raw, target, recorded=False)
            visible, changed = prepare_episode_match_hints([raw, target])
            self.assertCountEqual(visible, [raw, target])
            self.assertEqual(changed, 1)
            self.assertEqual(target.read_bytes(), b"user file")
            self.assertNotIn(target.name, (root / ".plexignore").read_text().splitlines())
            self.assertNotIn(raw.name, (root / ".plexignore").read_text().splitlines())

    def test_replaced_hidden_alias_is_unhidden_without_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            target = root / "Show.S02E01.mkv"
            raw.write_bytes(b"source")
            os.link(raw, target)
            self._legacy_ignore(root, raw, target)
            prepare_episode_match_hints([raw, target])
            replacement = root / "user.tmp"
            replacement.write_bytes(b"user replacement")
            os.replace(replacement, target)
            visible, changed = prepare_episode_match_hints([raw, target])
            self.assertCountEqual(visible, [raw, target])
            self.assertEqual(changed, 0)
            self.assertEqual(target.read_bytes(), b"user replacement")
            self.assertNotIn(target.name, (root / ".plexignore").read_text().splitlines())

    def test_ignore_transition_retry_keeps_published_hints_and_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            target = root / "Show.S02E01.mkv"
            raw.write_bytes(b"episode")
            os.link(raw, target)
            self._legacy_ignore(root, raw, target)
            with patch("filename_normalizer._write_dotted_ignore", side_effect=PermissionError("ignore failed")):
                with self.assertRaises(PermissionError):
                    prepare_episode_match_hints([raw, target])
            self.assertIn(f"ep: S02E01: {raw.name}", (root / ".plexmatch").read_text().splitlines())
            self.assertIn(raw.name, (root / ".plexignore").read_text().splitlines())
            self.assertTrue(raw.samefile(target))
            self.assertEqual(prepare_episode_match_hints([raw, target]), ([raw], 0))
            self.assertNotIn(raw.name, (root / ".plexignore").read_text().splitlines())

    def test_concurrent_hint_writes_keep_both_episode_entries(self):
        import filename_normalizer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / f"Show.S02.E{episode:02d}.mkv" for episode in [1, 2]]
            for source in sources:
                source.write_bytes(b"episode")
            writer = filename_normalizer._write_sidecar
            def delayed_write(*args):
                time.sleep(0.05)
                writer(*args)
            with patch("filename_normalizer._write_sidecar", side_effect=delayed_write):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(lambda source: prepare_episode_match_hints([source]), sources))
            lines = (root / ".plexmatch").read_text().splitlines()
            for episode, source in enumerate(sources, 1):
                self.assertIn(f"ep: S02E{episode:02d}: {source.name}", lines)
                self.assertTrue(source.exists())

    @unittest.skipIf(os.name == "nt", "POSIX permissions are enforced on the NAS and Linux CI")
    def test_plexmatch_is_readable_and_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "Show.S02.E01.mkv"
            raw.write_bytes(b"first")
            prepare_episode_match_hints([raw])
            sidecar = root / ".plexmatch"
            self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o644)
            sidecar.chmod(0o640)
            second = root / "Show.S02.E02.mkv"
            second.write_bytes(b"second")
            prepare_episode_match_hints([second])
            self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o640)

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
        self.assertEqual(prepare_episode_match_hints([raw, target]), ([raw], 1))
        self.assertEqual(raw.read_bytes(), b"new")
        self.assertEqual(target.read_bytes(), b"legacy")

    def test_next_restore_keeps_raw_visible_after_hint_transition(self):
        target = self.storage / "Show.S02E01.mkv"
        target.write_bytes(b"legacy")
        raw = self.storage / "Show.S02.E01.mkv"
        self.write_torrent(raw.name)
        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 1)
        self.assertEqual(prepare_episode_match_hints([raw, target]), ([raw], 1))

        self.assertEqual(restore_dotted_episode_torrent_paths(self.torrent, self.storage), 0)

        lines = (self.storage / ".plexignore").read_text().splitlines()
        self.assertNotIn(raw.name, lines)
        self.assertIn(target.name, lines)
        self.assertTrue(raw.samefile(target))
        self.assertEqual(prepare_episode_match_hints([raw, target]), ([raw], 0))

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
