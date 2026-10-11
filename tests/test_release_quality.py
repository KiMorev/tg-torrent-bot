import unittest

from release_quality import detect_release_quality, is_screen_release, quality_sort_key


class ReleaseQualityTests(unittest.TestCase):
    def test_screen_sources_are_excluded_even_with_resolution(self):
        for marker in (
            "CAM", "CAMRip", "CAM-Rip", "HDCAM", "HDCAMRip", "HD-CAM-Rip",
            "TS", "TSRip", "HDTS", "HD-TS", "Telesync", "TC", "TCRip", "HDTC",
            "Telecine", "SCR", "SCRRip", "DVDSCR", "DVD-Scr", "Screener",
            "Workprint", "TS1080p", "экранка", "экранная копия",
        ):
            with self.subTest(marker=marker):
                self.assertTrue(is_screen_release(f"Movie (2026) {marker} 2160p"))
        for title in ("Film CAM 2026 2160p", "Film TS 2026 1080p", "Film TC (2026) 1080p", "Cam (2018) CAM 1080p"):
            with self.subTest(title=title):
                self.assertTrue(is_screen_release(title))

    def test_valid_titles_and_transport_stream_containers_are_preserved(self):
        for title in (
            "Movie (2026) WEB-DL 2160p MPEG-TS", "Movie (2026) WEB-DL 1080p file.ts",
            "Movie.WEB-DL.1080p.ts", "Cam (2018) WEB-DL 1080p",
            "Cam 2018 WEB-DL 1080p", "Cam / Cam (2018) WEB-DL 1080p", "Cats (2019) WEB-DL 1080p",
            "Movie (2026) BDRemux 2160p HDR10+ DTS-HD MA",
        ):
            with self.subTest(title=title):
                self.assertFalse(is_screen_release(title))

    def test_resolution_precedes_other_scores_and_explicit_preference_is_preserved(self):
        high = {"title": "Movie 2160p WEBRip"}
        low = {"title": "Movie 1080p BDRemux"}
        self.assertGreater(quality_sort_key(high, 0), quality_sort_key(low, 99999))
        self.assertGreater(
            quality_sort_key(low, 0, preferred_quality="1080p"),
            quality_sort_key(high, 99999, preferred_quality="1080p"),
        )
        self.assertGreater(quality_sort_key(high, 2), quality_sort_key(high, 1))

    def test_resolution_aliases_and_unknown_resolution(self):
        self.assertEqual(detect_release_quality("Movie_UHD_2160p"), "2160p")
        self.assertEqual(detect_release_quality("Movie 1080i"), "1080p")
        self.assertIsNone(detect_release_quality("Movie"))
        self.assertGreater(quality_sort_key({"quality": "720p"}), quality_sort_key({}))
