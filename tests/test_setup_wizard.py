import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config import load_settings
from scripts.setup_wizard import (
    InstallerConfig,
    PlexPin,
    ProbeError,
    build_plex_auth_url,
    configure_plex,
    create_plex_pin,
    destination_share_name,
    extract_chat_candidates,
    format_env_value,
    installer_probe_url,
    normalize_ds_url,
    poll_plex_pin,
    probe_download_destination,
    probe_jackett,
    probe_plex,
    probe_plex_resources,
    read_env_file,
    render_env,
    run_interactive,
)


class FakeConsole:
    def __init__(self, *, answers=None, booleans=None) -> None:
        self.answers = list(answers or [])
        self.booleans = list(booleans or [])
        self.output: list[str] = []

    def write(self, text: str = "") -> None:
        self.output.append(text)

    def ask(self, prompt: str, *, default: str = "", secret: bool = False) -> str:
        if not self.answers:
            raise AssertionError(f"No answer left for prompt: {prompt}")
        value = self.answers.pop(0)
        return default if value == "" else value

    def ask_required(self, prompt: str, *, default: str = "", secret: bool = False) -> str:
        value = self.ask(prompt, default=default, secret=secret)
        if not value:
            raise AssertionError(f"Required prompt got empty answer: {prompt}")
        return value

    def ask_yes_no(self, prompt: str, *, default: bool = False) -> bool:
        if not self.booleans:
            raise AssertionError(f"No boolean left for prompt: {prompt}")
        return self.booleans.pop(0)

    def close(self) -> None:
        pass


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

    def test_render_env_contains_selected_integrations(self) -> None:
        values = self._config().__dict__.copy()
        values.update(
            rutracker_username="rt_user",
            rutracker_password="rt_pass",
            jackett_url="http://jackett.local:9117",
            jackett_api_key="jackett-key",
            jackett_indexers="rutracker,kinozal",
            kinopoisk_api_key="kp-key",
            tmdb_api_token="tmdb-token",
            movie_discovery_enabled=True,
            plex_url="http://plex.local:32400",
            plex_token="plex-token",
            plex_movie_section="1",
            plex_auth_client_id="client-1",
            openai_api_key="sk-test",
            voice_search_enabled=True,
            gpt_enabled=True,
        )
        config = InstallerConfig(**values)

        text = render_env(config)

        self.assertIn("RUTRACKER_USERNAME=rt_user", text)
        self.assertIn("JACKETT_INDEXERS=rutracker,kinozal", text)
        self.assertIn("KINOPOISK_API_KEY=kp-key", text)
        self.assertIn("TMDB_API_TOKEN=tmdb-token", text)
        self.assertIn("MOVIE_DISCOVERY_ENABLED=true", text)
        self.assertIn("PLEX_MOVIE_SECTION=1", text)
        self.assertIn("PLEX_AUTH_CLIENT_ID=client-1", text)
        self.assertIn("VOICE_SEARCH_ENABLED=true", text)
        self.assertIn("GPT_ENABLED=true", text)

    def test_render_env_preserves_existing_unmanaged_values(self) -> None:
        text = render_env(
            self._config(),
            {
                "BOT_TOKEN": "old",
                "TRACKERS_MAX": "12",
                "CUSTOM_VALUE": "keep me",
            },
        )

        self.assertIn("BOT_TOKEN=123:token", text)
        self.assertIn("TRACKERS_MAX=12", text)
        self.assertIn("CUSTOM_VALUE='keep me'", text)

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

    def test_read_env_file_parses_single_quoted_values(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("BOT_TOKEN=123:token\nDS_PASSWORD='pa ss#word$1'\n", encoding="utf-8")

            values = read_env_file(path)

        self.assertEqual(values["BOT_TOKEN"], "123:token")
        self.assertEqual(values["DS_PASSWORD"], "pa ss#word$1")


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

    def test_destination_share_name_handles_relative_and_volume_paths(self) -> None:
        self.assertEqual(destination_share_name("video"), "video")
        self.assertEqual(destination_share_name("video/movies"), "video")
        self.assertEqual(destination_share_name("/volume1/video/movies"), "video")


class SetupWizardProbeTests(unittest.TestCase):
    def test_probe_download_destination_rejects_missing_share(self) -> None:
        with patch(
            "scripts.setup_wizard._read_json_url",
            return_value={"success": True, "data": {"shares": [{"name": "downloads"}]}},
        ):
            with self.assertRaisesRegex(ProbeError, "video"):
                probe_download_destination("https://nas.local:5001", "sid", "video", verify_ssl=False)

    def test_probe_jackett_returns_configured_indexers(self) -> None:
        with patch(
            "scripts.setup_wizard._read_json_url",
            return_value=[
                {"id": "rutracker", "name": "Rutracker", "configured": True},
                {"id": "disabled", "name": "Disabled", "configured": False},
            ],
        ):
            indexers = probe_jackett("http://jackett.local:9117", "secret")

        self.assertEqual(indexers, [{"id": "rutracker", "name": "Rutracker"}])

    def test_probe_plex_returns_movie_and_show_sections(self) -> None:
        import xml.etree.ElementTree as ET

        identity = ET.fromstring("<MediaContainer machineIdentifier='m1' />")
        sections = ET.fromstring(
            "<MediaContainer>"
            "<Directory key='1' type='movie' title='Movies' />"
            "<Directory key='2' type='show' title='Shows' />"
            "</MediaContainer>"
        )
        with patch("scripts.setup_wizard._read_xml_url", side_effect=[identity, sections]):
            result = probe_plex("http://plex.local:32400", "token")

        self.assertEqual(
            result,
            [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "2", "title": "Shows", "type": "show"},
            ],
        )

    def test_build_plex_auth_url_contains_client_code_and_product(self) -> None:
        url = build_plex_auth_url("client-1", "pin-code")

        self.assertTrue(url.startswith("https://app.plex.tv/auth#?"))
        self.assertIn("clientID=client-1", url)
        self.assertIn("code=pin-code", url)
        self.assertIn("context%5Bdevice%5D%5Bproduct%5D=PlexLoader", url)

    def test_create_plex_pin_returns_pin_with_auth_url(self) -> None:
        with patch(
            "scripts.setup_wizard._read_json_url",
            return_value={"id": 123, "code": "pin-code"},
        ) as read_json:
            pin = create_plex_pin("client-1")

        self.assertEqual(pin.pin_id, "123")
        self.assertEqual(pin.code, "pin-code")
        self.assertEqual(pin.auth_url, build_plex_auth_url("client-1", "pin-code"))
        self.assertEqual(read_json.call_args.kwargs["data"]["X-Plex-Client-Identifier"], "client-1")

    def test_poll_plex_pin_waits_until_auth_token(self) -> None:
        console = FakeConsole()
        pin = PlexPin(pin_id="123", code="pin-code", auth_url="https://example.test")
        with patch("scripts.setup_wizard.check_plex_pin", side_effect=["", "plex-token"]):
            with patch("scripts.setup_wizard.time.sleep") as sleep:
                token = poll_plex_pin(
                    pin,
                    "client-1",
                    console,
                    timeout_seconds=5,
                    interval_seconds=0.01,
                )

        self.assertEqual(token, "plex-token")
        self.assertEqual(console.output, ["Жду подтверждения Plex..."])
        sleep.assert_called_once_with(0.01)

    def test_probe_plex_resources_returns_server_connections(self) -> None:
        import xml.etree.ElementTree as ET

        resources_xml = ET.fromstring(
            "<MediaContainer>"
            "<Device name='NAS Plex' provides='server' accessToken='server-token'>"
            "<Connection uri='http://192.168.1.10:32400' local='1' relay='0' />"
            "</Device>"
            "<Device name='Plexamp' provides='player'>"
            "<Connection uri='http://ignored.local' />"
            "</Device>"
            "</MediaContainer>"
        )
        with patch("scripts.setup_wizard._read_xml_url", return_value=resources_xml):
            resources = probe_plex_resources("account-token", "client-1")

        self.assertEqual(
            resources,
            [
                {
                    "name": "NAS Plex",
                    "uri": "http://192.168.1.10:32400",
                    "token": "server-token",
                    "local": "1",
                    "relay": "0",
                }
            ],
        )

    def test_configure_plex_auth_flow_uses_reachable_account_resource(self) -> None:
        console = FakeConsole(
            booleans=[True, True],
            answers=[""],
        )
        with patch("scripts.setup_wizard.run_plex_pin_auth", return_value="account-token"):
            with patch(
                "scripts.setup_wizard.probe_plex_resources",
                return_value=[
                    {
                        "name": "NAS Plex",
                        "uri": "http://192.168.1.10:32400",
                        "token": "server-token",
                        "local": "1",
                        "relay": "0",
                    }
                ],
            ):
                with patch(
                    "scripts.setup_wizard.probe_plex",
                    return_value=[{"key": "1", "title": "Movies", "type": "movie"}],
                ):
                    result = configure_plex(
                        console,
                        {"PLEX_AUTH_CLIENT_ID": "client-1"},
                        skip_checks=False,
                    )

        self.assertEqual(
            result,
            (
                "http://192.168.1.10:32400",
                "server-token",
                "1",
                "",
                "client-1",
            ),
        )


class SetupWizardInteractiveTests(unittest.TestCase):
    def test_run_interactive_skip_checks_collects_only_selected_integrations(self) -> None:
        console = FakeConsole(
            booleans=[True, False, True, True, False, False, True],
            answers=[
                "123:token",
                "100",
                "",
                "tg_bot",
                "secret",
                "video",
                "rt_user",
                "rt_pass",
                "",
                "plex-token",
                "",
                "",
                "sk-test",
            ],
        )
        with TemporaryDirectory() as tmp:
            result = run_interactive(Path(tmp), skip_checks=True, console=console)
            text = (Path(tmp) / ".env").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("RUTRACKER_USERNAME=rt_user", text)
        self.assertIn("JACKETT_URL=", text)
        self.assertIn("PLEX_URL=http://host.docker.internal:32400", text)
        self.assertIn("MOVIE_DISCOVERY_ENABLED=true", text)
        self.assertIn("OPENAI_API_KEY=sk-test", text)
        self.assertIn("VOICE_SEARCH_ENABLED=true", text)

    def test_install_script_removes_current_storage_mount_mode(self) -> None:
        install_text = Path("install.sh").read_text(encoding="utf-8")
        compose_text = Path("compose.yaml").read_text(encoding="utf-8")

        self.assertIn("/volume1/video:/storage:rw", compose_text)
        self.assertIn("/volume1/video:/storage:r[ow]", install_text)


if __name__ == "__main__":
    unittest.main()
