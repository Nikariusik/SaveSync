# SaveSync

SaveSync shares zipped game-save folders between people in a private room. The
server stores archives and metadata; the Windows desktop client lets players
upload a folder or download a selected archive into a configured folder.

This project is distributed under the MIT License; see [LICENSE](LICENSE).

## Server

Python 3.10 or newer is required.

```powershell
cd C:\panel
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

The app creates rooms from **Создать комнату** or **Настройки…**. The server
returns a 24-character random access code; this code is the room's only
credential. Share it only with room members. Old rooms that used a separate
password or short code must be recreated; their archived files are not deleted.

Archives and the SQLite index are stored in `.\data` by default. Set
`SAVE_STORAGE_DIR` to change this location. The upload limit defaults to 2 GiB;
set `MAX_UPLOAD_BYTES` to change it. Back up the storage directory.

## Desktop client

On Windows, run `python client.py` (Tkinter is included with most Python
installations). The compact main window keeps connection details, game and
save-folder selection, actions, and the save list in clearly separated rows.
Select
**Клиент** to connect to a server, or **Локальный сервер**
to run the API server directly on this PC. In host mode, choose **Настройки…**
to set the save-storage folder, listening port, and whether other clients may
upload saves, then press **Запустить сервер**.
Settings are grouped into **Подключение**, **Сейвы**, and **Свои игры** tabs.
The embedded server listens on all network interfaces. SaveSync shows its LAN
address and port; allow that TCP port through Windows Firewall if prompted.
To connect people outside the LAN, configure Porthole to forward the shown
process and TCP port to the shown LAN address (not `127.0.0.1`). The process is
`SaveSync.exe` in the packaged app or `python.exe` when run from source. Give
friends the external address shown by Porthole and the room access code; they
enter both in the main window. SaveSync does not configure Porthole itself.

The embedded server uses HTTP. This is suitable for a trusted LAN or a private,
encrypted tunnel such as Porthole, but should not be exposed directly to an
untrusted network. The room code is a bearer secret and appears in API URLs;
keep it private.

In client mode, enter a server address, access code, and player name. The
main-screen checkbox **Сохранять полученные сейвы на ПК** mirrors new server
archives to the configured local folder; archives are stored as ZIP files in per-game subfolders and are not
automatically extracted. The mirror is checked every 30 seconds.

Choose a title from the **Игра** dropdown; **Все игры** shows saves for every
game in the room. To upload manually, select a specific game first. When
exactly one supported game is being monitored, the upload button can use that
game while **Все игры** is selected.

Use **Загрузить мой сейв** to upload a folder manually, **Обновить список** to
see available archives, and select an archive before pressing **Скачать и
закинуть**. The client shows download progress and asks before extracting because files
with matching names can be replaced. Close the game before manual upload or
restore; automatic snapshots remain enabled while a game is running.

The client saves connection settings and custom game paths in the current
user's application config directory. Treat that Windows account as trusted.

Archives are listed newest-first; downloads are always for the explicitly
selected archive, so a newer upload is not silently overwritten by an older
one. Extraction rejects traversal paths, symbolic links, duplicate paths, and
archives expanding beyond 10 GiB.

### Pre-release Windows executable

The current very early alpha/beta test build is available on the GitHub
[Releases page](https://github.com/Nikariusik/SaveSync/releases). It may contain
bugs or lose data; keep separate backups of important worlds and saves. Please
report issues with steps to reproduce them.

### Build a single Windows executable

On Windows with Python installed, run:

```powershell
cd C:\panel
.\build-exe.ps1
```

The self-contained desktop client is written to `dist\SaveSync.exe`; Python
does not need to be installed on computers that run this executable. Share the
EXE with friends and have each person enter the address of the separately
running SaveSync server in Settings, or select host mode to run the included
server from the EXE. Windows SmartScreen may show a warning because the
un-signed test executable does not have a publisher certificate.

### Custom games

Open **Настройки… → Свои игры**, enter a display name, choose the game's save
folder, and optionally enter its executable name (for example,
`examplegame.exe`). The game appears in the main dropdown and its saves can be
uploaded or downloaded manually. If an executable name is provided, the client
also watches that folder while the matching process is running. Without a
process name the game remains available for manual use only.

## Automatic game detection and uploads

Automatic monitoring is enabled by default in the desktop client. Every five
seconds it checks running processes and watches the detected save directory.
After a save file changes, the client waits until changes settle (up to 30
seconds during continuous writes), creates a ZIP snapshot, and uploads it to
the configured room. **Настройки… → Автосейвы** lets you set the minimum
interval between uploads for each game (5 minutes by default). Changes during
that interval are combined, and the latest state is uploaded when the interval
expires. The first changed save after starting the client uploads immediately.
The same settings section limits each player's retained snapshots per game in
the room (10 by default, configurable from 1 to 100); when the limit is
exceeded, the server removes that player's oldest archives for that game.
Automatic snapshots are listed under the detected game name. The client must
stay open, and the server address and access code must be
configured for uploads to work. Monitoring stays active for 15 seconds after
a game exits so its final save can be uploaded. Minecraft worlds are identified
by their own folders containing `level.dat`; each world is monitored and
uploaded as a separate save. Restoring a downloaded snapshot temporarily pauses
that game's watcher so an older snapshot is not immediately republished. Use
the checkbox to pause monitoring.

Retention is grouped by room, game, and the uploader name entered in the
client. Names are not authenticated accounts, so people using the same name
share a retention limit.

The client currently detects:

| Game | Process | Save directory |
|---|---|---|
| Minecraft | `java.exe` / `javaw.exe` running the Minecraft client | Each world containing `level.dat` under `%APPDATA%\.minecraft\saves`, or `saves` under the active `--gameDir` |
| Terraria | `Terraria.exe` | `Documents\My Games\Terraria` |
| Project Zomboid | `ProjectZomboid*.exe` | `%USERPROFILE%\Zomboid\Saves` or `Documents\Zomboid\Saves` |
| Stardew Valley | `Stardew Valley.exe` | `%APPDATA%\StardewValley\Saves` |
| Valheim | `valheim.exe` | `%USERPROFILE%\AppData\LocalLow\IronGate\Valheim\worlds_local` |

If a game uses a custom save location, select that folder for manual upload;
automatic detection currently uses the standard paths above (plus Minecraft's
active `--gameDir`). Game saves are snapshots, not merged worlds. Avoid playing
the same world concurrently: a later upload becomes a separate snapshot and
does not combine changes from other players.
