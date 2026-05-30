import unittest

from search_intent import (
    INTENT_SERIES_MASTER,
    parse_search_intent,
    validate_gpt_intent_payload,
)


class SearchIntentParserTests(unittest.TestCase):
    def test_quality_accepts_cyrillic_p(self):
        draft = parse_search_intent("дюна 1080р")

        self.assertEqual(draft.base_query, "дюна")
        self.assertEqual(draft.quality, "1080p")
        self.assertEqual(draft.confidence, "high")

    def test_whole_series_and_voice_required(self):
        draft = parse_search_intent("скачать клинику целиком в озвучке LostFilm 1080")

        self.assertEqual(draft.base_query, "клинику")
        self.assertEqual(draft.intent, INTENT_SERIES_MASTER)
        self.assertEqual(draft.quality, "1080p")
        self.assertEqual(draft.voice_hints, ("LostFilm",))
        self.assertTrue(draft.voice_required)

    def test_voice_without_marker_stays_in_title(self):
        draft = parse_search_intent("LostFilm документальный фильм 1080")

        self.assertEqual(draft.base_query, "LostFilm документальный")
        self.assertEqual(draft.voice_hints, ())
        self.assertFalse(draft.voice_required)

    def test_bare_voice_after_only_media_word_stays_title(self):
        draft = parse_search_intent("фильм LostFilm 1080")

        self.assertEqual(draft.base_query, "LostFilm")
        self.assertEqual(draft.voice_hints, ())

    def test_bare_voice_after_title_is_preference(self):
        draft = parse_search_intent("клиника LostFilm 1080")

        self.assertEqual(draft.base_query, "клиника")
        self.assertEqual(draft.voice_hints, ("LostFilm",))
        self.assertFalse(draft.voice_required)

    def test_negative_voice_is_removed_but_not_requested(self):
        draft = parse_search_intent("клиника без LostFilm 1080")

        self.assertEqual(draft.base_query, "клиника")
        self.assertEqual(draft.voice_hints, ())
        self.assertFalse(draft.voice_required)

    def test_series_word_alone_does_not_mean_whole_series(self):
        draft = parse_search_intent("сериал клиника 3 сезон 1080")

        self.assertEqual(draft.base_query, "клиника")
        self.assertEqual(draft.season, 3)
        self.assertNotEqual(draft.intent, INTENT_SERIES_MASTER)

    def test_conflicting_quality_is_low_confidence(self):
        draft = parse_search_intent("офис 4к 720")

        self.assertIn("quality", draft.conflicts)
        self.assertEqual(draft.confidence, "low")


class SearchIntentGptValidationTests(unittest.TestCase):
    def test_unknown_voice_is_ignored(self):
        fallback = parse_search_intent("клиника 1080")

        draft = validate_gpt_intent_payload({
            "base_query": "клиника",
            "voice_hints": ["FakeStudio"],
            "voice_required": True,
        }, fallback)

        self.assertEqual(draft.voice_hints, ())
        self.assertFalse(draft.voice_required)

    def test_invalid_quality_falls_back(self):
        fallback = parse_search_intent("дюна 1080")

        draft = validate_gpt_intent_payload({
            "base_query": "дюна",
            "quality": "999p",
        }, fallback)

        self.assertEqual(draft.quality, "1080p")


if __name__ == "__main__":
    unittest.main()
