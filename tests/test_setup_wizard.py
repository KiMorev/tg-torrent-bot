import unittest
from unittest.mock import patch

from config import load_settings
from scripts.setup_wizard import (
    InstallerConfig,
    extract_chat_candidates,
    format_env_value,
    installer_probe_url,
    normalize_ds_url,
    render_env,
)


class SetupWizardEnvTests(unittest.TestCase):
    def _config(self) -> InstallerConfig:
        return InstallerConfig(
            bot_token="123:token",
            allowed_chat_ids="100",
            admin_chat_ids="100",
            ds_url="https://host.docker.internal:5001",
            ds_account="tg_bot",
            ds_password="secret",
            ds_destination="video",
            ds_verify_ssl=False,
            timezone="Europe/Moscow",
        )

    def test_render_env_contains_core_settings_and_disables_optional_features(self) -> None:
        text = render_env(self._config())

        self.assertIn("BOT_TOKEN=123:token", text)
        self.assertIn("ALLOWED_CHAT_IDS=100", text)
        self.assertIn("ADMIN_CHAT_IDS=100", text)
        self.assertIn("DS_URL=https://host.docker.internal:5001", text)
        self.assertIn("DS_VERIFY_SSL=false", text)
        self.assertIn("MOVIE_DISCOVERY_ENABLED=false", text)
        self.assertIn("VOICE_SEARCH_ENABLED=false", text)
        self.assertIn("GPT_ENABLED=false", text)

    def test_rendered_env_loads_as_bot_settings(self) -> None:
        env = {}
        for line in render_env(self._config()).splitlines():
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            env[key] = value

        settings = load_settings(env)

        self.assertEqual(settings.bot_token, "123:token")
        self.assertEqual(settings.allowed_chat_ids, {100})
        self.assertEqual(settings.ds_url, "https://host.docker.internal:5001")
        self.assertFalse(settings.ds_verify_ssl)
        self.assertFalse(settings.movie_discovery_enabled)
        self.assertFalse(settings.voice_search_enabled)
        self.assertFalse(settings.gpt_enabled)

    def test_render_env_rejects_missing_required_values(self) -> None:
        config = InstallerConfig(
            bot_token="",
            allowed_chat_ids="100",
            admin_chat_ids="100",
            ds_url="https://host.docker.internal:5001",
            ds_account="tg_bot",
            ds_password="secret",
            ds_destination="video",
            ds_verify_ssl=False,
        )

        with self.assertRaisesRegex(RuntimeError, "BOT_TOKEN"):
            render_env(config)

    def test_format_env_value_quotes_spaces_hash_and_dollar_safely(self) -> None:
        self.assertEqual(format_env_value("simple-token_123"), "simple-token_123")
        self.assertEqual(format_env_value("pa ss#word$1"), "'pa ss#word$1'")
        self.assertEqual(format_env_value("let's go"), "'let\\'s go'")


class SetupWizardParsingTests(unittest.TestCase):
    def test_normalize_ds_url_adds_https_and_strips_trailing_slash(self) -> None:
        self.assertEqual(normalize_ds_url("192.168.1.10:5001/"), "https://192.168.1.10:5001")
        self.assertEqual(normalize_ds_url("http://nas.local:5000/"), "http://nas.local:5000")

    def test_installer_probe_url_maps_container_host_when_wizard_runs_on_nas(self) -> None:
        self.assertEqual(
            installer_probe_url("https://host.docker.internal:5001"),
            "https://127.0.0.1:5001",
        )
        self.assertEqual(
            installer_probe_url("https://192.168.1.10:5001"),
            "https://192.168.1.10:5001",
        )

    def test_installer_probe_url_keeps_container_host_inside_docker_wizard(self) -> None:
        with patch.dict("os.environ", {"PLEXLOADER_WIZARD_IN_DOCKER": "1"}):
            self.assertEqual(
                installer_probe_url("https://host.docker.internal:5001"),
                "https://host.docker.internal:5001",
            )

    def test_extract_chat_candidates_from_get_updates_payload(self) -> None:
        payload = {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "text": "/start",
                        "chat": {
                            "id": 100,
                            "first_name": "Ivan",
                            "username": "ivan",
                        },
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "text": "again",
                        "chat": {
                            "id": 100,
                            "first_name": "Ivan",
                        },
                    },
                },
                {
                    "update_id": 3,
                    "message": {
                        "text": "/start",
                        "chat": {
                            "id": -200,
                            "title": "Family",
                        },
                    },
                },
            ],
        }

        candidates = extract_chat_candidates(payload)

        self.assertEqual([c.chat_id for c in candidates], [100, -200])
        self.assertEqual(candidates[0].label, "Ivan @ivan")
        self.assertEqual(candidates[1].label, "Family")


if __name__ == "__main__":
    unittest.main()
