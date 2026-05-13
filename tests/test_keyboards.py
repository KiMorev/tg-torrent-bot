import unittest

from keyboards import (
    _admin_diagnostics_keyboard,
    _admin_kp_cache_cleared_keyboard,
    _admin_kp_cache_confirm_keyboard,
    _admin_kp_force_refresh_keyboard,
    _admin_panel_keyboard,
    _final_notification_keyboard,
    _jackett_select_keyboard,
    _search_advanced_keyboard,
    _search_options_keyboard,
    _search_results_keyboard,
    tracker_selection_label,
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

    def test_switch_trackers_button_shown_when_requested(self) -> None:
        keyboard = _search_results_keyboard([], show_switch_trackers=True)
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertIn("🔄 Сменить трекеры", buttons)
        self.assertEqual(buttons["🔄 Сменить трекеры"], "srch:switch_trackers")

    def test_direct_rutracker_button_shown_when_requested(self) -> None:
        keyboard = _search_results_keyboard([], show_direct_rutracker=True)
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertIn("🔗 Rutracker напрямую", buttons)
        self.assertEqual(buttons["🔗 Rutracker напрямую"], "srch:direct_rt")

    def test_retry_jackett_button_shown_when_requested(self) -> None:
        keyboard = _search_results_keyboard([], show_retry_jackett=True)
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertIn("↩️ Повторить через Jackett", buttons)
        self.assertEqual(buttons["↩️ Повторить через Jackett"], "srch:switch_trackers")

    def test_retry_jackett_and_switch_trackers_are_mutually_exclusive(self) -> None:
        labels_switch = [b.text for row in _search_results_keyboard([], show_switch_trackers=True).inline_keyboard for b in row]
        labels_retry = [b.text for row in _search_results_keyboard([], show_retry_jackett=True).inline_keyboard for b in row]
        self.assertNotIn("↩️ Повторить через Jackett", labels_switch)
        self.assertNotIn("🔄 Сменить трекеры", labels_retry)

    def test_neither_button_shown_by_default(self) -> None:
        keyboard = _search_results_keyboard([])
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertNotIn("🔄 Сменить трекеры", labels)
        self.assertNotIn("🔗 Прямой поиск Rutracker", labels)

    def test_legacy_show_jackett_expand_maps_to_switch_trackers(self) -> None:
        keyboard = _search_results_keyboard([], show_jackett_expand=True)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🔄 Сменить трекеры", labels)


class SearchOptionsKeyboardTests(unittest.TestCase):
    def test_no_tracker_button_without_label(self) -> None:
        keyboard = _search_options_keyboard()
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertNotIn("🌐", "".join(labels[:3]))  # no tracker button

    def test_tracker_button_shown_with_label(self) -> None:
        keyboard = _search_options_keyboard("Rutracker")
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertIn("🌐 Трекер: Rutracker", buttons)
        self.assertEqual(buttons["🌐 Трекер: Rutracker"], "srch:pick_tracker:options")

    def test_search_and_advanced_always_present(self) -> None:
        keyboard = _search_options_keyboard("Rutracker")
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🟢 Искать", labels)
        self.assertIn("⚙️ Доп. параметры", labels)


class SearchAdvancedKeyboardTests(unittest.TestCase):
    _settings = {"quality": "1080p", "audio": False, "subs": False}

    def test_no_tracker_button_without_label(self) -> None:
        keyboard = _search_advanced_keyboard(self._settings)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertFalse(any("🌐 Трекер:" in lbl for lbl in labels))

    def test_tracker_button_shown_with_label(self) -> None:
        keyboard = _search_advanced_keyboard(self._settings, "NNMClub")
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertIn("🌐 Трекер: NNMClub", buttons)
        self.assertEqual(buttons["🌐 Трекер: NNMClub"], "srch:pick_tracker:advanced")


class JackettSelectKeyboardTests(unittest.TestCase):
    _indexers = [
        {"id": "rutracker", "name": "RuTracker"},
        {"id": "nnmclub", "name": "NNM-Club"},
    ]

    def test_default_confirm_label_is_search(self) -> None:
        keyboard = _jackett_select_keyboard(self._indexers, {"rutracker"})
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🔍 Искать", labels)
        self.assertNotIn("✅ Применить", labels)

    def test_apply_confirm_label(self) -> None:
        keyboard = _jackett_select_keyboard(self._indexers, {"rutracker"}, confirm_label="✅ Применить")
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("✅ Применить", labels)
        self.assertNotIn("🔍 Искать", labels)

    def test_back_button_shown_when_requested(self) -> None:
        keyboard = _jackett_select_keyboard(self._indexers, {"rutracker"}, show_back=True)
        buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
        self.assertIn("⬅️ Назад", buttons)
        self.assertEqual(buttons["⬅️ Назад"], "srch:jk_back")

    def test_cancel_button_shown_by_default(self) -> None:
        keyboard = _jackett_select_keyboard(self._indexers, {"rutracker"})
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("❌ Отмена", labels)
        self.assertNotIn("⬅️ Назад", labels)


class TrackerSelectionLabelTests(unittest.TestCase):
    _indexers = [
        {"id": "rutracker", "name": "RuTracker"},
        {"id": "nnmclub", "name": "NNM-Club"},
        {"id": "kinozal", "name": "Kinozal"},
    ]

    def test_single_tracker(self) -> None:
        self.assertEqual(tracker_selection_label(self._indexers, {"rutracker"}), "RuTracker")

    def test_two_trackers(self) -> None:
        self.assertEqual(
            tracker_selection_label(self._indexers, {"rutracker", "nnmclub"}),
            "RuTracker, NNM-Club",
        )

    def test_all_trackers(self) -> None:
        all_ids = {"rutracker", "nnmclub", "kinozal"}
        self.assertEqual(tracker_selection_label(self._indexers, all_ids), "Все трекеры")

    def test_many_trackers_abbreviated(self) -> None:
        self.assertEqual(
            tracker_selection_label(self._indexers, {"rutracker", "nnmclub", "kinozal"}),
            "Все трекеры",
        )

    def test_empty_indexers_returns_default(self) -> None:
        self.assertEqual(tracker_selection_label([], {"rutracker"}), "Rutracker")

    def test_no_selected_returns_no_trackers(self) -> None:
        self.assertEqual(tracker_selection_label(self._indexers, set()), "нет трекеров")


if __name__ == "__main__":
    unittest.main()
