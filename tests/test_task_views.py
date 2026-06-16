import unittest
from datetime import datetime, timedelta, timezone

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

        self.assertIn("Все загрузки", text)
        self.assertIn("Обновлено: 12:00:00", text)
        self.assertIn("Всего: 3 · Активно: 3 · Завершено: 0 · С ошибкой: 0", text)
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

    def test_format_tasks_empty_state_explains_next_step(self) -> None:
        my_text = format_tasks(
            [],
            scope="my",
            updated_at="12:00:00",
            owners={},
            scope_all="all",
        )
        all_text = format_tasks(
            [],
            scope="all",
            updated_at="12:00:00",
            owners={},
            scope_all="all",
        )

        self.assertIn("В ваших загрузках сейчас пусто", my_text)
        self.assertIn("задачи, которые вы запустили через бот", my_text)
        self.assertIn("нажмите «Обновить»", my_text)
        self.assertIn("Задач сейчас нет", all_text)
        self.assertIn("задачи из Download Station и YouTube-очереди", all_text)

    def test_format_task_card_for_user_hides_technical_fields(self) -> None:
        text = format_task_card(
            {
                "id": "tid1",
                "title": "Movie",
                "status": "finished",
                "size": 100,
                "additional": {"transfer": {"size_downloaded": 100, "speed_download": 0}},
            }
        )

        self.assertIn("Загрузка", text)
        self.assertIn("Файл: Movie", text)
        self.assertNotIn("Задача Download Station", text)
        self.assertNotIn("ID: tid1", text)
        self.assertIn("Статус: ✅ Завершено", text)
        self.assertIn("Скачано: 100.0 B", text)
        self.assertNotIn("Осталось:", text)

    def test_format_task_card_for_admin_includes_technical_fields(self) -> None:
        text = format_task_card(
            {
                "id": "tid1",
                "title": "Movie",
                "status": "finished",
                "size": 100,
                "additional": {"transfer": {"size_downloaded": 100, "speed_download": 0}},
            },
            is_admin=True,
        )

        self.assertIn("Задача", text)
        self.assertNotIn("Задача Download Station", text)
        self.assertIn("Имя: Movie", text)
        self.assertIn("ID: tid1", text)

    def test_format_tasks_my_scope_hides_task_id(self) -> None:
        text = format_tasks(
            [{"id": "tid1", "title": "Movie", "status": "downloading", "size": 100}],
            scope="my",
            updated_at="12:00:00",
            owners={"tid1": 100},
            scope_all="all",
        )

        self.assertIn("Мои загрузки", text)
        self.assertNotIn("ID: tid1", text)

    def test_format_tasks_all_scope_includes_task_id(self) -> None:
        text = format_tasks(
            [{"id": "tid1", "title": "Movie", "status": "downloading", "size": 100}],
            scope="all",
            updated_at="12:00:00",
            owners={"tid1": 100},
            scope_all="all",
        )

        self.assertIn("Все загрузки", text)
        self.assertIn("ID: tid1", text)

    def test_finished_task_shows_short_time_size_and_auto_delete_deadline(self) -> None:
        display_timezone = timezone(timedelta(hours=3), "MSK")
        finished_at = datetime(2026, 6, 2, 14, 20, tzinfo=display_timezone).timestamp()
        now = datetime(2026, 6, 2, 15, 20, tzinfo=display_timezone)
        task = {
            "id": "tid1",
            "title": "Movie",
            "status": "finished",
            "size": 100,
            "additional": {"transfer": {"size_downloaded": 100, "speed_download": 0}},
        }

        list_text = format_tasks(
            [task],
            scope="my",
            updated_at="12:00:00",
            owners={"tid1": 100},
            scope_all="all",
            auto_delete_tasks={"tid1": finished_at},
            auto_delete_enabled=True,
            auto_delete_statuses={"finished"},
            auto_delete_after_hours=24,
            now=now,
            display_timezone=display_timezone,
        )
        card_text = format_task_card(
            task,
            auto_delete_tasks={"tid1": finished_at},
            auto_delete_enabled=True,
            auto_delete_statuses={"finished"},
            auto_delete_after_hours=24,
            now=now,
            display_timezone=display_timezone,
        )

        self.assertIn("Завершено · сегодня 14:20 · 100.0 B", list_text)
        self.assertIn("Автоочистка: через 23 ч", list_text)
        self.assertNotIn("Скорость:", list_text)
        self.assertIn("Статус: ✅ Завершено · сегодня 14:20", card_text)
        self.assertIn("Скачано: 100.0 B", card_text)
        self.assertIn("Автоочистка: через 23 ч", card_text)

    def test_status_summary_counts_seeding_and_soft_complete_as_finished(self) -> None:
        tasks = [
            {"id": "active", "title": "Active", "status": "downloading", "size": 100},
            {
                "id": "seed",
                "title": "Seed",
                "status": "seeding",
                "size": 100,
                "additional": {"transfer": {"size_downloaded": 100}},
            },
            {
                "id": "soft",
                "title": "Soft",
                "status": "error",
                "type": "bt",
                "size": 100,
                "additional": {"transfer": {"size_downloaded": 100}, "detail": {}},
            },
            {"id": "broken", "title": "Broken", "status": "error", "size": 100},
        ]

        text = format_tasks(
            tasks,
            scope="my",
            updated_at="12:00:00",
            owners={},
            scope_all="all",
        )

        self.assertIn("Всего: 4 · Активно: 1 · Завершено: 2 · С ошибкой: 1", text)
        self.assertIn("2. ✅ Seed", text)
        self.assertIn("Завершено · 100.0 B", text)
        self.assertIn("3. ✅ Soft", text)
        self.assertIn("Скачано полностью · 100.0 B", text)
        self.assertIn("Статус: ошибка", text)

    def test_complete_error_is_shown_as_downloaded_in_list_and_card(self) -> None:
        task = {
            "id": "tid1",
            "title": "Movie",
            "status": "error",
            "type": "bt",
            "size": 100,
            "additional": {"transfer": {"size_downloaded": 100, "speed_download": 0}, "detail": {}},
        }

        list_text = format_tasks(
            [task],
            scope="my",
            updated_at="12:00:00",
            owners={"tid1": 100},
            scope_all="all",
        )
        card_text = format_task_card(task)
        admin_card_text = format_task_card(task, is_admin=True)

        self.assertIn("1. ✅ Movie", list_text)
        self.assertIn("Скачано полностью · 100.0 B", list_text)
        self.assertNotIn("Статус: ошибка", list_text)
        self.assertIn("Статус: ✅ Скачано полностью", card_text)
        self.assertIn("Сервис загрузок показывает ошибку, но файл скачан полностью.", card_text)
        self.assertIn("Download Station показывает ошибку, но файл скачан полностью.", admin_card_text)

    def test_has_active_tasks_checks_transfer_statuses(self) -> None:
        self.assertTrue(has_active_tasks([{"status": "waiting"}]))
        self.assertTrue(has_active_tasks([{"status": "DOWNLOADING"}]))
        self.assertFalse(has_active_tasks([{"status": "finished"}]))
