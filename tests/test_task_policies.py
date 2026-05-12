import unittest

from task_policies import (
    auto_delete_notice,
    format_task_notification,
    is_auto_delete_candidate,
    notification_recipients,
    notification_status_key,
)


class NotificationPolicyTests(unittest.TestCase):
    def test_notification_recipients_prefers_explicit_chat_ids(self) -> None:
        self.assertEqual(
            notification_recipients(
                "tid1",
                explicit_chat_ids={1, 2},
                task_owners={"tid1": 3},
                notify_external_tasks=True,
                fallback_chat_ids={4},
            ),
            {1, 2},
        )

    def test_notification_recipients_falls_back_to_owner_then_external_chats(self) -> None:
        self.assertEqual(
            notification_recipients(
                "tid1",
                explicit_chat_ids=set(),
                task_owners={"tid1": 3},
                notify_external_tasks=True,
                fallback_chat_ids={4},
            ),
            {3},
        )
        self.assertEqual(
            notification_recipients(
                "tid2",
                explicit_chat_ids=set(),
                task_owners={},
                notify_external_tasks=True,
                fallback_chat_ids={4},
            ),
            {4},
        )
        self.assertEqual(
            notification_recipients(
                "tid2",
                explicit_chat_ids=set(),
                task_owners={},
                notify_external_tasks=False,
                fallback_chat_ids={4},
            ),
            set(),
        )

    def test_notification_recipients_ignores_revoked_owner(self) -> None:
        self.assertEqual(
            notification_recipients(
                "tid1",
                explicit_chat_ids=set(),
                task_owners={"tid1": 3},
                notify_external_tasks=False,
                fallback_chat_ids={4},
                allowed_chat_ids={4},
            ),
            set(),
        )

    def test_notification_status_key_deduplicates_finished_and_seeding(self) -> None:
        self.assertEqual(notification_status_key("finished"), "done")
        self.assertEqual(notification_status_key("seeding"), "done")
        self.assertEqual(notification_status_key("error"), "error")
        self.assertEqual(notification_status_key("paused"), "paused")

    def test_auto_delete_notice_respects_enabled_and_statuses(self) -> None:
        self.assertEqual(
            auto_delete_notice(
                "finished",
                enabled=True,
                finished_statuses={"finished"},
                delete_after_hours=24,
            ),
            "Автоочистка: через 24 ч.",
        )
        self.assertEqual(
            auto_delete_notice(
                "downloading",
                enabled=True,
                finished_statuses={"finished"},
                delete_after_hours=24,
            ),
            "",
        )
        self.assertEqual(
            auto_delete_notice(
                "finished",
                enabled=False,
                finished_statuses={"finished"},
                delete_after_hours=24,
            ),
            "",
        )

    def test_format_task_notification_includes_core_task_fields(self) -> None:
        text = format_task_notification(
            {
                "id": "tid1",
                "status": "finished",
                "title": "Movie",
                "size": 10,
                "additional": {"transfer": {"size_downloaded": 10, "speed_download": 0}},
            },
            auto_delete_enabled=True,
            auto_delete_statuses={"finished"},
            auto_delete_after_hours=24,
        )

        self.assertIn("Загрузка завершена", text)
        self.assertIn("Имя: Movie", text)
        self.assertIn("ID: tid1", text)
        self.assertIn("Автоочистка: через 24 ч.", text)

    def test_is_auto_delete_candidate_requires_id_and_matching_status(self) -> None:
        self.assertTrue(is_auto_delete_candidate({"id": "tid1", "status": "finished"}, {"finished"}))
        self.assertFalse(is_auto_delete_candidate({"id": "tid1", "status": "downloading"}, {"finished"}))
        self.assertFalse(is_auto_delete_candidate({"status": "finished"}, {"finished"}))


if __name__ == "__main__":
    unittest.main()
