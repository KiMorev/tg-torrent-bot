import tempfile
import unittest
from pathlib import Path

from search_facts import (
    SearchFact,
    format_search_fact_line,
    load_search_facts,
    select_search_fact,
)
from state_store import JsonStateStore


def _facts(count: int) -> list[SearchFact]:
    return [SearchFact(id=f"fact_{i}", text=f"Факт {i}") for i in range(count)]


def _first(values: list[str]) -> str:
    return values[0]


def _first_sample(values: list[str], count: int) -> list[str]:
    return values[:count]


class SearchFactsTests(unittest.TestCase):
    def test_empty_facts_return_none(self) -> None:
        text, state = select_search_fact([], {}, 100)
        self.assertIsNone(text)
        self.assertEqual(state, {})

    def test_does_not_repeat_inside_current_pool(self) -> None:
        state: dict = {}
        shown: list[str] = []

        for _ in range(3):
            text, state = select_search_fact(
                _facts(3),
                state,
                100,
                pool_size=3,
                refresh_threshold=1.0,
                choice=_first,
                sample=_first_sample,
            )
            shown.append(text)

        self.assertEqual(shown, ["Факт 0", "Факт 1", "Факт 2"])

    def test_histories_are_per_chat(self) -> None:
        state: dict = {}

        first_text, state = select_search_fact(
            _facts(2), state, 100, pool_size=2, choice=_first, sample=_first_sample
        )
        second_text, state = select_search_fact(
            _facts(2), state, 200, pool_size=2, choice=_first, sample=_first_sample
        )

        self.assertEqual(first_text, "Факт 0")
        self.assertEqual(second_text, "Факт 0")
        self.assertEqual(
            state["chats"]["100"]["recent_shown_ids"],
            state["chats"]["200"]["recent_shown_ids"],
        )

    def test_refresh_uses_recent_history_before_repeating(self) -> None:
        state: dict = {
            "chats": {
                "100": {
                    "pool_fact_ids": ["fact_0", "fact_1"],
                    "shown_in_pool": ["fact_0", "fact_1"],
                    "recent_shown_ids": ["fact_0", "fact_1"],
                }
            }
        }

        text, state = select_search_fact(
            _facts(4),
            state,
            100,
            pool_size=2,
            refresh_threshold=0.7,
            choice=_first,
            sample=_first_sample,
        )

        self.assertEqual(text, "Факт 2")
        self.assertEqual(state["chats"]["100"]["pool_fact_ids"], ["fact_2", "fact_3"])

    def test_recent_history_is_limited(self) -> None:
        state = {
            "chats": {
                "100": {
                    "pool_fact_ids": ["fact_2"],
                    "shown_in_pool": [],
                    "recent_shown_ids": ["fact_0", "fact_1"],
                }
            }
        }

        _, state = select_search_fact(
            _facts(3),
            state,
            100,
            pool_size=1,
            recent_limit=2,
            choice=_first,
            sample=_first_sample,
        )

        self.assertEqual(state["chats"]["100"]["recent_shown_ids"], ["fact_1", "fact_2"])

    def test_load_search_facts_skips_invalid_and_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            path.write_text(
                '[{"id":"a","text":"A"},{"id":"a","text":"A2"},{"id":"","text":"bad"}]',
                encoding="utf-8",
            )

            facts = load_search_facts(path)

        self.assertEqual(facts, [SearchFact(id="a", text="A")])

    def test_format_search_fact_line(self) -> None:
        self.assertEqual(format_search_fact_line(None), "")
        self.assertEqual(format_search_fact_line("факт"), "\n\nПока ждёте: факт")


class SearchFactsStateStoreTests(unittest.TestCase):
    def test_search_facts_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonStateStore(
                approved_chat_ids_file=root / "approved.json",
                tracker_processed_file=root / "tracker.json",
                task_owners_file=root / "owners.json",
                notified_tasks_file=root / "notified.json",
                auto_delete_tasks_file=root / "auto_delete.json",
                search_facts_state_file=root / "search_facts_state.json",
            )

            store.save_search_facts_state({"chats": {"100": {"recent_shown_ids": ["a"]}}})

            self.assertEqual(
                store.load_search_facts_state(),
                {"chats": {"100": {"recent_shown_ids": ["a"]}}},
            )


if __name__ == "__main__":
    unittest.main()
