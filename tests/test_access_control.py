import unittest

from access_control import (
    access_request_user_label,
    all_allowed_chat_ids,
    is_admin_chat,
    is_allowed_chat,
)


class AccessControlTests(unittest.TestCase):
    def test_all_allowed_chat_ids_merges_config_admins_and_approved(self) -> None:
        self.assertEqual(
            all_allowed_chat_ids({1, 2}, {2, 3}, {4}),
            {1, 2, 3, 4},
        )

    def test_is_admin_chat_handles_none_and_membership(self) -> None:
        self.assertFalse(is_admin_chat(None, {1}))
        self.assertFalse(is_admin_chat(2, {1}))
        self.assertTrue(is_admin_chat(1, {1}))

    def test_is_allowed_chat_accepts_any_allowed_source(self) -> None:
        self.assertTrue(is_allowed_chat(1, {1}, set(), set()))
        self.assertTrue(is_allowed_chat(2, set(), {2}, set()))
        self.assertTrue(is_allowed_chat(3, set(), set(), {3}))
        self.assertFalse(is_allowed_chat(None, {1}, {2}, {3}))
        self.assertFalse(is_allowed_chat(4, {1}, {2}, {3}))

    def test_access_request_user_label_prefers_name_and_username(self) -> None:
        self.assertEqual(access_request_user_label("User Name", "handle", 123), "User Name @handle")
        self.assertEqual(access_request_user_label("", "handle", 123), "@handle")
        self.assertEqual(access_request_user_label("", "", 123), "123")
        self.assertEqual(access_request_user_label(None, None, None), "неизвестно")


if __name__ == "__main__":
    unittest.main()
