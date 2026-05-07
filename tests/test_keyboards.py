import unittest

from keyboards import _admin_diagnostics_keyboard, _admin_panel_keyboard, _final_notification_keyboard


class KeyboardTests(unittest.TestCase):
    def test_final_notification_keyboard_uses_configured_plex_url(self) -> None:
        keyboard = _final_notification_keyboard(
            "tid1",
            show_plex=True,
            plex_url="https://example.com/plex",
        )

        plex_button = keyboard.inline_keyboard[0][0]
        self.assertEqual(plex_button.text, "▶️ Открыть Plex (iOS)")
        self.assertEqual(plex_button.url, "https://example.com/plex")

    def test_final_notification_keyboard_hides_plex_button_when_disabled(self) -> None:
        keyboard = _final_notification_keyboard("tid1", show_plex=False)

        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertNotIn("▶️ Открыть Plex (iOS)", labels)

    def test_final_notification_keyboard_defaults_to_ios_plex_scheme(self) -> None:
        keyboard = _final_notification_keyboard("tid1", show_plex=True)

        plex_button = keyboard.inline_keyboard[0][0]
        self.assertEqual(plex_button.text, "▶️ Открыть Plex (iOS)")
        self.assertEqual(plex_button.url, "plex://")

    def test_admin_panel_keyboard_links_core_sections(self) -> None:
        keyboard = _admin_panel_keyboard()

        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }

        self.assertEqual(buttons["🧭 Диагностика"], "admin:diagnostics")
        self.assertEqual(buttons["👥 Пользователи"], "access:users_refresh")
        self.assertEqual(buttons["📋 Загрузки"], "task:list:all")
        self.assertEqual(buttons["✖️ Закрыть"], "admin:close")

    def test_admin_diagnostics_keyboard_can_return_home(self) -> None:
        keyboard = _admin_diagnostics_keyboard()

        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }

        self.assertEqual(buttons["🔄 Проверить снова"], "admin:diagnostics")
        self.assertEqual(buttons["⬅️ Админ-панель"], "admin:home")
        self.assertEqual(buttons["✖️ Закрыть"], "admin:close")


if __name__ == "__main__":
    unittest.main()
