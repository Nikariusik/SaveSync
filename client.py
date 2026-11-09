import hashlib
import ipaddress
import importlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import psutil
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

MAX_EXTRACTED_BYTES = 10 * 1024**3
MAX_ARCHIVE_ENTRIES = 100_000
CHUNK_SIZE = 1024 * 1024
SAVE_CHANGE_DEBOUNCE_SECONDS = 4
SAVE_CHANGE_MAX_WAIT_SECONDS = 30
GAME_EXIT_GRACE_SECONDS = 15
DEFAULT_AUTO_SAVE_INTERVAL_MINUTES = 5
MIN_AUTO_SAVE_INTERVAL_MINUTES = 1
MAX_AUTO_SAVE_INTERVAL_MINUTES = 1440
DEFAULT_MAX_SAVES_PER_GAME = 10
MIN_MAX_SAVES_PER_GAME = 1
MAX_MAX_SAVES_PER_GAME = 100
DEFAULT_HOST_PORT = 8765
LOCAL_SAVE_SYNC_INTERVAL_MS = 30_000
APP_VERSION = "0.1.0-alpha.3"
GITHUB_REPOSITORY_URL = "https://github.com/Nikariusik/SaveSync"
GITHUB_RELEASES_API_URL = (
    "https://api.github.com/repos/Nikariusik/SaveSync/releases?per_page=100"
)
GITHUB_RELEASES_URL = f"{GITHUB_REPOSITORY_URL}/releases"
MAX_UPDATE_SIZE = 1024**3
MINECRAFT_WORLD_PREFIX = "Minecraft — "
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class GameProfile:
    name: str
    process_names: frozenset[str]
    save_location: str
    process_markers: tuple[str, ...] = ()
    save_directory: Path | None = None


GAME_PROFILES = (
    GameProfile(
        "Minecraft",
        frozenset({"java.exe", "javaw.exe"}),
        "minecraft",
        (
            "net.minecraft.client.main.main",
            "net.minecraft.launchwrapper.launch",
            "net.fabricmc.loader.impl.launch.knot.knotclient",
            "cpw.mods.bootstraplauncher.bootstraplauncher",
            "org.prismlauncher.entrypoint",
            "--gamedir",
        ),
    ),
    GameProfile("Terraria", frozenset({"terraria.exe"}), "terraria"),
    GameProfile(
        "Project Zomboid",
        frozenset({"projectzomboid64.exe", "projectzomboid32.exe", "projectzomboid.exe"}),
        "project-zomboid",
    ),
    GameProfile("Stardew Valley", frozenset({"stardew valley.exe"}), "stardew-valley"),
    GameProfile("Valheim", frozenset({"valheim.exe"}), "valheim"),
)
ALL_GAMES = "Все игры"
GAME_CHOICES = (ALL_GAMES, *(profile.name for profile in GAME_PROFILES))


def auto_upload_delay(
    interval_minutes: int,
    last_upload_at: float | None,
    now: float,
) -> float:
    if last_upload_at is None:
        return 0
    return max(0, last_upload_at + interval_minutes * 60 - now)


def local_server_url(address: str, port: int) -> str:
    host = address.strip()
    try:
        address_value = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Не удалось определить IPv4-адрес этого компьютера.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Порт должен быть от 1 до 65535.")
    if address_value.is_unspecified or address_value.is_loopback:
        raise ValueError("Укажите доступный в локальной сети IPv4-адрес.")
    return f"http://{address_value}:{port}"


def parse_release_version(tag: str) -> tuple[int, int, int, int, int] | None:
    match = re.fullmatch(
        r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?",
        tag,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    major, minor, patch, prerelease, prerelease_number = match.groups()
    prerelease_rank = {"alpha": 0, "beta": 1, "rc": 2, None: 3}
    return (
        int(major),
        int(minor),
        int(patch),
        prerelease_rank[prerelease.casefold() if prerelease else None],
        int(prerelease_number or 0),
    )


def newest_update_release(
    releases: object,
    current_version: str = APP_VERSION,
) -> dict[str, object] | None:
    current = parse_release_version(current_version)
    if current is None:
        raise ValueError(f"Внутренняя версия приложения некорректна: {current_version}")
    if not isinstance(releases, list):
        raise ValueError("GitHub вернул некорректный список релизов")

    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = release.get("tag_name")
        version = parse_release_version(tag) if isinstance(tag, str) else None
        if version is not None and version > current:
            candidates.append((version, release))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def release_executable_asset(release: dict[str, object]) -> tuple[str, str, int]:
    tag = release.get("tag_name")
    if not isinstance(tag, str) or parse_release_version(tag) is None:
        raise ValueError("Релиз имеет неподдерживаемый номер версии")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("В релизе отсутствует EXE-файл")
    version_name = tag[1:] if tag[:1].casefold() == "v" else tag
    expected_names = {
        f"SaveSync-{tag}.exe",
        f"SaveSync-{version_name}.exe",
        "SaveSync.exe",
    }
    matching = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") in expected_names
    ]
    if len(matching) != 1:
        raise ValueError(
            "В релизе должен быть ровно один совместимый файл SaveSync EXE"
        )
    asset = matching[0]
    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        raise ValueError("GitHub не указал ссылку на EXE")
    parsed_url = urllib.parse.urlsplit(url)
    expected_path = (
        f"/Nikariusik/SaveSync/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(str(asset['name']), safe='')}"
    )
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.casefold() != "github.com"
        or parsed_url.path != expected_path
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("Ссылка на EXE ведёт не на официальный релиз SaveSync")
    digest = asset.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("У файла релиза отсутствует корректная SHA-256 контрольная сумма")
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_UPDATE_SIZE:
        raise ValueError("Размер EXE в релизе некорректен или превышает 1 ГБ")
    return url, digest.removeprefix("sha256:").lower(), size


def safe_game_folder_name(game: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", game).strip(" .")
    if not name:
        name = "game"
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        name += "_"
    return name[:80]


def local_snapshot_path(destination: Path, save: dict[str, str]) -> Path:
    save_id = save.get("id", "")
    game = save.get("game", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", save_id):
        raise ValueError("Сервер вернул недопустимый идентификатор сохранения")
    if not game:
        raise ValueError("Сервер вернул сохранение без названия игры")
    return destination / safe_game_folder_name(game) / f"{save_id}.zip"


def save_remote_saves_locally(api: "SaveSyncAPI", destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for save in api.list_saves(""):
        target = local_snapshot_path(destination, save)
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target.with_suffix(".part")
        try:
            api.download(save["id"], temporary_path)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
        downloaded += 1
    return downloaded


class EmbeddedHostServer:
    def __init__(self, storage_root: Path, port: int, allow_uploads: bool):
        self.storage_root = storage_root.resolve()
        self.port = port
        self.allow_uploads = allow_uploads
        previous_storage_dir = os.environ.get("SAVE_STORAGE_DIR")
        os.environ["SAVE_STORAGE_DIR"] = str(storage_root)
        try:
            server_module = importlib.import_module("server")
        finally:
            if previous_storage_dir is None:
                os.environ.pop("SAVE_STORAGE_DIR", None)
            else:
                os.environ["SAVE_STORAGE_DIR"] = previous_storage_dir
        uvicorn = importlib.import_module("uvicorn")
        app = server_module.create_app(
            storage_root=storage_root,
            allow_uploads=allow_uploads,
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=port,
                log_level="warning",
                log_config=None,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run,
            name="savesync-host",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while self.thread.is_alive() and not self.server.started:
            if time.monotonic() >= deadline:
                self.server.should_exit = True
                self.thread.join(timeout=2)
                raise RuntimeError("Время запуска сервера истекло")
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("Не удалось запустить сервер. Проверьте порт и настройки.")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("Сервер не завершился за отведённое время")


def open_url(
    request: urllib.request.Request,
    timeout: int,
):
    return urllib.request.urlopen(request, timeout=timeout)


def check_github_update() -> dict[str, object] | None:
    request = urllib.request.Request(
        GITHUB_RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"SaveSync/{APP_VERSION}",
        },
    )
    try:
        with open_url(request, timeout=30) as response:
            releases = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        raise RuntimeError(f"GitHub ответил {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Не удалось проверить GitHub: {exc}") from exc

    release = newest_update_release(releases)
    if release is not None:
        release_executable_asset(release)
    return release


def download_update_executable(
    release: dict[str, object],
    progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, Path]:
    url, expected_digest, expected_size = release_executable_asset(release)
    download_directory = Path(
        tempfile.mkdtemp(prefix="savesync-update-")
    )
    download_path = download_directory / "SaveSync-update.exe"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"SaveSync/{APP_VERSION}"},
    )
    digest = hashlib.sha256()
    received = 0
    try:
        with open_url(request, timeout=60) as response:
            final_url = urllib.parse.urlsplit(response.geturl())
            final_host = (final_url.hostname or "").casefold()
            if (
                final_url.scheme != "https"
                or not (
                    final_host == "github.com"
                    or final_host.endswith(".githubusercontent.com")
                )
            ):
                raise RuntimeError("GitHub перенаправил скачивание на недоверенный адрес")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    advertised_size = int(content_length)
                except ValueError as exc:
                    raise RuntimeError("GitHub вернул некорректный размер EXE") from exc
                if advertised_size != expected_size:
                    raise RuntimeError("Размер EXE не совпадает с данными релиза")
            with download_path.open("wb") as output:
                while chunk := response.read(CHUNK_SIZE):
                    received += len(chunk)
                    if received > expected_size or received > MAX_UPDATE_SIZE:
                        raise RuntimeError("Скачанный EXE превышает указанный размер")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(received, expected_size)
        if received != expected_size:
            raise RuntimeError("Скачанный EXE имеет неполный размер")
        if digest.hexdigest() != expected_digest:
            raise RuntimeError("SHA-256 скачанного EXE не совпадает с релизом GitHub")
        return download_path, download_directory
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
            shutil.rmtree(download_directory, ignore_errors=True)
        raise RuntimeError(f"GitHub ответил {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        shutil.rmtree(download_directory, ignore_errors=True)
        raise RuntimeError(f"Не удалось скачать обновление: {exc}") from exc
    except Exception:
        shutil.rmtree(download_directory, ignore_errors=True)
        raise


def stage_update_executable(download_path: Path, target_path: Path) -> Path:
    if os.name != "nt":
        raise RuntimeError("Автоматическая установка обновлений доступна только в Windows")
    target_path = target_path.resolve()
    if target_path.suffix.casefold() != ".exe" or not target_path.is_file():
        raise RuntimeError("Не удалось определить EXE-файл SaveSync для обновления")
    if not download_path.is_file():
        raise RuntimeError("Скачанный EXE не найден")
    descriptor, staged_name = tempfile.mkstemp(
        prefix=".SaveSync-update-",
        suffix=".exe",
        dir=target_path.parent,
    )
    os.close(descriptor)
    staged_path = Path(staged_name)
    try:
        shutil.copyfile(download_path, staged_path)
    except OSError:
        staged_path.unlink(missing_ok=True)
        raise
    return staged_path


def start_windows_updater(
    staged_path: Path,
    target_path: Path,
    download_directory: Path,
    process_id: int,
) -> subprocess.Popen:
    if os.name != "nt":
        raise RuntimeError("Автоматическая установка обновлений доступна только в Windows")
    target_path = target_path.resolve()
    staged_path = staged_path.resolve()
    if staged_path.parent != target_path.parent:
        raise ValueError("Временная копия EXE должна быть рядом с приложением")
    script_path = download_directory / "install-update.ps1"
    script = r"""
param(
    [int]$WaitForProcess,
    [string]$StagedFile,
    [string]$TargetFile,
    [string]$CleanupDirectory,
    [string]$ScriptFile
)
$ErrorActionPreference = 'Stop'
$backupFile = "$TargetFile.previous"
try {
    $deadline = (Get-Date).AddMinutes(5)
    while (Get-Process -Id $WaitForProcess -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) {
            throw 'SaveSync did not exit in time.'
        }
        Start-Sleep -Milliseconds 400
    }

    if (Test-Path -LiteralPath $backupFile) {
        Remove-Item -LiteralPath $backupFile -Force -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $TargetFile -Destination $backupFile -Force
    try {
        Move-Item -LiteralPath $StagedFile -Destination $TargetFile -Force
        $newProcess = Start-Process -FilePath $TargetFile -PassThru
        Start-Sleep -Seconds 3
        $newProcess.Refresh()
        if ($newProcess.HasExited) {
            throw 'Updated SaveSync did not start successfully.'
        }
        Remove-Item -LiteralPath $backupFile -Force -ErrorAction SilentlyContinue
    } catch {
        if (Test-Path -LiteralPath $TargetFile) {
            Remove-Item -LiteralPath $TargetFile -Force
        }
        if (Test-Path -LiteralPath $backupFile) {
            Move-Item -LiteralPath $backupFile -Destination $TargetFile -Force
            Start-Process -FilePath $TargetFile
        }
        throw
    }
} catch {
    $logFile = Join-Path $env:TEMP (
        'savesync-update-' + [guid]::NewGuid().ToString('N') + '.log'
    )
    $_ | Out-String | Set-Content -LiteralPath $logFile -Encoding UTF8
    Start-Process -FilePath 'notepad.exe' -ArgumentList $logFile
} finally {
    if (Test-Path -LiteralPath $StagedFile) {
        Remove-Item -LiteralPath $StagedFile -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $CleanupDirectory) {
        Remove-Item -LiteralPath $CleanupDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $ScriptFile) {
        Remove-Item -LiteralPath $ScriptFile -Force -ErrorAction SilentlyContinue
    }
}
"""
    script_path.write_text(script, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        return subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script_path),
                "-WaitForProcess",
                str(process_id),
                "-StagedFile",
                str(staged_path),
                "-TargetFile",
                str(target_path),
                "-CleanupDirectory",
                str(download_directory),
                "-ScriptFile",
                str(script_path),
            ],
            creationflags=flags,
            close_fds=True,
        )
    except OSError:
        script_path.unlink(missing_ok=True)
        raise


def custom_game_profiles(settings: object) -> tuple[GameProfile, ...]:
    if not isinstance(settings, list):
        raise ValueError("Список пользовательских игр должен быть массивом")
    profiles = []
    reserved_names = {profile.name.casefold() for profile in GAME_PROFILES}
    for item in settings:
        if not isinstance(item, dict):
            raise ValueError("Настройка пользовательской игры должна быть объектом")
        name = item.get("name")
        save_directory = item.get("save_directory")
        process_name = item.get("process_name", "")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(save_directory, str)
            or not save_directory.strip()
            or not isinstance(process_name, str)
        ):
            raise ValueError("У пользовательской игры должны быть название и папка сейвов")
        name = name.strip()
        if name.casefold() in reserved_names:
            raise ValueError(f"Игра «{name}» уже есть в списке или добавлена повторно")
        reserved_names.add(name.casefold())
        process_name = Path(process_name.strip()).name.casefold() if process_name.strip() else ""
        profiles.append(
            GameProfile(
                name,
                frozenset({process_name}) if process_name else frozenset(),
                "custom",
                save_directory=Path(save_directory).expanduser(),
            )
        )
    return tuple(profiles)


def game_profiles_with_custom(settings: object) -> tuple[GameProfile, ...]:
    return GAME_PROFILES + custom_game_profiles(settings)


def _minecraft_game_directory(command_line: str | list[str] | tuple[str, ...]) -> Path | None:
    if isinstance(command_line, (list, tuple)):
        for index, argument in enumerate(command_line):
            argument = str(argument)
            if argument.casefold() == "--gamedir" and index + 1 < len(command_line):
                return Path(str(command_line[index + 1]).strip('"')).expanduser()
            if argument.casefold().startswith("--gamedir="):
                return Path(argument.split("=", 1)[1].strip('"')).expanduser()
        return None
    match = re.search(
        r'--gameDir(?:=|\s+)(?:"([^"]+)"|([^\s"]+))',
        command_line,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return Path(match.group(1) or match.group(2)).expanduser()


def _minecraft_instance_save_directories(
    command_line: str | list[str] | tuple[str, ...],
) -> list[Path]:
    arguments = (
        command_line
        if isinstance(command_line, (list, tuple))
        else command_line.split()
    )
    for argument in arguments:
        prefix = "-Djava.library.path="
        value = str(argument)
        if not value.casefold().startswith(prefix.casefold()):
            continue
        native_directory = Path(value[len(prefix):].strip('"')).expanduser()
        if native_directory.name.casefold() != "natives":
            continue
        instance_directory = native_directory.parent
        return [
            instance_directory / "minecraft" / "saves",
            instance_directory / ".minecraft" / "saves",
            instance_directory / "saves",
        ]
    return []


def game_save_candidates(
    profile: GameProfile,
    appdata: Path | None = None,
    home: Path | None = None,
    command_line: str | list[str] | tuple[str, ...] = "",
) -> list[Path]:
    home_path = Path(home or Path.home())
    roaming = Path(appdata or os.environ.get("APPDATA", home_path / "AppData" / "Roaming"))
    documents = [home_path / "Documents"]
    if os.environ.get("OneDrive"):
        documents.append(Path(os.environ["OneDrive"]) / "Documents")

    if profile.save_location == "minecraft":
        game_directory = _minecraft_game_directory(command_line)
        if game_directory is not None:
            return [game_directory / "saves"]
        return _minecraft_instance_save_directories(command_line) + [
            roaming / ".minecraft" / "saves"
        ]
    if profile.save_location == "terraria":
        return [path / "My Games" / "Terraria" for path in documents]
    if profile.save_location == "project-zomboid":
        return [home_path / "Zomboid" / "Saves"] + [
            path / "Zomboid" / "Saves" for path in documents
        ]
    if profile.save_location == "stardew-valley":
        return [roaming / "StardewValley" / "Saves"]
    if profile.save_location == "valheim":
        return [home_path / "AppData" / "LocalLow" / "IronGate" / "Valheim" / "worlds_local"]
    if profile.save_location == "custom" and profile.save_directory is not None:
        return [profile.save_directory]
    return []


def find_active_game_saves(
    processes: list[dict[str, object]],
    appdata: Path | None = None,
    home: Path | None = None,
    profiles: tuple[GameProfile, ...] | None = None,
) -> dict[str, Path | None]:
    active: dict[str, Path | None] = {}
    supported_profiles = profiles if profiles is not None else GAME_PROFILES
    for process in processes:
        process_names = {
            Path(str(value)).name.casefold()
            for value in (process.get("name"), process.get("exe"))
            if value
        }
        raw_command_line = process.get("cmdline") or []
        command_line = (
            " ".join(str(part) for part in raw_command_line)
            if isinstance(raw_command_line, (list, tuple))
            else str(raw_command_line)
        )
        command_line_folded = command_line.casefold()

        for profile in supported_profiles:
            if not process_names.intersection(profile.process_names):
                continue
            if profile.process_markers and not any(
                marker in command_line_folded for marker in profile.process_markers
            ):
                continue
            if profile.name in active and active[profile.name] is not None:
                continue
            save_path = next(
                (
                    candidate
                    for candidate in game_save_candidates(
                        profile, appdata, home, raw_command_line
                    )
                    if candidate.is_dir()
                    or (
                        profile.save_location == "minecraft"
                        and candidate.parent.is_dir()
                    )
                ),
                None,
            )
            active[profile.name] = save_path
    return active


class SaveChangeHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.first_change: float | None = None
        self.suspended = False
        self.ignore_until = 0.0

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in {"created", "modified", "deleted", "moved"}:
            return
        with self.lock:
            now = time.monotonic()
            if self.suspended or now < self.ignore_until:
                return
            if self.first_change is None:
                self.first_change = now
            elapsed = now - self.first_change
            delay = min(SAVE_CHANGE_DEBOUNCE_SECONDS, max(0, SAVE_CHANGE_MAX_WAIT_SECONDS - elapsed))
            if self.timer is not None:
                self.timer.cancel()
            self.timer = threading.Timer(delay, self._flush)
            self.timer.daemon = True
            self.timer.start()

    def _flush(self) -> None:
        with self.lock:
            self.timer = None
            self.first_change = None
        self.callback()

    def stop(self) -> None:
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.first_change = None

    def suspend(self) -> None:
        with self.lock:
            self.suspended = True
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.first_change = None

    def resume(self) -> None:
        with self.lock:
            self.suspended = False
            self.ignore_until = time.monotonic() + 3


def minecraft_world_directories(saves_directory: Path) -> dict[str, Path]:
    if not saves_directory.is_dir():
        return {}
    return {
        child.name: child
        for child in saves_directory.iterdir()
        if child.is_dir()
        and (child / "level.dat").is_file()
    }


def minecraft_world_name(game: str) -> str | None:
    if not game.startswith(MINECRAFT_WORLD_PREFIX):
        return None
    name = game[len(MINECRAFT_WORLD_PREFIX):]
    if (
        not name
        or name in (".", "..")
        or Path(name).name != name
        or any(character in name for character in '<>:"/\\|?*')
        or any(ord(character) < 32 for character in name)
        or name.rstrip(" .") != name
        or name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
    ):
        return None
    return name


def game_is_running(
    game: str,
    processes: list[dict[str, object]],
    profiles: tuple[GameProfile, ...],
) -> bool:
    profile_name = "Minecraft" if game.startswith(MINECRAFT_WORLD_PREFIX) else game
    profile = next((item for item in profiles if item.name == profile_name), None)
    if profile is None:
        return False
    for process in processes:
        process_names = {
            Path(str(value)).name.casefold()
            for value in (process.get("name"), process.get("exe"))
            if value
        }
        if not process_names.intersection(profile.process_names):
            continue
        raw_command_line = process.get("cmdline") or []
        command_line = (
            " ".join(str(part) for part in raw_command_line)
            if isinstance(raw_command_line, (list, tuple))
            else str(raw_command_line)
        ).casefold()
        if not profile.process_markers or any(
            marker in command_line for marker in profile.process_markers
        ):
            return True
    return False


def active_game_processes() -> list[dict[str, object]]:
    processes = []
    for process in psutil.process_iter(
        attrs=["name", "exe", "cmdline"],
        ad_value=None,
    ):
        try:
            processes.append(process.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


def config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "SaveSync" / "config.json"


def create_archive(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Папка сохранений не найдена")
    file_count = 0
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in source.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            resolved = item.resolve(strict=True)
            if not resolved.is_relative_to(source):
                continue
            archive.write(item, item.relative_to(source).as_posix())
            file_count += 1
    if file_count == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("В выбранной папке нет файлов для архивации")


def safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve(strict=True)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("В архиве слишком много файлов")

        total_size = 0
        prepared: list[tuple[zipfile.ZipInfo, Path]] = []
        seen: set[str] = set()
        for member in members:
            name = member.filename.replace("\\", "/")
            parts = name.split("/")
            if member.is_dir() and parts[-1:] == [""]:
                parts.pop()
            if (
                not name
                or name.startswith("/")
                or not parts
                or any(part in ("", ".", "..") for part in parts)
                or any(":" in part for part in parts)
                or any(part.rstrip(" .") != part for part in parts)
                or any(part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES for part in parts)
                or "\x00" in name
            ):
                raise ValueError("Архив содержит небезопасный путь")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("Архив содержит символическую ссылку")
            key = "/".join(part.casefold() for part in parts)
            if key in seen:
                raise ValueError("Архив содержит повторяющиеся пути")
            seen.add(key)
            total_size += member.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise ValueError("Распакованный архив превышает лимит 10 ГБ")
            target = root.joinpath(*parts)
            if not target.resolve(strict=False).is_relative_to(root):
                raise ValueError("Архив выходит за пределы папки сохранений")
            prepared.append((member, target))

        for member, target in prepared:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=CHUNK_SIZE)


class SaveSyncAPI:
    def __init__(
        self,
        base_url: str,
        room: str,
    ):
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Укажите корректный адрес сервера (http:// или https://)")
        if parsed.query or parsed.fragment:
            raise ValueError("Адрес сервера не должен содержать query или fragment")
        if not room:
            raise ValueError("Укажите код доступа")
        self.base_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.room = urllib.parse.quote(room, safe="")

    @staticmethod
    def create_room(
        base_url: str,
        name: str,
    ) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Укажите корректный адрес сервера (http:// или https://)")
        if parsed.query or parsed.fragment:
            raise ValueError("Адрес сервера не должен содержать query или fragment")
        payload = json.dumps({"name": name}).encode("utf-8")
        url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/api/rooms", "", "")
        )
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with open_url(
                request,
                timeout=30,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"Сервер ответил {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Не удалось связаться с сервером: {exc}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("code"), str):
            raise RuntimeError("Сервер вернул неожиданный код комнаты")
        return result

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, path: str) -> urllib.request.Request:
        return urllib.request.Request(self._url(path))

    def list_saves(self, game: str) -> list[dict[str, str]]:
        query = urllib.parse.urlencode({"game": game})
        request = self._request(f"/api/rooms/{self.room}/saves?{query}")
        try:
            with open_url(
                request,
                timeout=30,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"Сервер ответил {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Не удалось связаться с сервером: {exc}") from exc
        if not isinstance(result, list):
            raise RuntimeError("Сервер вернул неожиданный список сохранений")
        return result

    def room_info(self) -> dict[str, str]:
        request = self._request(f"/api/rooms/{self.room}")
        try:
            with open_url(
                request,
                timeout=30,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"Сервер ответил {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Не удалось связаться с сервером: {exc}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("name"), str):
            raise RuntimeError("Сервер вернул неожиданные данные комнаты")
        return result

    def upload(
        self,
        game: str,
        uploaded_by: str,
        archive_path: Path,
        max_saves: int | None = None,
        save_name: str | None = None,
    ) -> None:
        boundary = f"----SaveSync{uuid.uuid4().hex}"
        fields = {"game": game, "uploaded_by": uploaded_by}
        if max_saves is not None:
            fields["max_saves"] = str(max_saves)
        if save_name is not None:
            fields["save_name"] = save_name
        parts = []
        for name, value in fields.items():
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        file_header = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="archive"; filename="save.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode("ascii")
        footer = f"\r\n--{boundary}--\r\n".encode("ascii")
        file_size = archive_path.stat().st_size
        content_length = sum(map(len, parts)) + len(file_header) + file_size + len(footer)
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.netloc, timeout=60)
        else:
            connection = http.client.HTTPConnection(parsed.netloc, timeout=60)
        path = f"{parsed.path.rstrip('/')}/api/rooms/{self.room}/saves"
        try:
            connection.putrequest("POST", path)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            for part in parts:
                connection.send(part)
            connection.send(file_header)
            with archive_path.open("rb") as archive:
                while chunk := archive.read(CHUNK_SIZE):
                    connection.send(chunk)
            connection.send(footer)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
            if response.status not in (200, 201):
                raise RuntimeError(f"Сервер ответил {response.status}: {response_body}")
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise RuntimeError(f"Не удалось загрузить сохранение: {exc}") from exc
        finally:
            connection.close()

    def download(
        self,
        save_id: str,
        destination: Path,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        path = (
            f"/api/rooms/{self.room}/saves/"
            f"{urllib.parse.quote(save_id, safe='')}/download"
        )
        request = self._request(path)
        try:
            with open_url(
                request,
                timeout=60,
            ) as response:
                try:
                    total = int(response.headers.get("Content-Length", ""))
                except (TypeError, ValueError):
                    total = None
                received = 0
                with destination.open("wb") as output:
                    while chunk := response.read(CHUNK_SIZE):
                        output.write(chunk)
                        received += len(chunk)
                        if progress is not None:
                            progress(received, total)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"Сервер ответил {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Не удалось скачать сохранение: {exc}") from exc


class SaveSyncWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SaveSync — общие сохранения")
        self.root.geometry("860x560")
        self.root.minsize(720, 480)
        self.events = Queue()
        self.entries: dict[str, dict[str, str]] = {}
        self.auto_enabled = tk.BooleanVar(value=True)
        self.monitored: dict[str, tuple[Path, Observer, SaveChangeHandler]] = {}
        self.inactive_since: dict[str, float] = {}
        self.suppressed_games: set[str] = set()
        self.custom_games: list[dict[str, str]] = []
        self.minecraft_worlds: dict[str, Path] = {}
        self.settings_window = None
        self.settings_status = tk.StringVar()
        self.host_server: EmbeddedHostServer | None = None
        self._local_sync_lock = threading.Lock()
        self._auto_upload_lock = threading.Lock()
        self._auto_uploading: set[str] = set()
        self._auto_upload_pending: dict[str, Path] = {}
        self._auto_upload_last_at: dict[str, float] = {}
        self._auto_upload_timers: dict[str, threading.Timer] = {}
        self._update_in_progress = False
        self.values = {
            "mode": tk.StringVar(value="client"),
            "server": tk.StringVar(value="http://127.0.0.1:8000"),
            "room": tk.StringVar(),
            "room_name": tk.StringVar(),
            "host_storage_dir": tk.StringVar(
                value=str(config_path().parent / "host_data")
            ),
            "host_port": tk.StringVar(value=str(DEFAULT_HOST_PORT)),
            "local_save_dir": tk.StringVar(
                value=str(Path.home() / "SaveSyncReceived")
            ),
            "game": tk.StringVar(value=ALL_GAMES),
            "folder": tk.StringVar(),
            "player": tk.StringVar(value=os.environ.get("USERNAME", "Игрок")),
            "auto_save_interval_minutes": tk.StringVar(
                value=str(DEFAULT_AUTO_SAVE_INTERVAL_MINUTES)
            ),
            "max_saves_per_game": tk.StringVar(value=str(DEFAULT_MAX_SAVES_PER_GAME)),
        }
        self.host_allow_uploads = tk.BooleanVar(value=True)
        self.local_sync_enabled = tk.BooleanVar(value=False)
        self._download_indeterminate = False
        self._configure_styles()
        self._build_ui()
        self._load_config()
        self._update_mode_controls()
        self.root.after(100, self._process_events)
        self.root.after(500, self._scan_active_games)
        self.root.after(LOCAL_SAVE_SYNC_INTERVAL_MS, self._schedule_local_sync)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)

        header = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        title_block = ttk.Frame(header)
        title_block.grid(row=0, column=0, sticky="w")
        ttk.Label(title_block, text="SaveSync", style="Title.TLabel").pack(
            side="left"
        )
        ttk.Label(
            title_block,
            text="общие сохранения",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(8, 0), pady=(5, 0))

        mode_controls = ttk.Frame(header)
        mode_controls.grid(row=0, column=1, sticky="e")
        ttk.Radiobutton(
            mode_controls,
            text="Клиент",
            variable=self.values["mode"],
            value="client",
            command=self._mode_changed,
            style="Mode.TRadiobutton",
        ).pack(side="left", padx=(0, 3))
        ttk.Radiobutton(
            mode_controls,
            text="Локальный сервер",
            variable=self.values["mode"],
            value="host",
            command=self._mode_changed,
            style="Mode.TRadiobutton",
        ).pack(side="left", padx=(0, 6))
        self.host_toggle_button = ttk.Button(
            mode_controls,
            text="Запустить сервер",
            command=self._toggle_host_server,
            style="Primary.TButton",
        )
        self.host_toggle_button.pack(side="left")
        self.create_room_button = ttk.Button(
            mode_controls,
            text="Создать комнату",
            command=self._create_room_from_main,
        )
        self.create_room_button.pack(side="left", padx=(5, 0))
        self.local_sync_checkbox = ttk.Checkbutton(
            mode_controls,
            text="Локальные копии",
            variable=self.local_sync_enabled,
            command=self._toggle_local_sync,
        )
        self.local_sync_checkbox.pack(side="left", padx=(6, 0))

        connection_frame = ttk.LabelFrame(
            self.root,
            text="Подключение",
            style="Card.TLabelframe",
            padding=(10, 5),
        )
        connection_frame.grid(
            row=1, column=0, sticky="ew", padx=14, pady=(0, 6)
        )
        connection_frame.columnconfigure(0, weight=3, uniform="connect")
        connection_frame.columnconfigure(1, weight=2, uniform="connect")
        connection_frame.columnconfigure(2, weight=1, uniform="connect")
        for column, (label, key) in enumerate(
            (
                ("Адрес сервера", "server"),
                ("Код доступа", "room"),
                ("Имя игрока", "player"),
            )
        ):
            field = ttk.Frame(connection_frame, style="Card.TFrame")
            field.grid(row=0, column=column, sticky="ew", padx=(0, 8))
            field.columnconfigure(0, weight=1)
            ttk.Label(field, text=label, style="Field.TLabel").grid(
                row=0, column=0, sticky="w", pady=(0, 2)
            )
            ttk.Entry(field, textvariable=self.values[key]).grid(
                row=1, column=0, sticky="ew"
            )

        game_frame = ttk.LabelFrame(
            self.root,
            text="Сейв",
            style="Card.TLabelframe",
            padding=(10, 5),
        )
        game_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
        game_frame.columnconfigure(0, weight=2, uniform="save")
        game_frame.columnconfigure(1, weight=3, uniform="save")
        game_field = ttk.Frame(game_frame, style="Card.TFrame")
        game_field.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(game_field, text="Игра или мир", style="Field.TLabel").pack(
            anchor="w", pady=(0, 2)
        )
        self.game_dropdown = ttk.Combobox(
            game_field,
            textvariable=self.values["game"],
            values=self._game_choices(),
            state="readonly",
        )
        self.game_dropdown.pack(fill="x")
        folder_field = ttk.Frame(game_frame, style="Card.TFrame")
        folder_field.grid(row=0, column=1, sticky="ew")
        folder_field.columnconfigure(0, weight=1)
        ttk.Label(
            folder_field,
            text="Папка сейвов (если нужно указать вручную)",
            style="Field.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        ttk.Entry(folder_field, textvariable=self.values["folder"]).grid(
            row=1, column=0, sticky="ew", padx=(0, 6)
        )
        ttk.Button(
            folder_field,
            text="Обзор…",
            command=self._choose_folder,
        ).grid(row=1, column=1, sticky="ew")

        actions = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        left_actions = ttk.Frame(actions)
        left_actions.grid(row=0, column=0, sticky="w")
        ttk.Button(
            left_actions,
            text="Обновить",
            command=self._refresh,
        ).pack(side="left")
        ttk.Button(
            left_actions,
            text="Загрузить сейв",
            command=self._upload,
            style="Primary.TButton",
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            left_actions,
            text="Скачать и восстановить",
            command=self._download,
        ).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            actions,
            text="Автоотправка",
            variable=self.auto_enabled,
            command=self._toggle_auto_monitoring,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(
            actions,
            text="Настройки",
            command=self._open_settings,
        ).grid(row=0, column=2, sticky="e", padx=(8, 0))

        list_frame = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        list_frame.grid(row=4, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("game", "save_name", "uploaded_by", "created_at")
        self.table = ttk.Treeview(
            list_frame,
            columns=columns,
            show="headings",
            height=6,
            style="Saves.Treeview",
        )
        self.table.heading("game", text="Игра")
        self.table.heading("save_name", text="Сохранение")
        self.table.heading("uploaded_by", text="Загрузил")
        self.table.heading("created_at", text="Время загрузки (UTC)")
        self.table.column("game", width=150, minwidth=100)
        self.table.column("save_name", width=180, minwidth=110)
        self.table.column("uploaded_by", width=130, minwidth=90)
        self.table.column("created_at", width=260, minwidth=160)
        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.table.yview,
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        status_frame = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        status_frame.grid(row=5, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Укажите адрес сервера и код доступа")
        ttk.Label(status_frame, textvariable=self.status, style="Status.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.download_progress = ttk.Progressbar(
            status_frame,
            mode="determinate",
            maximum=100,
            length=140,
        )
        self.download_progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.download_progress.grid_remove()
        self.monitor_status = tk.StringVar(value="Ищу запущенные игры…")
        ttk.Label(
            status_frame,
            textvariable=self.monitor_status,
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.root.configure(background="#f3f5f9")
        self.root.option_add("*Font", ("Segoe UI", 9))
        style.configure("TFrame", background="#f3f5f9")
        style.configure("Card.TLabelframe", background="#ffffff", padding=7)
        style.configure("Card.TFrame", background="#ffffff")
        style.configure(
            "Card.TLabelframe.Label",
            background="#ffffff",
            foreground="#334155",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure("TLabel", background="#f3f5f9", foreground="#243247")
        style.configure(
            "Field.TLabel",
            background="#ffffff",
            foreground="#64748b",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Title.TLabel",
            background="#f3f5f9",
            foreground="#172033",
            font=("Segoe UI", 17, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#f3f5f9",
            foreground="#64748b",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Status.TLabel",
            background="#f3f5f9",
            foreground="#334155",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure("TButton", padding=(9, 5))
        style.configure(
            "Primary.TButton",
            background="#2563eb",
            foreground="#ffffff",
            padding=(10, 5),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1d4ed8"), ("disabled", "#94a3b8")],
            foreground=[("disabled", "#f8fafc")],
        )
        style.configure("Mode.TRadiobutton", background="#f3f5f9", padding=(4, 5))
        style.configure("TEntry", padding=(5, 4), fieldbackground="#ffffff")
        style.configure("TCombobox", padding=(5, 4), fieldbackground="#ffffff")
        style.configure("TNotebook", background="#f3f5f9", borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            padding=(12, 6),
            background="#e9eef5",
            foreground="#475569",
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff")],
            foreground=[("selected", "#1d4ed8")],
        )
        style.configure(
            "Saves.Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground="#243247",
            rowheight=25,
            borderwidth=0,
        )
        style.configure(
            "Saves.Treeview.Heading",
            background="#e9eef5",
            foreground="#334155",
            font=("Segoe UI", 9, "bold"),
            padding=(7, 5),
        )
        style.map(
            "Saves.Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", "#1e3a8a")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#e2e8f0",
            background="#2563eb",
            borderwidth=0,
        )

    def _game_profiles(self) -> tuple[GameProfile, ...]:
        return game_profiles_with_custom(self.custom_games)

    def _game_choices(self) -> tuple[str, ...]:
        available = {
            entry["game"]
            for entry in self.entries.values()
            if minecraft_world_name(entry.get("game", "")) is not None
        }
        available.update(self.minecraft_worlds)
        return (
            ALL_GAMES,
            *(profile.name for profile in self._game_profiles()),
            *sorted(available),
        )

    def _mode_changed(self) -> None:
        if self.values["mode"].get() != "host" and self.host_server is not None:
            try:
                self._stop_host_server()
            except Exception as exc:
                self.values["mode"].set("host")
                messagebox.showerror("Сервер", str(exc), parent=self.root)
                return
        self._update_mode_controls()
        try:
            self._save_config()
        except OSError as exc:
            messagebox.showerror("Настройки", str(exc), parent=self.root)

    def _update_mode_controls(self) -> None:
        is_host = self.values["mode"].get() == "host"
        if is_host:
            self.local_sync_checkbox.pack_forget()
            self.host_toggle_button.pack(side="left")
            self.create_room_button.pack(side="left", padx=(6, 8))
            self.host_toggle_button.configure(
                text="Остановить сервер" if self.host_server is not None
                else "Запустить сервер"
            )
        else:
            self.host_toggle_button.pack_forget()
            self.create_room_button.pack_forget()
            self.local_sync_checkbox.pack(side="left", padx=8)

    def _host_parameters(self) -> tuple[Path, int]:
        directory_value = self.values["host_storage_dir"].get().strip()
        if not directory_value:
            raise ValueError("Укажите папку хранилища сервера.")
        try:
            port = int(self.values["host_port"].get())
        except ValueError as exc:
            raise ValueError("Порт должен быть целым числом от 1 до 65535.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Порт должен быть от 1 до 65535.")
        return Path(directory_value).expanduser(), port

    @staticmethod
    def _lan_address() -> str:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                return address
        except OSError:
            pass
        finally:
            probe.close()
        return "127.0.0.1"

    def _start_host_server(self) -> None:
        if self.host_server is not None:
            return
        storage_root, port = self._host_parameters()
        storage_root.mkdir(parents=True, exist_ok=True)
        server = EmbeddedHostServer(
            storage_root,
            port,
            self.host_allow_uploads.get(),
        )
        server.start()
        try:
            lan_address = self._lan_address()
            connection_url = local_server_url(lan_address, port)
        except Exception:
            server.stop()
            raise
        self.host_server = server
        self._update_mode_controls()
        self.values["server"].set(connection_url)
        try:
            self._save_config()
        except OSError as exc:
            self.status.set(
                f"Сервер запущен, но адрес не удалось сохранить в настройках: {exc}"
            )
            return
        self.status.set(
            f"Локальный сервер запущен: {connection_url}. "
            f"В Porthole выберите процесс {Path(sys.executable).name}, "
            f"TCP-порт {port}, адрес назначения {lan_address}. "
            "Друзьям передайте внешний адрес, показанный Porthole, и код комнаты. "
            "Загрузка сейвов клиентами "
            f"{'разрешена' if self.host_allow_uploads.get() else 'запрещена'}"
        )

    def _ensure_host_server(self) -> None:
        storage_root, port = self._host_parameters()
        current = self.host_server
        if current is not None and (
            current.storage_root != storage_root.resolve()
            or current.port != port
            or current.allow_uploads != self.host_allow_uploads.get()
        ):
            self._stop_host_server()
        if self.host_server is None:
            self._start_host_server()

    def _stop_host_server(self) -> None:
        if self.host_server is None:
            return
        server = self.host_server
        server.stop()
        self.host_server = None
        self._update_mode_controls()
        self.status.set("Локальный сервер остановлен")

    def _toggle_host_server(self) -> None:
        if self.values["mode"].get() != "host":
            return
        try:
            if self.host_server is None:
                self._save_config()
                self._start_host_server()
            else:
                self._stop_host_server()
        except Exception as exc:
            messagebox.showerror("Сервер", str(exc), parent=self.root)

    def _create_room_from_main(self) -> None:
        if self.values["mode"].get() != "host":
            return
        try:
            self._save_config()
            self._ensure_host_server()
        except Exception as exc:
            messagebox.showerror("Сервер", str(exc), parent=self.root)
            return
        name = simpledialog.askstring(
            "Новая комната",
            "Название комнаты:",
            parent=self.root,
        )
        if name is None:
            return
        name = name.strip()
        if not name or len(name) > 60:
            messagebox.showerror(
                "Новая комната",
                "Название комнаты должно содержать от 1 до 60 символов.",
                parent=self.root,
            )
            return
        _, port = self._host_parameters()
        if self.host_server is None:
            messagebox.showerror(
                "Сервер",
                "Сервер не запущен.",
                parent=self.root,
            )
            return
        self.values["room_name"].set(name)
        connection_url = self.values["server"].get()
        self.values["server"].set(connection_url)
        self._create_room_request(
            f"http://127.0.0.1:{port}",
            name,
        )

    def _choose_host_storage_directory(self) -> None:
        directory = filedialog.askdirectory(
            parent=self.settings_window or self.root,
            title="Выберите папку для хранения сейвов сервера",
        )
        if directory:
            self.values["host_storage_dir"].set(directory)

    def _choose_local_save_directory(self) -> None:
        directory = filedialog.askdirectory(
            parent=self.settings_window or self.root,
            title="Выберите папку для локальных копий",
        )
        if directory:
            self.values["local_save_dir"].set(directory)

    def _open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return

        self.root.update_idletasks()
        width, height = 860, 620
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        x = max(0, min(x, screen_width - width))
        y = max(0, min(y, screen_height - height))

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("Настройки SaveSync")
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(520, 500)
        window.transient(self.root)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        window.protocol("WM_DELETE_WINDOW", self._close_settings)

        notebook = ttk.Notebook(window)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        connection_page = ttk.Frame(notebook, padding=6)
        saves_page = ttk.Frame(notebook, padding=6)
        games_page = ttk.Frame(notebook, padding=6)
        github_page = ttk.Frame(notebook, padding=12)
        notebook.add(connection_page, text="Подключение")
        notebook.add(saves_page, text="Сейвы")
        notebook.add(games_page, text="Свои игры")
        notebook.add(github_page, text="GitHub")

        for column in range(2):
            connection_page.columnconfigure(
                column,
                weight=1,
                uniform="connection",
            )
        host_frame = ttk.LabelFrame(
            connection_page,
            text="Режим сервера / хоста",
            padding=7,
        )
        host_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=4)
        host_frame.columnconfigure(1, weight=1)
        ttk.Label(host_frame, text="Папка хранилища").grid(
            row=0, column=0, sticky="w", pady=4
        )
        ttk.Entry(
            host_frame,
            textvariable=self.values["host_storage_dir"],
        ).grid(row=0, column=1, sticky="ew", padx=(8, 6), pady=4)
        ttk.Button(
            host_frame,
            text="Обзор…",
            command=self._choose_host_storage_directory,
        ).grid(row=0, column=2, pady=4)
        ttk.Label(host_frame, text="Порт").grid(
            row=1, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            host_frame,
            from_=1,
            to=65535,
            textvariable=self.values["host_port"],
            width=10,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)
        porthole_help = tk.StringVar()

        def update_porthole_help(*_args: str) -> None:
            port = self.values["host_port"].get().strip() or str(DEFAULT_HOST_PORT)
            porthole_help.set(
                f"Локальный сервер (HTTP) слушает все сетевые интерфейсы. "
                f"Для Porthole выберите процесс {Path(sys.executable).name}, "
                f"TCP-порт {port}, адрес назначения {self._lan_address()}. "
                "Друзьям передайте внешний адрес из Porthole; код комнаты "
                "передавайте отдельно."
            )

        self.values["host_port"].trace_add("write", update_porthole_help)
        update_porthole_help()
        ttk.Label(
            host_frame,
            textvariable=porthole_help,
            wraplength=340,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 4))
        ttk.Checkbutton(
            host_frame,
            text="Разрешить другим клиентам загружать сейвы на этот ПК",
            variable=self.host_allow_uploads,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(
            host_frame,
            text="Код доступа — единственный секрет комнаты. Не публикуйте его. "
            "HTTP подходит для локальной сети и защищённого туннеля Porthole; "
            "не используйте его напрямую в недоверенной сети.",
            wraplength=330,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))

        room_frame = ttk.LabelFrame(connection_page, text="Комната", padding=7)
        room_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=4)
        room_frame.columnconfigure(1, weight=1)
        ttk.Label(room_frame, text="Название комнаты").grid(
            row=0, column=0, sticky="w", pady=4
        )
        ttk.Entry(room_frame, textvariable=self.values["room_name"]).grid(
            row=0, column=1, sticky="ew", padx=(8, 0), pady=4
        )
        ttk.Label(room_frame, text="Код доступа").grid(
            row=1, column=0, sticky="w", pady=4
        )
        ttk.Entry(room_frame, textvariable=self.values["room"]).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=4
        )
        ttk.Label(
            room_frame,
            text="Участникам комнаты нужны адрес сервера и код доступа. "
            "В режиме хоста используйте внешний адрес, выданный Porthole.",
            wraplength=320,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        room_actions = ttk.Frame(room_frame)
        room_actions.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 2))
        ttk.Button(
            room_actions,
            text="Создать",
            command=self._create_room_from_settings,
        ).pack(side="left")
        ttk.Button(
            room_actions,
            text="Проверить код",
            command=self._join_room_from_settings,
        ).pack(side="left", padx=8)
        ttk.Label(
            room_frame,
            textvariable=self.settings_status,
            wraplength=420,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        for column in range(2):
            saves_page.columnconfigure(
                column,
                weight=1,
                uniform="save-settings",
            )
        saves_frame = ttk.LabelFrame(saves_page, text="Автосейвы", padding=7)
        saves_frame.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=4)
        saves_frame.columnconfigure(1, weight=1)
        ttk.Label(saves_frame, text="Интервал (минуты)").grid(
            row=0, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            saves_frame,
            from_=MIN_AUTO_SAVE_INTERVAL_MINUTES,
            to=MAX_AUTO_SAVE_INTERVAL_MINUTES,
            textvariable=self.values["auto_save_interval_minutes"],
            width=10,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(saves_frame, text="Сейвов на игрока для каждой игры").grid(
            row=1, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            saves_frame,
            from_=MIN_MAX_SAVES_PER_GAME,
            to=MAX_MAX_SAVES_PER_GAME,
            textvariable=self.values["max_saves_per_game"],
            width=10,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(
            saves_frame,
            text="При превышении лимита сервер удалит самые старые сейвы этого игрока "
            "в выбранной игре и комнате.",
            wraplength=400,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        local_frame = ttk.LabelFrame(
            saves_page,
            text="Локальные копии (режим клиента)",
            padding=7,
        )
        local_frame.grid(row=0, column=1, sticky="new", padx=(6, 0), pady=4)
        local_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            local_frame,
            text="Автоматически сохранять новые сейвы из комнаты на этот ПК",
            variable=self.local_sync_enabled,
            command=self._toggle_local_sync,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(local_frame, text="Папка локальных копий").grid(
            row=1, column=0, sticky="w", pady=4
        )
        ttk.Entry(
            local_frame,
            textvariable=self.values["local_save_dir"],
        ).grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=4)
        ttk.Button(
            local_frame,
            text="Обзор…",
            command=self._choose_local_save_directory,
        ).grid(row=1, column=2, pady=4)
        ttk.Label(
            local_frame,
            text="Копии скачиваются архивами ZIP в подпапки по играм и не распаковываются.",
            wraplength=400,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))

        games_frame = ttk.LabelFrame(
            games_page,
            text="Пользовательские игры",
            padding=7,
        )
        games_frame.pack(fill="both", expand=True, padx=4, pady=4)
        games_frame.columnconfigure(1, weight=1)
        games_frame.rowconfigure(4, weight=1)

        ttk.Label(games_frame, text="Название").grid(
            row=0, column=0, sticky="w", pady=4
        )
        self.custom_game_name = tk.StringVar()
        ttk.Entry(games_frame, textvariable=self.custom_game_name).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4
        )
        ttk.Label(games_frame, text="Папка сохранений").grid(
            row=1, column=0, sticky="w", pady=4
        )
        self.custom_game_directory = tk.StringVar()
        ttk.Entry(games_frame, textvariable=self.custom_game_directory).grid(
            row=1, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            games_frame,
            text="Обзор…",
            command=self._choose_custom_game_directory,
        ).grid(row=1, column=2, pady=4)
        ttk.Label(games_frame, text="Процесс (.exe, необязательно)").grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.custom_game_process = tk.StringVar()
        ttk.Entry(games_frame, textvariable=self.custom_game_process).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4
        )
        ttk.Label(
            games_frame,
            text="Если указать процесс, папка будет отслеживаться, когда игра запущена. "
            "Без процесса игру можно выбрать для ручной загрузки и скачивания.",
            wraplength=700,
        ).grid(row=3, column=0, columnspan=3, sticky="nw", pady=(2, 6))

        columns = ("name", "directory", "process")
        self.custom_game_table = ttk.Treeview(
            games_frame,
            columns=columns,
            show="headings",
            height=5,
            selectmode="browse",
        )
        self.custom_game_table.heading("name", text="Игра")
        self.custom_game_table.heading("directory", text="Папка сейвов")
        self.custom_game_table.heading("process", text="Процесс")
        self.custom_game_table.column("name", width=140)
        self.custom_game_table.column("directory", width=390)
        self.custom_game_table.column("process", width=140)
        self.custom_game_table.grid(
            row=4, column=0, columnspan=3, sticky="nsew", pady=(6, 4)
        )
        game_actions = ttk.Frame(games_frame)
        game_actions.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Button(
            game_actions,
            text="Добавить игру",
            command=self._add_custom_game,
        ).pack(side="left")
        ttk.Button(
            game_actions,
            text="Удалить выбранную",
            command=self._remove_custom_game,
        ).pack(side="left", padx=8)
        self._refresh_custom_game_table()

        github_page.columnconfigure(0, weight=1)
        github_card = ttk.LabelFrame(
            github_page,
            text="SaveSync на GitHub",
            padding=12,
        )
        github_card.grid(row=0, column=0, sticky="new", padx=4, pady=4)
        github_card.columnconfigure(0, weight=1)
        ttk.Label(
            github_card,
            text="Исходный код, выпуски и сообщения об ошибках проекта.",
            wraplength=680,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        github_links = ttk.Frame(github_card)
        github_links.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(
            github_links,
            text="Открыть репозиторий",
            command=lambda: self._open_github_page(GITHUB_REPOSITORY_URL),
        ).pack(side="left")
        ttk.Button(
            github_links,
            text="Открыть выпуски",
            command=lambda: self._open_github_page(GITHUB_RELEASES_URL),
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            github_card,
            text=f"Установленная версия: {APP_VERSION}",
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 4))
        self.github_status = tk.StringVar(
            value="Проверка ищет опубликованные тестовые и стабильные версии."
        )
        ttk.Label(
            github_card,
            textvariable=self.github_status,
            wraplength=680,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.github_progress = ttk.Progressbar(
            github_card,
            mode="determinate",
            maximum=100,
            length=340,
        )
        self.github_progress.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self.github_progress.grid_remove()
        self.github_check_button = ttk.Button(
            github_card,
            text="Проверить и установить обновление",
            command=self._check_for_updates,
            style="Primary.TButton",
            state="disabled" if self._update_in_progress else "normal",
        )
        self.github_check_button.grid(row=5, column=0, sticky="w")
        ttk.Label(
            github_page,
            text="EXE скачивается только из официального релиза SaveSync и "
            "проверяется по SHA-256 перед заменой. Для установки приложение "
            "перезапустится. Запущенный из Python исходный код можно обновить "
            "только вручную.",
            wraplength=720,
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", padx=6, pady=(8, 0))

        footer = ttk.Frame(window, padding=(12, 6, 12, 12))
        footer.grid(row=1, column=0, sticky="ew")
        ttk.Button(
            footer,
            text="Сохранить настройки",
            command=self._save_settings_from_window,
        ).pack(side="right")

    def _open_github_page(self, url: str) -> None:
        try:
            if not webbrowser.open(url):
                raise RuntimeError("Не удалось открыть браузер")
        except Exception as exc:
            messagebox.showerror(
                "GitHub",
                f"Не удалось открыть страницу GitHub: {exc}",
                parent=self.settings_window or self.root,
            )

    def _check_for_updates(self) -> None:
        if self._update_in_progress:
            return
        self._update_in_progress = True
        self.github_check_button.configure(state="disabled")
        self.github_status.set("Проверяю новые выпуски SaveSync на GitHub…")
        self.github_progress.grid_remove()

        def check() -> None:
            try:
                release = check_github_update()
                if release is None:
                    self.events.put(("update-current", APP_VERSION))
                    return
                version = str(release["tag_name"])
                if not getattr(sys, "frozen", False) or os.name != "nt":
                    release_url = (
                        f"{GITHUB_REPOSITORY_URL}/releases/tag/"
                        f"{urllib.parse.quote(version, safe='')}"
                    )
                    self.events.put(("update-manual", (version, release_url)))
                    return
                self.events.put(("update-downloading", version))
                download_path, download_directory = download_update_executable(
                    release,
                    lambda received, total: self.events.put(
                        ("update-progress", (received, total))
                    ),
                )
                self.events.put(
                    (
                        "update-ready",
                        (release, download_path, download_directory),
                    )
                )
            except Exception as exc:
                self.events.put(("update-error", str(exc)))

        threading.Thread(target=check, daemon=True).start()

    def _reset_update_controls(self, status: str) -> None:
        self._update_in_progress = False
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.github_check_button.configure(state="normal")
            self.github_progress.stop()
            self.github_progress.grid_remove()
            self.github_status.set(status)

    def _install_downloaded_update(
        self,
        release: dict[str, object],
        download_path: Path,
        download_directory: Path,
    ) -> None:
        version = str(release["tag_name"])
        confirmed = messagebox.askyesno(
            "Установить обновление",
            f"Найдена версия {version}. Установить её и перезапустить SaveSync?",
            parent=self.settings_window or self.root,
        )
        if not confirmed:
            shutil.rmtree(download_directory, ignore_errors=True)
            self._reset_update_controls("Установка отменена.")
            return

        staged_path: Path | None = None
        updater: subprocess.Popen | None = None
        try:
            target_path = Path(sys.executable).resolve()
            staged_path = stage_update_executable(download_path, target_path)
            updater = start_windows_updater(
                staged_path,
                target_path,
                download_directory,
                os.getpid(),
            )
            self._close()
            if self.root.winfo_exists():
                updater.terminate()
                staged_path.unlink(missing_ok=True)
                shutil.rmtree(download_directory, ignore_errors=True)
                self._reset_update_controls(
                    "Не удалось закрыть сервер. Обновление не установлено."
                )
        except Exception as exc:
            if updater is not None and updater.poll() is None:
                updater.terminate()
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            shutil.rmtree(download_directory, ignore_errors=True)
            self._reset_update_controls(f"Не удалось установить обновление: {exc}")
            messagebox.showerror(
                "Обновление",
                f"Не удалось подготовить установку: {exc}",
                parent=self.settings_window or self.root,
            )

    def _close_settings(self) -> None:
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None

    def _create_room_from_settings(self) -> None:
        name = self.values["room_name"].get().strip()
        if not name:
            self.settings_status.set("Укажите название комнаты.")
            return
        if len(name) > 60:
            self.settings_status.set("Название комнаты должно быть не длиннее 60 символов.")
            return
        if self.values["mode"].get() == "host":
            try:
                self._save_config()
                self._ensure_host_server()
                _, port = self._host_parameters()
                if self.host_server is None:
                    raise RuntimeError("Сервер не запущен")
                base_url = f"http://127.0.0.1:{port}"
            except Exception as exc:
                self.settings_status.set(f"Не удалось запустить сервер: {exc}")
                return
        else:
            base_url = self.values["server"].get()
        self._create_room_request(base_url, name)

    def _create_room_request(
        self,
        base_url: str,
        name: str,
    ) -> None:
        self.settings_status.set("Создаю комнату…")

        def create() -> None:
            try:
                result = SaveSyncAPI.create_room(
                    base_url,
                    name,
                )
                self.events.put(("room-created", result))
            except Exception as exc:
                self.events.put(("room-error", str(exc)))

        threading.Thread(target=create, daemon=True).start()

    def _join_room_from_settings(self) -> None:
        try:
            api = self._api()
        except Exception as exc:
            self.settings_status.set(str(exc))
            return
        self.settings_status.set("Проверяю код доступа…")

        def connect() -> None:
            try:
                room = api.room_info()
                saves = api.list_saves("")
                self.events.put(("room-joined", (room, len(saves))))
            except Exception as exc:
                self.events.put(("room-error", str(exc)))

        threading.Thread(target=connect, daemon=True).start()

    def _choose_custom_game_directory(self) -> None:
        directory = filedialog.askdirectory(
            parent=self.settings_window or self.root,
            title="Выберите папку сохранений игры",
        )
        if directory:
            self.custom_game_directory.set(directory)

    def _refresh_custom_game_table(self) -> None:
        if self.settings_window is None or not self.settings_window.winfo_exists():
            return
        for item in self.custom_game_table.get_children():
            self.custom_game_table.delete(item)
        for index, game in enumerate(self.custom_games):
            self.custom_game_table.insert(
                "",
                "end",
                iid=f"custom-game-{index}",
                values=(
                    game["name"],
                    game["save_directory"],
                    game.get("process_name", ""),
                ),
            )

    def _add_custom_game(self) -> None:
        name = self.custom_game_name.get().strip()
        directory = Path(self.custom_game_directory.get().strip()).expanduser()
        process_name = self.custom_game_process.get().strip()
        if not name:
            messagebox.showerror(
                "Своя игра",
                "Введите название игры.",
                parent=self.settings_window or self.root,
            )
            return
        if not directory.is_dir():
            messagebox.showerror(
                "Своя игра",
                "Выберите существующую папку сохранений.",
                parent=self.settings_window or self.root,
            )
            return
        if process_name and not process_name.casefold().endswith(".exe"):
            messagebox.showerror(
                "Своя игра",
                "Укажите имя исполняемого файла с расширением .exe.",
                parent=self.settings_window or self.root,
            )
            return
        profiles = self._game_profiles()
        if any(profile.name.casefold() == name.casefold() for profile in profiles):
            messagebox.showerror(
                "Своя игра",
                "Игра с таким названием уже есть в списке.",
                parent=self.settings_window or self.root,
            )
            return
        self.custom_games.append(
            {
                "name": name,
                "save_directory": str(directory.resolve()),
                "process_name": Path(process_name).name if process_name else "",
            }
        )
        self.custom_game_name.set("")
        self.custom_game_directory.set("")
        self.custom_game_process.set("")
        self.game_dropdown.configure(values=self._game_choices())
        self._refresh_custom_game_table()
        self._save_settings_from_window(show_confirmation=False)

    def _remove_custom_game(self) -> None:
        selection = self.custom_game_table.selection()
        if not selection:
            return
        index = int(selection[0].removeprefix("custom-game-"))
        self._stop_monitor(self.custom_games[index]["name"])
        del self.custom_games[index]
        self.game_dropdown.configure(values=self._game_choices())
        self._refresh_custom_game_table()
        self._save_settings_from_window(show_confirmation=False)

    def _save_settings_from_window(self, show_confirmation: bool = True) -> None:
        for key, label, minimum, maximum in (
            (
                "auto_save_interval_minutes",
                "Интервал автосейва",
                MIN_AUTO_SAVE_INTERVAL_MINUTES,
                MAX_AUTO_SAVE_INTERVAL_MINUTES,
            ),
            (
                "max_saves_per_game",
                "Количество сейвов",
                MIN_MAX_SAVES_PER_GAME,
                MAX_MAX_SAVES_PER_GAME,
            ),
        ):
            try:
                value = int(self.values[key].get())
            except ValueError:
                self.settings_status.set(f"{label}: введите целое число.")
                return
            if not minimum <= value <= maximum:
                self.settings_status.set(
                    f"{label}: допустимо от {minimum} до {maximum}."
                )
                return
        was_host_running = self.host_server is not None
        if self.values["mode"].get() == "host" or was_host_running:
            try:
                self._host_parameters()
            except ValueError as exc:
                self.settings_status.set(str(exc))
                return
        if self.local_sync_enabled.get() and not self.values["local_save_dir"].get().strip():
            self.settings_status.set("Укажите папку для локальных копий сейвов.")
            return
        try:
            self._save_config()
        except OSError as exc:
            messagebox.showerror(
                "Настройки",
                f"Не удалось сохранить настройки: {exc}",
                parent=self.settings_window or self.root,
            )
            return
        if was_host_running:
            try:
                self._stop_host_server()
                self._start_host_server()
            except Exception as exc:
                self.settings_status.set(f"Настройки сохранены, но сервер не запущен: {exc}")
                return
        if show_confirmation:
            self.settings_status.set("Настройки сохранены.")

    def _load_config(self) -> None:
        path = config_path()
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError("Файл настроек должен содержать JSON-объект")
            custom_games = settings.get("custom_games", [])
            if not isinstance(custom_games, list):
                raise ValueError("Список пользовательских игр должен быть массивом")
            self.custom_games = [
                {
                    "name": profile.name,
                    "save_directory": str(profile.save_directory),
                    "process_name": next(iter(profile.process_names), ""),
                }
                for profile in custom_game_profiles(custom_games)
                if profile.save_directory is not None
            ]
            invalid_game = settings.get("game") not in (None, *self._game_choices())
            auto_enabled = settings.get("auto_enabled")
            if isinstance(auto_enabled, bool):
                self.auto_enabled.set(auto_enabled)
            host_allow_uploads = settings.get("host_allow_uploads")
            if isinstance(host_allow_uploads, bool):
                self.host_allow_uploads.set(host_allow_uploads)
            local_sync_enabled = settings.get("local_sync_enabled")
            if isinstance(local_sync_enabled, bool):
                self.local_sync_enabled.set(local_sync_enabled)
            for key, value in settings.items():
                if key in self.values and isinstance(value, str):
                    if key == "game" and value not in self._game_choices():
                        continue
                    self.values[key].set(value)
            for key, default, minimum, maximum in (
                (
                    "auto_save_interval_minutes",
                    DEFAULT_AUTO_SAVE_INTERVAL_MINUTES,
                    MIN_AUTO_SAVE_INTERVAL_MINUTES,
                    MAX_AUTO_SAVE_INTERVAL_MINUTES,
                ),
                (
                    "max_saves_per_game",
                    DEFAULT_MAX_SAVES_PER_GAME,
                    MIN_MAX_SAVES_PER_GAME,
                    MAX_MAX_SAVES_PER_GAME,
                ),
            ):
                try:
                    value = int(self.values[key].get())
                except ValueError:
                    value = default
                self.values[key].set(str(min(maximum, max(minimum, value))))
            self.game_dropdown.configure(values=self._game_choices())
            if invalid_game:
                messagebox.showwarning(
                    "Выбор игры",
                    "Сохранённая игра больше не входит в список. Выберите игру "
                    "из выпадающего списка.",
                    parent=self.root,
                )
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            messagebox.showwarning(
                "Настройки", f"Не удалось прочитать {path}: {exc}", parent=self.root
            )

    def _save_config(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    **{key: value.get() for key, value in self.values.items()},
                    "custom_games": self.custom_games,
                    "auto_enabled": self.auto_enabled.get(),
                    "host_allow_uploads": self.host_allow_uploads.get(),
                    "local_sync_enabled": self.local_sync_enabled.get(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _api(self) -> SaveSyncAPI:
        self._save_config()
        return SaveSyncAPI(
            self.values["server"].get(),
            self.values["room"].get(),
        )

    def _toggle_local_sync(self) -> None:
        if self.local_sync_enabled.get() and not self.values["local_save_dir"].get().strip():
            messagebox.showerror(
                "Локальные копии",
                "Сначала укажите папку для сохранения сейвов в настройках.",
                parent=self.root,
            )
            self.local_sync_enabled.set(False)
            return
        try:
            self._save_config()
        except OSError as exc:
            messagebox.showerror("Настройки", str(exc), parent=self.root)
            return
        if self.local_sync_enabled.get() and self.values["mode"].get() == "client":
            self._start_local_sync()

    def _schedule_local_sync(self) -> None:
        if self.local_sync_enabled.get() and self.values["mode"].get() == "client":
            self._start_local_sync()
        self.root.after(LOCAL_SAVE_SYNC_INTERVAL_MS, self._schedule_local_sync)

    def _start_local_sync(self) -> None:
        if not self._local_sync_lock.acquire(blocking=False):
            return

        def sync() -> None:
            try:
                api = self._api()
                destination = Path(
                    self.values["local_save_dir"].get()
                ).expanduser()
                count = save_remote_saves_locally(api, destination)
                self.events.put(("local-sync", count))
            except Exception as exc:
                self.events.put(("local-sync-error", str(exc)))
            finally:
                self._local_sync_lock.release()

        threading.Thread(target=sync, daemon=True).start()

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            self.values["folder"].set(folder)

    def _save_folder_for_game(self, game: str) -> Path | None:
        monitored = self.monitored.get(game)
        if monitored is not None:
            return monitored[0]
        world_name = minecraft_world_name(game)
        if world_name is not None:
            local_path = self.minecraft_worlds.get(game)
            if local_path is not None and local_path.is_dir():
                return local_path
            configured_folder = self.values["folder"].get().strip()
            if configured_folder:
                configured_path = Path(configured_folder).expanduser()
                if (
                    configured_path.name.casefold() == world_name.casefold()
                    and (configured_path / "level.dat").is_file()
                ):
                    return configured_path
            profile = next(
                item for item in self._game_profiles() if item.name == "Minecraft"
            )
            candidates = game_save_candidates(profile)
            root = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.is_dir()
                ),
                candidates[-1] if candidates else None,
            )
            return root / world_name if root is not None else None
        if game.startswith(MINECRAFT_WORLD_PREFIX):
            return None
        profile = next((item for item in self._game_profiles() if item.name == game), None)
        if profile is not None and profile.save_location == "custom":
            return profile.save_directory
        if game == "Minecraft":
            configured_folder = self.values["folder"].get().strip()
            if configured_folder:
                configured_path = Path(configured_folder).expanduser()
                if (configured_path / "level.dat").is_file():
                    return configured_path
            worlds = [
                path
                for path in self.minecraft_worlds.values()
                if path.is_dir()
            ]
            return worlds[0] if len(worlds) == 1 else None
        configured_folder = self.values["folder"].get().strip()
        if configured_folder:
            return Path(configured_folder).expanduser()
        if profile is not None:
            return next(
                (
                    candidate
                    for candidate in game_save_candidates(profile)
                    if candidate.is_dir()
                ),
                None,
            )
        return None

    def _stop_monitor(self, game: str) -> None:
        monitored = self.monitored.pop(game, None)
        self.inactive_since.pop(game, None)
        if monitored is None:
            return
        _, observer, handler = monitored
        handler.stop()
        observer.stop()
        observer.join(timeout=3)

    def _toggle_auto_monitoring(self) -> None:
        try:
            self._save_config()
        except OSError as exc:
            messagebox.showerror(
                "Настройки",
                f"Не удалось сохранить настройки автоотправки: {exc}",
                parent=self.root,
            )
        if not self.auto_enabled.get():
            for game in list(self.monitored):
                self._stop_monitor(game)
            with self._auto_upload_lock:
                timers = list(self._auto_upload_timers.values())
                self._auto_upload_timers.clear()
                self._auto_upload_pending.clear()
            for timer in timers:
                timer.cancel()
            self.monitor_status.set("Автоотправка выключена")
            return
        self.monitor_status.set("Ищу запущенные игры…")
        self._scan_active_games()

    def _scan_active_games(self) -> None:
        if not self.auto_enabled.get():
            return
        try:
            active_roots = find_active_game_saves(
                active_game_processes(),
                profiles=self._game_profiles(),
            )
        except Exception as exc:
            self.monitor_status.set(f"Не удалось проверить запущенные игры: {exc}")
            self.root.after(5000, self._scan_active_games)
            return

        active: dict[str, Path | None] = {}
        missing_paths = []
        discovered_worlds: dict[str, Path] = {}
        for game, save_path in active_roots.items():
            if game != "Minecraft":
                active[game] = save_path
                continue
            if save_path is None:
                missing_paths.append("Minecraft (не найдена папка saves)")
                continue
            worlds = minecraft_world_directories(save_path)
            if not worlds:
                missing_paths.append("Minecraft (не найдены миры с level.dat)")
                continue
            for world_name, world_path in worlds.items():
                world_label = f"{MINECRAFT_WORLD_PREFIX}{world_name}"
                active[world_label] = world_path
                discovered_worlds[world_label] = world_path

        for game, path in self.minecraft_worlds.items():
            if path.is_dir():
                discovered_worlds.setdefault(game, path)
        minecraft_profile = next(
            profile
            for profile in self._game_profiles()
            if profile.name == "Minecraft"
        )
        for root in game_save_candidates(minecraft_profile):
            for world_name, world_path in minecraft_world_directories(root).items():
                discovered_worlds.setdefault(
                    f"{MINECRAFT_WORLD_PREFIX}{world_name}",
                    world_path,
                )
        self.minecraft_worlds = discovered_worlds
        self.game_dropdown.configure(values=self._game_choices())

        now = time.monotonic()
        for game in list(self.monitored):
            if game not in active:
                inactive_since = self.inactive_since.setdefault(game, now)
                if now - inactive_since >= GAME_EXIT_GRACE_SECONDS:
                    self._stop_monitor(game)
                continue
            self.inactive_since.pop(game, None)
            save_path = active[game]
            if save_path is None or self.monitored[game][0] != save_path:
                self._stop_monitor(game)

        monitor_errors = []
        for game, save_path in active.items():
            if save_path is None:
                missing_paths.append(game)
                continue
            if game in self.monitored:
                continue
            handler = SaveChangeHandler(
                lambda game=game, path=save_path: self.events.put(
                    ("auto-change", (game, path))
                )
            )
            try:
                observer = Observer()
                observer.schedule(handler, str(save_path), recursive=True)
                observer.start()
                self.monitored[game] = (save_path, observer, handler)
            except (OSError, RuntimeError) as exc:
                handler.stop()
                monitor_errors.append(f"{game}: {exc}")

        status_parts = []
        if self.monitored:
            watched = ", ".join(
                f"{game} ({path})" for game, (path, _, _) in self.monitored.items()
            )
            status_parts.append(f"Отслеживаются изменения: {watched}")
        if missing_paths:
            status_parts.append(
                "Не найдены папки сейвов: " + ", ".join(missing_paths)
            )
        if monitor_errors:
            status_parts.append("Ошибки отслеживания: " + "; ".join(monitor_errors))
        if not active_roots:
            status_parts.append("Поддерживаемые игры не запущены")
        self.monitor_status.set("; ".join(status_parts))
        self.root.after(5000, self._scan_active_games)

    def _close(self) -> None:
        try:
            self._stop_host_server()
        except Exception as exc:
            messagebox.showerror("Сервер", str(exc), parent=self.root)
            return
        for game in list(self.monitored):
            self._stop_monitor(game)
        with self._auto_upload_lock:
            timers = list(self._auto_upload_timers.values())
            self._auto_upload_timers.clear()
        for timer in timers:
            timer.cancel()
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.root.destroy()

    def _queue_auto_upload(self, game: str, folder: Path) -> None:
        try:
            interval = int(self.values["auto_save_interval_minutes"].get())
            if not MIN_AUTO_SAVE_INTERVAL_MINUTES <= interval <= MAX_AUTO_SAVE_INTERVAL_MINUTES:
                raise ValueError("Интервал автосейва вне допустимого диапазона")
        except ValueError as exc:
            self.monitor_status.set(f"Не удалось запланировать автосейв: {exc}")
            return
        with self._auto_upload_lock:
            self._auto_upload_pending[game] = folder
            if game in self._auto_uploading or game in self._auto_upload_timers:
                return
            last_uploaded = self._auto_upload_last_at.get(game)
            delay = auto_upload_delay(
                interval,
                last_uploaded,
                time.monotonic(),
            )
            if delay:
                timer = threading.Timer(
                    delay,
                    lambda: self.events.put(("auto-due", game)),
                )
                timer.daemon = True
                self._auto_upload_timers[game] = timer
                timer.start()
                return
        self._start_auto_upload(game)

    def _start_auto_upload(self, game: str) -> None:
        with self._auto_upload_lock:
            timer = self._auto_upload_timers.pop(game, None)
            folder = self._auto_upload_pending.pop(game, None)
            if folder is None or game in self._auto_uploading:
                return
            self._auto_uploading.add(game)
        if timer is not None:
            timer.cancel()
        try:
            api = self._api()
            player = self.values["player"].get().strip() or "Unknown"
            max_saves = int(self.values["max_saves_per_game"].get())
            if not MIN_MAX_SAVES_PER_GAME <= max_saves <= MAX_MAX_SAVES_PER_GAME:
                raise ValueError("Количество сейвов вне допустимого диапазона")
        except Exception as exc:
            with self._auto_upload_lock:
                self._auto_uploading.discard(game)
            self.events.put(("auto-error", f"{game}: {exc}"))
            return

        def upload_latest_changes(current_folder: Path) -> None:
            try:
                with tempfile.TemporaryDirectory(prefix="savesync-auto-") as temporary:
                    archive_path = Path(temporary) / "save.zip"
                    create_archive(current_folder, archive_path)
                    api.upload(
                        game,
                        player,
                        archive_path,
                        max_saves=max_saves,
                        save_name=folder.name,
                    )
            except Exception as exc:
                with self._auto_upload_lock:
                    self._auto_uploading.discard(game)
                    self._auto_upload_pending.pop(game, None)
                self.events.put(("auto-error", f"{game}: {exc}"))
                return

            with self._auto_upload_lock:
                self._auto_upload_last_at[game] = time.monotonic()
                pending_folder = self._auto_upload_pending.pop(game, None)
                self._auto_uploading.discard(game)
            self.events.put(("auto-uploaded", game))
            if pending_folder is not None:
                self.events.put(("auto-change", (game, pending_folder)))

        threading.Thread(
            target=upload_latest_changes,
            args=(folder,),
            daemon=True,
        ).start()

    def _run(self, status: str, action) -> None:
        self.status.set(status)

        def worker() -> None:
            try:
                result = action()
                self.events.put(("success", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh(self) -> None:
        try:
            api = self._api()
            selected_game = self.values["game"].get().strip()
            if selected_game not in self._game_choices():
                messagebox.showerror("Выберите игру", "Выберите игру из списка.")
                return
            game_filter = "" if selected_game == ALL_GAMES else selected_game
            self._run(
                "Загружаю список…",
                lambda: api.list_saves(game_filter),
            )
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self.root)

    def _upload(self) -> None:
        game = self.values["game"].get().strip()
        if game == ALL_GAMES:
            active_games = list(self.monitored)
            if len(active_games) != 1:
                messagebox.showerror(
                    "Выберите игру",
                    "Для загрузки выберите конкретную игру из списка.",
                )
                return
            game = active_games[0]
        if game not in self._game_choices():
            messagebox.showerror("Выберите игру", "Выберите игру из списка.")
            return
        folder = self._save_folder_for_game(game)
        player = self.values["player"].get().strip() or "Unknown"
        if folder is None or not folder.is_dir():
            messagebox.showerror("Папка не найдена", "Выберите папку сохранений игры.")
            return
        if game_is_running(game, active_game_processes(), self._game_profiles()):
            messagebox.showerror(
                "Закройте игру",
                f"Закройте {game} перед ручной загрузкой сейва. "
                "Автоматические снимки продолжают работать во время игры.",
                parent=self.root,
            )
            return
        try:
            api = self._api()
            max_saves = int(self.values["max_saves_per_game"].get())
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self.root)
            return

        def upload() -> str:
            if game_is_running(game, active_game_processes(), self._game_profiles()):
                raise RuntimeError(
                    f"Закройте {game} перед ручной загрузкой сейва."
                )
            with tempfile.TemporaryDirectory(prefix="savesync-") as temporary:
                archive_path = Path(temporary) / "save.zip"
                create_archive(folder, archive_path)
                api.upload(
                    game,
                    player,
                    archive_path,
                    max_saves=max_saves,
                    save_name=folder.name,
                )
            return "Сейв загружен. Обновляю список…"

        def upload_and_refresh():
            upload()
            return api.list_saves(game)

        self._run("Упаковываю и загружаю сейв…", upload_and_refresh)

    def _download(self) -> None:
        selection = self.table.selection()
        if not selection:
            messagebox.showinfo("Сохранение", "Сначала выберите сейв из списка.")
            return
        save = self.entries[selection[0]]
        game = save["game"]
        if (
            game not in self._game_choices()
            or (
                game not in {profile.name for profile in self._game_profiles()}
                and minecraft_world_name(game) is None
            )
        ):
            messagebox.showerror(
                "Неизвестная игра",
                f"Игра «{game}» отсутствует в списке поддерживаемых.",
                parent=self.root,
            )
            return
        folder = self._save_folder_for_game(save["game"])
        is_minecraft_world = minecraft_world_name(game) is not None
        if folder is None or (not is_minecraft_world and not folder.is_dir()):
            messagebox.showerror("Папка не найдена", "Выберите папку сохранений игры.")
            return
        if game_is_running(game, active_game_processes(), self._game_profiles()):
            messagebox.showerror(
                "Закройте игру",
                f"Закройте {game} перед восстановлением, затем повторите скачивание.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Подтвердите замену",
            "Файлы из архива будут записаны в папку сохранений и могут заменить "
            "одноимённые файлы. Перед восстановлением закройте игру. Продолжить?",
            parent=self.root,
        ):
            return
        with self._auto_upload_lock:
            upload_in_progress = game in self._auto_uploading
        if upload_in_progress:
            messagebox.showinfo(
                "Автосохранение",
                "Дождитесь завершения текущей автоотправки и повторите скачивание.",
                parent=self.root,
            )
            return
        try:
            api = self._api()
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self.root)
            return
        monitored = self.monitored.get(game)
        if monitored is not None:
            monitored[2].suspend()
        self.suppressed_games.add(game)
        self.download_progress.configure(
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.download_progress.grid()
        self._download_indeterminate = False

        def report_progress(received: int, total: int | None) -> None:
            self.events.put(("download-progress", (received, total)))

        def download() -> str:
            try:
                with tempfile.TemporaryDirectory(prefix="savesync-") as temporary:
                    archive_path = Path(temporary) / "download.zip"
                    api.download(save["id"], archive_path, report_progress)
                    if game_is_running(
                        game,
                        active_game_processes(),
                        self._game_profiles(),
                    ):
                        raise RuntimeError(
                            f"Закройте {game} перед восстановлением сейва."
                        )
                    self.events.put(("download-phase", "Распаковываю архив…"))
                    safe_extract(archive_path, folder)
                return "Сохранение скачано и распаковано."
            finally:
                if monitored is not None:
                    monitored[2].resume()
                self.events.put(("monitor-resumed", game))

        self._run("Скачиваю и распаковываю сейв…", download)

    def _process_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "error":
                    self.status.set("Операция завершилась с ошибкой")
                    self.download_progress.stop()
                    self.download_progress.grid_remove()
                    self._download_indeterminate = False
                    messagebox.showerror("Ошибка", str(payload), parent=self.root)
                elif event == "room-created":
                    self.values["room"].set(payload["code"])
                    self.values["room_name"].set(payload["name"])
                    try:
                        self._save_config()
                    except OSError as exc:
                        self.settings_status.set(
                            f"Комната создана, но настройки не сохранились: {exc}"
                        )
                        continue
                    self.settings_status.set(
                        f"Комната «{payload['name']}» создана. "
                        f"Код доступа: {payload['code']}. Передайте его только "
                        "участникам комнаты."
                    )
                    if self.values["mode"].get() == "host":
                        self.status.set(
                            f"Комната «{payload['name']}»: сервер "
                            f"{self.values['server'].get()}, код доступа "
                            f"{payload['code']}. Передайте друзьям внешний адрес "
                            "Porthole и код доступа."
                        )
                elif event == "room-joined":
                    room, save_count = payload
                    self.values["room_name"].set(room["name"])
                    self.settings_status.set(
                        f"Подключены к комнате «{room['name']}». "
                        f"Доступно сохранений: {save_count}."
                    )
                    try:
                        self._save_config()
                    except OSError as exc:
                        self.settings_status.set(
                            f"Подключились, но настройки не сохранились: {exc}"
                        )
                elif event == "room-error":
                    self.settings_status.set(f"Ошибка комнаты: {payload}")
                elif event == "update-current":
                    self._reset_update_controls(
                        f"Установлена последняя версия: {payload}."
                    )
                elif event == "update-downloading":
                    if self.settings_window is not None and self.settings_window.winfo_exists():
                        self.github_status.set(
                            f"Найдена версия {payload}. Скачиваю и проверяю EXE…"
                        )
                        self.github_progress.configure(
                            mode="determinate",
                            maximum=100,
                            value=0,
                        )
                        self.github_progress.grid()
                elif event == "update-progress":
                    received, total = payload
                    if self.settings_window is not None and self.settings_window.winfo_exists():
                        self.github_progress.configure(
                            mode="determinate",
                            maximum=total,
                            value=received,
                        )
                        percent = received * 100 // total
                        received_mb = received / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        self.github_status.set(
                            f"Скачиваю обновление: {percent}% "
                            f"({received_mb:.1f}/{total_mb:.1f} МБ)"
                        )
                elif event == "update-manual":
                    version, url = payload
                    should_open = messagebox.askyesno(
                        "Доступно обновление",
                        f"Найдена версия {version}. Автоустановка доступна "
                        "только для собранного Windows EXE. Открыть страницу релиза?",
                        parent=self.settings_window or self.root,
                    )
                    if should_open:
                        self._open_github_page(url)
                    self._reset_update_controls(
                        f"Доступна версия {version}; установите её со страницы релиза."
                    )
                elif event == "update-ready":
                    release, download_path, download_directory = payload
                    self._install_downloaded_update(
                        release,
                        download_path,
                        download_directory,
                    )
                elif event == "update-error":
                    self._reset_update_controls(
                        f"Проверка обновлений не удалась: {payload}"
                    )
                    messagebox.showerror(
                        "Проверка обновлений",
                        str(payload),
                        parent=self.settings_window or self.root,
                    )
                elif event == "local-sync":
                    if payload:
                        self.status.set(
                            f"Локально сохранено новых архивов: {payload}"
                        )
                elif event == "local-sync-error":
                    self.status.set(f"Ошибка локального сохранения: {payload}")
                elif event == "auto-change":
                    game, folder = payload
                    if game in self.suppressed_games:
                        continue
                    self.monitor_status.set(f"Изменились сейвы {game}; ожидаю автосейв…")
                    self._queue_auto_upload(game, folder)
                elif event == "auto-due":
                    self._start_auto_upload(str(payload))
                elif event == "monitor-resumed":
                    self.suppressed_games.discard(str(payload))
                elif event == "auto-error":
                    self.monitor_status.set(f"Ошибка автоотправки: {payload}")
                elif event == "auto-uploaded":
                    self.monitor_status.set(f"Автосейв {payload} отправлен на сервер")
                elif event == "download-progress":
                    received, total = payload
                    received_mb = received / (1024 * 1024)
                    if total and total > 0:
                        if self._download_indeterminate:
                            self.download_progress.stop()
                            self._download_indeterminate = False
                        self.download_progress.configure(
                            mode="determinate",
                            maximum=total,
                            value=received,
                        )
                        percent = received * 100 // total
                        total_mb = total / (1024 * 1024)
                        self.status.set(
                            f"Скачиваю сейв: {percent}% "
                            f"({received_mb:.1f}/{total_mb:.1f} МБ)"
                        )
                    else:
                        if not self._download_indeterminate:
                            self.download_progress.configure(mode="indeterminate")
                            self.download_progress.start(12)
                            self._download_indeterminate = True
                        self.status.set(
                            f"Скачиваю сейв: {received_mb:.1f} МБ"
                        )
                elif event == "download-phase":
                    if not self._download_indeterminate:
                        self.download_progress.configure(mode="indeterminate")
                        self.download_progress.start(12)
                        self._download_indeterminate = True
                    self.status.set(str(payload))
                elif isinstance(payload, list):
                    self.download_progress.stop()
                    self.download_progress.grid_remove()
                    self._download_indeterminate = False
                    for row in self.table.get_children():
                        self.table.delete(row)
                    self.entries.clear()
                    for save in payload:
                        item_id = save["id"]
                        self.entries[item_id] = save
                        game = save["game"]
                        world_name = minecraft_world_name(game)
                        save_name = save.get("save_name", "").strip()
                        if not save_name or save_name == game:
                            save_name = world_name or "Сохранение"
                        self.table.insert(
                            "",
                            "end",
                            iid=item_id,
                            values=(
                                "Minecraft" if world_name is not None else game,
                                save_name,
                                save["uploaded_by"],
                                save["created_at"],
                            ),
                        )
                    self.game_dropdown.configure(values=self._game_choices())
                    self.status.set(f"Найдено сохранений: {len(payload)}")
                else:
                    self.download_progress.stop()
                    self.download_progress.grid_remove()
                    self._download_indeterminate = False
                    self.status.set(str(payload))
        except Empty:
            pass
        self.root.after(100, self._process_events)


def main() -> None:
    root = tk.Tk()
    SaveSyncWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
