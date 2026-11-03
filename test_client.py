import threading
import tempfile
import unittest
import socket
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import client
from client import (
    ALL_GAMES,
    EmbeddedHostServer,
    GAME_CHOICES,
    GAME_PROFILES,
    SaveSyncAPI,
    SaveChangeHandler,
    create_archive,
    custom_game_profiles,
    find_active_game_saves,
    auto_upload_delay,
    host_connection_url,
    host_vpn_connection_url,
    local_ipv4_addresses,
    local_snapshot_path,
    normalize_host_connection_method,
    open_url,
    save_remote_saves_locally,
    safe_extract,
    safe_game_folder_name,
)
from watchdog.events import FileModifiedEvent


class ArchiveTests(unittest.TestCase):
    def test_create_archive_preserves_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "saves"
            (source / "world").mkdir(parents=True)
            (source / "world" / "level.dat").write_text("save", encoding="utf-8")
            archive_path = root / "save.zip"

            create_archive(source, archive_path)

            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist(), ["world/level.dat"])
                self.assertEqual(archive.read("world/level.dat"), b"save")

    def test_safe_extract_writes_inside_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "save.zip"
            destination = root / "game-saves"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("world/level.dat", "save")

            safe_extract(archive_path, destination)

            self.assertEqual(
                (destination / "world" / "level.dat").read_text(encoding="utf-8"),
                "save",
            )

    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "malicious.zip"
            destination = root / "game-saves"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")

            with self.assertRaisesRegex(ValueError, "небезопасный путь"):
                safe_extract(archive_path, destination)

            self.assertFalse((root / "outside.txt").exists())

    def test_safe_extract_rejects_case_insensitive_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "duplicate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("World/save.dat", "one")
                archive.writestr("world/save.dat", "two")

            with self.assertRaisesRegex(ValueError, "повторяющиеся пути"):
                safe_extract(archive_path, root / "game-saves")

    def test_safe_extract_rejects_normalized_and_windows_device_paths(self):
        for unsafe_path in ("./world/save.dat", "CON/save.dat"):
            with self.subTest(path=unsafe_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "unsafe.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(unsafe_path, "unsafe")

                with self.assertRaisesRegex(ValueError, "небезопасный путь"):
                    safe_extract(archive_path, root / "game-saves")


class ActiveGameDetectionTests(unittest.TestCase):
    def test_detects_minecraft_instance_save_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance_saves = root / "instances" / "modded instance" / "saves"
            instance_saves.mkdir(parents=True)

            active = find_active_game_saves(
                [
                    {
                        "name": "javaw.exe",
                        "cmdline": [
                            "javaw.exe",
                            "net.minecraft.client.main.Main",
                            "--gameDir",
                            str(instance_saves.parent),
                        ],
                    }
                ],
                appdata=root / "roaming",
                home=root / "user",
            )

            self.assertEqual(active, {"Minecraft": instance_saves})

    def test_detects_elyprism_instance_with_prism_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "ElyPrismLauncher" / "instances" / "1.12.2"
            (instance / "minecraft").mkdir(parents=True)

            active = find_active_game_saves(
                [
                    {
                        "name": "javaw.exe",
                        "cmdline": [
                            "javaw.exe",
                            f"-Djava.library.path={instance / 'natives'}",
                            "-cp",
                            "minecraft-1.12.2-client.jar",
                            "org.prismlauncher.EntryPoint",
                        ],
                    }
                ],
                appdata=root / "roaming",
                home=root / "user",
            )

            self.assertEqual(active, {"Minecraft": instance / "minecraft" / "saves"})
            self.assertFalse((instance / "minecraft" / "saves").exists())

    def test_ignores_java_process_without_minecraft_client(self):
        active = find_active_game_saves(
            [{"name": "javaw.exe", "cmdline": ["javaw.exe", "-jar", "server.jar"]}]
        )

        self.assertEqual(active, {})

    def test_detects_terraria_save_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            terraria_saves = root / "Documents" / "My Games" / "Terraria"
            terraria_saves.mkdir(parents=True)

            active = find_active_game_saves(
                [{"name": "Terraria.exe"}],
                appdata=root / "roaming",
                home=root,
            )

            self.assertEqual(active, {"Terraria": terraria_saves})

    def test_all_profiles_have_process_and_save_location_rules(self):
        self.assertGreaterEqual(len(GAME_PROFILES), 5)
        self.assertTrue(all(profile.process_names for profile in GAME_PROFILES))
        self.assertTrue(all(profile.save_location for profile in GAME_PROFILES))
        self.assertEqual(GAME_CHOICES[0], ALL_GAMES)
        self.assertEqual(GAME_CHOICES[1:], tuple(profile.name for profile in GAME_PROFILES))
        self.assertEqual(len(GAME_CHOICES), len(set(GAME_CHOICES)))

    def test_custom_game_uses_configured_save_path_and_process_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_directory = Path(temporary) / "My Game Saves"
            save_directory.mkdir()
            profiles = custom_game_profiles(
                [
                    {
                        "name": "My Game",
                        "save_directory": str(save_directory),
                        "process_name": "C:\\Games\\mygame.exe",
                    }
                ]
            )

            active = find_active_game_saves(
                [{"name": "mygame.exe"}],
                profiles=GAME_PROFILES + profiles,
            )

            self.assertEqual(active, {"My Game": save_directory})

    def test_custom_game_without_process_is_available_for_manual_use(self):
        profiles = custom_game_profiles(
            [{"name": "My Game", "save_directory": "C:\\Saves\\MyGame"}]
        )

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].process_names, frozenset())
        self.assertEqual(str(profiles[0].save_directory), "C:\\Saves\\MyGame")


class SaveChangeHandlerTests(unittest.TestCase):
    def test_file_changes_trigger_callback_after_debounce(self):
        callback_event = threading.Event()
        original_debounce = client.SAVE_CHANGE_DEBOUNCE_SECONDS
        client.SAVE_CHANGE_DEBOUNCE_SECONDS = 0.02
        handler = SaveChangeHandler(callback_event.set)
        try:
            handler.on_any_event(FileModifiedEvent("world.dat"))
            self.assertTrue(callback_event.wait(timeout=1))
        finally:
            handler.stop()
            client.SAVE_CHANGE_DEBOUNCE_SECONDS = original_debounce

    def test_suspended_handler_ignores_restore_events(self):
        callback_event = threading.Event()
        handler = SaveChangeHandler(callback_event.set)
        try:
            handler.suspend()
            handler.on_any_event(FileModifiedEvent("restored-world.dat"))
            self.assertFalse(callback_event.wait(timeout=0.05))
        finally:
            handler.stop()


class AutoUploadScheduleTests(unittest.TestCase):
    def test_first_upload_is_immediate_and_later_uploads_wait_for_interval(self):
        self.assertEqual(auto_upload_delay(5, None, 100), 0)
        self.assertEqual(auto_upload_delay(5, 100, 200), 200)
        self.assertEqual(auto_upload_delay(5, 100, 500), 0)


class HostConnectionAddressTests(unittest.TestCase):
    def test_porthole_is_default_and_legacy_name_is_migrated(self):
        self.assertEqual(client.DEFAULT_HOST_CONNECTION_METHOD, "porthole")
        self.assertEqual(normalize_host_connection_method(None), "porthole")
        self.assertEqual(normalize_host_connection_method("portholl"), "porthole")
        self.assertEqual(normalize_host_connection_method("upnp"), "upnp")

    def test_builds_remote_https_url_from_public_ipv4_or_dns(self):
        self.assertEqual(
            host_connection_url("8.8.8.8", 8765),
            "https://8.8.8.8:8765",
        )
        self.assertEqual(
            host_connection_url("savesync.example.net", 8765),
            "https://savesync.example.net:8765",
        )

    def test_rejects_addresses_with_scheme_or_invalid_port(self):
        with self.assertRaisesRegex(ValueError, "публичный IPv4"):
            host_connection_url("https://example.com", 8765)
        with self.assertRaisesRegex(ValueError, "Порт"):
            host_connection_url("8.8.8.8", 70000)

    def test_builds_vpn_url_and_rejects_non_vpn_addresses(self):
        self.assertEqual(
            host_vpn_connection_url("26.12.34.56", 8765),
            "https://26.12.34.56:8765",
        )
        for address in ("127.0.0.1", "0.0.0.0", "not-an-ip"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                host_vpn_connection_url(address, 8765)

    def test_lists_unique_non_loopback_ipv4_addresses(self):
        interfaces = {
            "Radmin VPN": [
                SimpleNamespace(family=socket.AF_INET, address="26.12.34.56"),
                SimpleNamespace(family=socket.AF_INET, address="26.12.34.56"),
            ],
            "Local": [
                SimpleNamespace(family=socket.AF_INET, address="127.0.0.1"),
                SimpleNamespace(family=socket.AF_INET, address="192.168.1.5"),
            ],
        }
        with patch("client.psutil.net_if_addrs", return_value=interfaces):
            self.assertEqual(
                local_ipv4_addresses(),
                ["26.12.34.56", "192.168.1.5"],
            )


class LocalSnapshotPathTests(unittest.TestCase):
    def test_game_name_is_sanitized_for_local_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = local_snapshot_path(
                Path(temporary),
                {"id": "save-123", "game": "../CON:Game"},
            )

        self.assertEqual(path.parent.name, safe_game_folder_name("../CON:Game"))
        self.assertEqual(path.name, "save-123.zip")

    def test_invalid_remote_save_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "идентификатор"):
                local_snapshot_path(
                    Path(temporary),
                    {"id": "../outside", "game": "Minecraft"},
                )

    def test_remote_snapshots_are_downloaded_once_per_local_folder(self):
        class FakeAPI:
            def list_saves(self, game):
                return [{"id": "save-123", "game": "Minecraft"}]

            def download(self, save_id, destination):
                Path(destination).write_bytes(b"archive")

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            api = FakeAPI()

            first_count = save_remote_saves_locally(api, destination)
            second_count = save_remote_saves_locally(api, destination)

            self.assertEqual(first_count, 1)
            self.assertEqual(second_count, 0)
            self.assertEqual(
                (destination / "Minecraft" / "save-123.zip").read_bytes(),
                b"archive",
            )


class EmbeddedHostServerTests(unittest.TestCase):
    def test_server_starts_and_serves_health_on_lan_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            server = EmbeddedHostServer(Path(temporary), port, allow_uploads=True)
            self.assertIsNone(server.server.config.log_config)
            try:
                server.start()
                base_url = f"https://127.0.0.1:{port}"
                with open_url(
                    urllib.request.Request(f"{base_url}/health"),
                    timeout=5,
                    fingerprint=server.certificate_fingerprint,
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'{"status":"ok"}')
                room = SaveSyncAPI.create_room(
                    base_url,
                    "Host integration",
                    "password123",
                    server.certificate_fingerprint,
                )
                api = SaveSyncAPI(
                    base_url,
                    room["code"],
                    "password123",
                    server.certificate_fingerprint,
                )
                self.assertEqual(api.room_info()["name"], "Host integration")
                wrong_pin_api = SaveSyncAPI(
                    base_url,
                    room["code"],
                    "password123",
                    "0" * 64,
                )
                with self.assertRaisesRegex(RuntimeError, "отпечаток"):
                    wrong_pin_api.room_info()
                source = Path(temporary) / "game-save"
                source.mkdir()
                (source / "world.dat").write_text("save", encoding="utf-8")
                archive_path = Path(temporary) / "upload.zip"
                create_archive(source, archive_path)

                api.upload("Minecraft", "Alice", archive_path, max_saves=2)

                saves = api.list_saves("")
                self.assertEqual(len(saves), 1)
                self.assertEqual(saves[0]["game"], "Minecraft")
                self.assertEqual(saves[0]["uploaded_by"], "Alice")
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
