import unittest
from pathlib import Path

from youtube_downloads import (
    YouTubeUnsupportedError,
    build_path_plan,
    compatible_quality_options,
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
            ]
        }

        choices = compatible_quality_options(info, 1080)

        self.assertEqual([choice.height for choice in choices], [1080, 720])

    def test_build_path_plan_sanitizes_components_and_keeps_youtube_id(self) -> None:
        info = {
            "id": "abcdefghijk",
            "title": "Bad / Title: ok?",
            "channel": "Chan*nel",
            "upload_date": "20260616",
        }

        plan = build_path_plan(info, Path("/youtube_storage"))

        self.assertEqual(plan.item_dir.parent.name, "Chan nel")
        self.assertIn("2026-06-16 - Bad Title ok [yt-abcdefghijk]", plan.item_dir.name)
        self.assertEqual(plan.video_path.suffix, ".mp4")
        self.assertEqual(plan.poster_path.name, "poster.jpg")
