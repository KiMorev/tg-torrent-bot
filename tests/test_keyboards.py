import unittest

from keyboards import (
    _admin_diagnostics_keyboard,
    _admin_kp_cache_cleared_keyboard,
    _admin_kp_cache_confirm_keyboard,
    _admin_kp_force_refresh_keyboard,
    _admin_panel_keyboard,
    _final_notification_keyboard,
    _search_results_keyboard,
)


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
        self.assertEqual(buttons["🔔 Подписки"], "admin:subscriptions")
        self.assertEqual(buttons["✖️ Закрыть"], "admin:close")

    def test_admin_panel_keyboard_has_kp_cache_clear_button(self) -> None:
        keyboard = _admin_panel_keyboard()

        buttons = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }

        self.assertIn("🗑 Очистить KP кеш", buttons, "KP cache clear button must be present")
        self.assertEqual(buttons["🗑 Очистить KP кеш"], "admin:clear_kp_cache")

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


class AdminKpCacheKeyboardTests(unittest.TestCase):
    def _buttons(self, keyboard) -> dict[str, str]:
        return {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}

    def test_confirm_keyboard_has_confirm_and_back_buttons(self) -> None:
        buttons = self._buttons(_admin_kp_cache_confirm_keyboard())
        self.assertEqual(buttons["✅ Да, очистить"], "admin:confirm_clear_kp_cache")
        self.assertEqual(buttons["⬅️ Назад"], "admin:home")
        self.assertEqual(buttons["✖️ Закрыть"], "admin:close")

    def test_confirm_keyboard_has_no_destructive_action_by_default(self) -> None:
        """The confirm keyboard must not contain the panel home button under a confusing label."""
        labels = [b.text for row in _admin_kp_cache_confirm_keyboard().inline_keyboard for b in row]
        self.assertNotIn("🗑 Очистить KP кеш", labels)

    def test_cleared_keyboard_returns_to_admin_panel(self) -> None:
        buttons = self._buttons(_admin_kp_cache_cleared_keyboard())
        self.assertEqual(buttons["⬅️ Админ-панель"], "admin:home")
        self.assertEqual(buttons["✖️ Закрыть"], "admin:close")

    def test_cleared_keyboard_has_no_confirm_button(self) -> None:
        labels = [b.text for row in _admin_kp_cache_cleared_keyboard().inline_keyboard for b in row]
        self.assertNotIn("✅ Да, очистить", labels)


class AdminKpForceRefreshKeyboardTests(unittest.TestCase):
    def _buttons(self, keyboard) -> dict[str, str]:
        return {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}

    def _labels(self, keyboard) -> list[str]:
        return [b.text for row in keyboard.inline_keyboard for b in row]

    def test_full_refresh_button_present_when_budget_allows(self) -> None:
        buttons = self._buttons(_admin_kp_force_refresh_keyboard(can_full=True))
        self.assertIn("✅ Обновить за один прогон", buttons)
        self.assertEqual(buttons["✅ Обновить за один прогон"], "admin:confirm_force_kp_refresh_full")

    def test_full_refresh_button_absent_when_budget_insufficient(self) -> None:
        labels = self._labels(_admin_kp_force_refresh_keyboard(can_full=False))
        self.assertNotIn("✅ Обновить за один прогон", labels)

    def test_gradual_refresh_button_always_present(self) -> None:
        for can_full in (True, False):
            with self.subTest(can_full=can_full):
                buttons = self._buttons(_admin_kp_force_refresh_keyboard(can_full=can_full))
                self.assertIn("🔄 Обновлять постепенно", buttons)
                self.assertEqual(buttons["🔄 Обновлять постепенно"], "admin:confirm_force_kp_refresh_gradual")

    def test_back_and_close_buttons_always_present(self) -> None:
        for can_full in (True, False):
            with self.subTest(can_full=can_full):
                buttons = self._buttons(_admin_kp_force_refresh_keyboard(can_full=can_full))
                self.assertEqual(buttons["⬅️ Назад"], "admin:home")
                self.assertEqual(buttons["✖️ Закрыть"], "admin:close")

    def test_admin_panel_has_force_refresh_button(self) -> None:
        buttons = self._buttons(_admin_panel_keyboard())
        self.assertIn("🔄 Обновить KP кэш", buttons)
        self.assertEqual(buttons["🔄 Обновить KP кэш"], "admin:force_kp_refresh")

    def test_admin_panel_still_has_clear_button(self) -> None:
        buttons = self._buttons(_admin_panel_keyboard())
        self.assertIn("🗑 Очистить KP кеш", buttons)


class SearchResultsKeyboardTests(unittest.TestCase):
    def test_back_to_discovery_button_shown_when_requested(self) -> None:
        keyboard = _search_results_keyboard([], show_back_to_discovery=True)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🎬 ← Новинки", labels)

    def test_back_to_discovery_button_absent_by_default(self) -> None:
        keyboard = _search_results_keyboard([])
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertNotIn("🎬 ← Новинки", labels)

    def test_back_to_discovery_callback_data(self) -> None:
        keyboard = _search_results_keyboard([], show_back_to_discovery=True)
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertEqual(buttons["🎬 ← Новинки"], "new:back")


if __name__ == "__main__":
    unittest.main()
