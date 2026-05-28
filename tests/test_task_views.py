import unittest

from task_views import (
    default_list_scope,
    filter_tasks_for_scope,
    find_task,
    format_task_card,
    format_tasks,
    has_active_tasks,
    normalize_list_scope,
)


class TaskViewTests(unittest.TestCase):
    def test_default_and_normalized_scope_respect_admin_access(self) -> None:
        self.assertEqual(default_list_scope(True, scope_all="all", scope_my="my"), "all")
        self.assertEqual(default_list_scope(False, scope_all="all", scope_my="my"), "my")

        self.assertEqual(
            normalize_list_scope(
                "all",
                False,
                scope_all="all",
                scope_my="my",
                scope_default="default",
            ),
            "my",
        )
        self.assertEqual(
            normalize_list_scope(
                "default",
                True,
                scope_all="all",
                scope_my="my",
                scope_default="default",
            ),
            "all",
        )

    def test_filter_tasks_for_scope_returns_all_for_admin_or_owned_tasks_for_user(self) -> None:
        tasks = [{"id": "tid1"}, {"id": "tid2"}, {"id": "tid3"}]
        owners = {"tid1": 100, "tid2": 200}

        self.assertEqual(
            filter_tasks_for_scope(tasks, 100, "all", owners=owners, is_admin=True, scope_all="all"),
            tasks,
        )
        self.assertEqual(
            filter_tasks_for_scope(tasks, 100, "my", owners=owners, is_admin=False, scope_all="all"),
            [{"id": "tid1"}],
        )

    def test_find_task_matches_by_id(self) -> None:
        task = {"id": "tid1", "title": "Movie"}
        self.assertIs(find_task([task], "tid1"), task)
        self.assertIsNone(find_task([task], "missing"))

    def test_format_tasks_includes_owner_and_pagination(self) -> None:
        tasks = [
            {
                "id": f"tid{i}",
                "title": f"Movie {i}",
                "status": "downloading",
                "size": 100,
                "additional": {"transfer": {"size_downloaded": i, "speed_download": 10}},
            }
            for i in range(1, 4)
        ]

        text = format_tasks(
            tasks,
            scope="all",
            updated_at="12:00:00",
            owners={"tid3": 100},
            owner_labels={100: "Ivan (100)"},
            page=1,
            page_size=2,
            scope_all="all",
        )

        self.assertIn("Все задачи Download Station", text)
        self.assertIn("Обновлено: 12:00:00", text)
        self.assertIn("3. ⬇️ Movie 3", text)
        self.assertIn("Владелец: Ivan (100)", text)
        self.assertIn("Страница 2 из 2", text)

    def test_format_tasks_falls_back_to_owner_id_without_label(self) -> None:
        text = format_tasks(
            [{"id": "tid1", "title": "Movie", "status": "downloading", "size": 100}],
            scope="all",
            updated_at="12:00:00",
            owners={"tid1": 100},
            scope_all="all",
        )

        self.assertIn("Владелец: 100", text)

    def test_format_task_card_includes_core_fields(self) -> None:
        text = format_task_card(
            {
                "id": "tid1",
                "title": "Movie",
                "status": "finished",
                "size": 100,
                "additional": {"transfer": {"size_downloaded": 100, "speed_download": 0}},
            }
        )

        self.assertIn("Задача Download Station", text)
        self.assertIn("Имя: Movie", text)
        self.assertIn("ID: tid1", text)
        self.assertIn("Статус: ✅ завершено", text)
        self.assertIn("Скачано: 100.0 B из 100.0 B (100.0%)", text)

    def test_has_active_tasks_checks_transfer_statuses(self) -> None:
        self.assertTrue(has_active_tasks([{"status": "waiting"}]))
        self.assertTrue(has_active_tasks([{"status": "DOWNLOADING"}]))
        self.assertFalse(has_active_tasks([{"status": "finished"}]))
