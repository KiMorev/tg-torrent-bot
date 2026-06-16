import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from youtube_downloads import (
    _apply_audio_language,
    _apply_mp4_metadata,
    _cleanup_failed_download,
    _download_with_retries,
    YouTubeDownloadError,
    YouTubePathPlan,
    YouTubeUnsupportedError,
    build_path_plan,
    compatible_quality_options,
    display_quality_label,
    extract_metadata,
    extract_youtube_video_id,
    find_youtube_url,
    select_format,
)


class YouTubeDownloadHelperTests(unittest.TestCase):
    def test_extracts_supported_youtube_urls(self) -> None:
        self.assertEqual(
            extract_youtube_video_id("https://www.youtube.com/watch?v=abcdefghijk"),
            "abcdefghijk",
        )
        self.assertEqual(
            extract_youtube_video_id("https://youtu.be/abcdefghijk?t=10"),
            "abcdefghijk",
        )
        self.assertEqual(
            extract_youtube_video_id("https://www.youtube.com/shorts/abcdefghijk"),
            "abcdefghijk",
        )

    def test_find_youtube_url_normalizes_to_watch_url(self) -> None:
        self.assertEqual(
            find_youtube_url("смотри https://youtu.be/abcdefghijk."),
            "https://www.youtube.com/watch?v=abcdefghijk",
        )

    def test_playlist_only_url_is_rejected_before_ytdlp_import(self) -> None:
        with self.assertRaises(YouTubeUnsupportedError):
            extract_metadata("https://www.youtube.com/playlist?list=PL123")

    def test_select_format_prefers_progressive_when_available(self) -> None:
        info = {
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "height": 360,
                    "vcodec": "avc1.42001E",
                    "acodec": "mp4a.40.2",
                    "tbr": 800,
                },
                {
                    "format_id": "22",
                    "ext": "mp4",
                    "height": 720,
                    "vcodec": "avc1.64001F",
                    "acodec": "mp4a.40.2",
                    "tbr": 2500,
                },
            ]
        }

        choice = select_format(info, 1080)

        self.assertEqual(choice.height, 720)
        self.assertEqual(choice.format_id, "22")

    def test_select_format_combines_h264_video_and_aac_audio(self) -> None:
        info = {
            "formats": [
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "height": 1080,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "tbr": 4000,
                    "filesize": 100,
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "abr": 128,
                    "filesize": 10,
                },
                {
                    "format_id": "248",
                    "ext": "webm",
                    "height": 1080,
                    "vcodec": "vp9",
                    "acodec": "none",
                },
            ]
        }

        choice = select_format(info, 1080)

        self.assertEqual(choice.height, 1080)
        self.assertEqual(choice.format_id, "137+140")
        self.assertEqual(choice.filesize, 110)

    def test_quality_options_only_include_exact_compatible_heights(self) -> None:
        info = {
            "formats": [
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "height": 1080,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                },
                {
                    "format_id": "399",
                    "ext": "mp4",
                    "height": 1080,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                },
                {
                    "format_id": "22",
                    "ext": "mp4",
                    "height": 720,
                    "vcodec": "avc1.64001F",
                    "acodec": "mp4a.40.2",
                },
                {
                    "format_id": "134",
                    "ext": "mp4",
                    "height": 360,
                    "vcodec": "avc1.4d401e",
                    "acodec": "none",
                },
            ]
        }

        choices = compatible_quality_options(info, 1080)

        self.assertEqual([choice.height for choice in choices], [1080, 720])
        self.assertEqual([choice.label for choice in choices], ["1080p", "720p"])

    def test_quality_options_hide_low_heights_and_standardize_labels(self) -> None:
        info = {
            "formats": [
                {
                    "format_id": "960v",
                    "ext": "mp4",
                    "height": 960,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                },
                {
                    "format_id": "640v",
                    "ext": "mp4",
                    "height": 640,
                    "vcodec": "avc1.64001F",
                    "acodec": "none",
                },
                {
                    "format_id": "320v",
                    "ext": "mp4",
                    "height": 320,
                    "vcodec": "avc1.4d4015",
                    "acodec": "none",
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                },
            ]
        }

        choices = compatible_quality_options(info, 1080)

        self.assertEqual([choice.height for choice in choices], [960, 640])
        self.assertEqual([choice.label for choice in choices], ["1080p", "720p"])
        self.assertEqual(display_quality_label(428), "480p")

    def test_build_path_plan_groups_by_channel_and_uses_clean_title(self) -> None:
        info = {
            "id": "abcdefghijk",
            "title": "Bad / Title: ok?",
            "channel": "Chan*nel",
            "upload_date": "20260616",
        }

        plan = build_path_plan(info, Path("/youtube_storage"))

        self.assertEqual(plan.item_dir.parent.name, "Chan nel")
        self.assertEqual(plan.item_dir.name, "Bad Title ok")
        self.assertEqual(plan.video_path.name, "Bad Title ok.mp4")
        self.assertNotIn("2026-06-16", plan.item_dir.name)
        self.assertNotIn("abcdefghijk", plan.item_dir.name)
        self.assertEqual(plan.poster_path.name, "poster.jpg")

    def test_apply_audio_language_remuxes_metadata_without_transcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "Clip.mp4"
            video_path.write_bytes(b"old")

            def fake_run(cmd, **kwargs):
                self.assertIn("-c", cmd)
                self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
                self.assertIn("-metadata:s:a:0", cmd)
                self.assertIn("language=rus", cmd)
                Path(cmd[-1]).write_bytes(b"new")

            with patch("youtube_downloads.subprocess.run", side_effect=fake_run) as run:
                language = _apply_audio_language(video_path, "rus")

            self.assertEqual(language, "rus")
            self.assertEqual(video_path.read_bytes(), b"new")
            run.assert_called_once()

    def test_apply_audio_language_auto_skips_remux(self) -> None:
        with patch("youtube_downloads.subprocess.run") as run:
            language = _apply_audio_language(Path("Clip.mp4"), "auto")

        self.assertIsNone(language)
        run.assert_not_called()

    def test_apply_mp4_metadata_sets_channel_as_album_for_plex_collections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "Clip.mp4"
            video_path.write_bytes(b"old")

            def fake_run(cmd, **kwargs):
                self.assertIn("-c", cmd)
                self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
                self.assertIn("title=Clip title", cmd)
                self.assertIn("artist=Channel Name", cmd)
                self.assertIn("album=Channel Name", cmd)
                self.assertIn("date=2026-06-16", cmd)
                self.assertIn("comment=https://www.youtube.com/watch?v=abcdefghijk", cmd)
                self.assertIn("language=und", cmd)
                Path(cmd[-1]).write_bytes(b"new")

            with patch("youtube_downloads.subprocess.run", side_effect=fake_run) as run:
                language = _apply_mp4_metadata(
                    video_path,
                    info={
                        "title": "Clip title",
                        "channel": "Channel Name",
                        "upload_date": "20260616",
                    },
                    canonical_url="https://www.youtube.com/watch?v=abcdefghijk",
                    audio_language="und",
                )

            self.assertEqual(language, "und")
            self.assertEqual(video_path.read_bytes(), b"new")
            run.assert_called_once()

    def test_download_with_retries_recovers_after_transient_timeout(self) -> None:
        attempts = []
        progress = []

        def run_download() -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError(
                    "ERROR: [download] Got error: "
                    "(<HTTPSConnection(host='rr4.googlevideo.com')>: timed out)"
                )

        _download_with_retries(
            run_download,
            progress_hook=progress.append,
            sleep_func=lambda _delay: None,
        )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(progress[0]["status"], "retrying")
        self.assertEqual(progress[0]["attempt"], 2)
        self.assertEqual(progress[0]["max_attempts"], 3)
        self.assertIn("таймаут", progress[0]["reason"])

    def test_download_with_retries_hides_low_level_timeout_after_final_failure(self) -> None:
        attempts = []

        def run_download() -> None:
            attempts.append(1)
            raise RuntimeError(
                "ERROR: [download] Got error: "
                "(<HTTPSConnection(host='rr4.googlevideo.com', port=443)>: "
                "Connection timed out. (connect timeout=20.0))"
            )

        with self.assertRaises(YouTubeDownloadError) as caught:
            _download_with_retries(
                run_download,
                progress_hook=lambda _payload: None,
                sleep_func=lambda _delay: None,
            )

        text = str(caught.exception)
        self.assertEqual(len(attempts), 3)
        self.assertIn("Не удалось скачать видео", text)
        self.assertIn("сетевой таймаут YouTube", text)
        self.assertNotIn("HTTPSConnection", text)
        self.assertNotIn("googlevideo.com", text)

    def test_cleanup_failed_download_removes_partial_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            plan = YouTubePathPlan(
                item_dir=item_dir,
                video_path=item_dir / "Clip.mp4",
                poster_path=item_dir / "poster.jpg",
                fanart_path=item_dir / "fanart.jpg",
                info_path=item_dir / "info.json",
            )
            (item_dir / "Clip.mp4.part").write_bytes(b"partial")
            (item_dir / "Clip.f137.mp4").write_bytes(b"partial")
            plan.info_path.write_text("{}", encoding="utf-8")

            _cleanup_failed_download(plan, preserve_final=False)

            self.assertFalse(item_dir.exists())

    def test_cleanup_failed_download_preserves_existing_final_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "Channel" / "Clip"
            item_dir.mkdir(parents=True)
            plan = YouTubePathPlan(
                item_dir=item_dir,
                video_path=item_dir / "Clip.mp4",
                poster_path=item_dir / "poster.jpg",
                fanart_path=item_dir / "fanart.jpg",
                info_path=item_dir / "info.json",
            )
            plan.video_path.write_bytes(b"ready")
            plan.poster_path.write_bytes(b"poster")
            (item_dir / "Clip.mp4.part").write_bytes(b"partial")

            _cleanup_failed_download(plan, preserve_final=True)

            self.assertTrue(item_dir.exists())
            self.assertEqual(plan.video_path.read_bytes(), b"ready")
            self.assertTrue(plan.poster_path.exists())
            self.assertFalse((item_dir / "Clip.mp4.part").exists())
