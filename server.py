import os
import re
import secrets
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Generator

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel


ROOM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24}$")
ROOM_CODE_BYTES = 18
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024**3)))
MAX_ARCHIVE_ENTRIES = 100_000
MAX_EXPANDED_BYTES = 10 * 1024**3
MAX_SAVES_PER_PLAYER_GAME = 100


class CreateRoomRequest(BaseModel):
    name: str


def create_app(
    storage_root: Path | str | None = None,
    allow_uploads: bool = True,
) -> FastAPI:
    root = Path(storage_root or os.environ.get("SAVE_STORAGE_DIR", "data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    database_path = root / "saves.sqlite3"
    @contextmanager
    def connect_db() -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(database_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS saves (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                game TEXT NOT NULL,
                uploaded_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                filename TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS saves_room_game_time "
            "ON saves (room_id, game, created_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        room_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rooms)")
        }

    app = FastAPI(title="SaveSync API", version="1.0.0")

    def authorize_room(room_id: str) -> None:
        if not ROOM_ID_PATTERN.fullmatch(room_id):
            raise HTTPException(status_code=401, detail="Invalid access code")

        with connect_db() as connection:
            row = connection.execute(
                "SELECT 1 FROM rooms WHERE code = ?",
                (room_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid access code")

    def get_save(save_id: str, room_id: str) -> tuple:
        with connect_db() as connection:
            row = connection.execute(
                "SELECT id, room_id, game, uploaded_by, created_at, filename "
                "FROM saves WHERE id = ? AND room_id = ?",
                (save_id, room_id),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Save not found")
        return row

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/rooms/{room_id}")
    def room_info(
        room_id: str,
        _: Annotated[None, Depends(authorize_room)],
    ) -> dict[str, str]:
        with connect_db() as connection:
            row = connection.execute(
                "SELECT name FROM rooms WHERE code = ?",
                (room_id,),
            ).fetchone()
        return {"code": room_id, "name": row[0] if row is not None else room_id}

    @app.post("/api/rooms", status_code=201)
    def create_room(request: CreateRoomRequest) -> dict[str, str]:
        name = request.name.strip()
        if not name or len(name) > 60:
            raise HTTPException(status_code=400, detail="Room name must be 1-60 characters")
        created_at = datetime.now(timezone.utc).isoformat()
        with connect_db() as connection:
            for _ in range(10):
                room_code = secrets.token_urlsafe(ROOM_CODE_BYTES)
                try:
                    if {"password_salt", "password_hash"} <= room_columns:
                        connection.execute(
                            "INSERT INTO rooms "
                            "(code, name, password_salt, password_hash, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                room_code,
                                name,
                                secrets.token_bytes(16),
                                secrets.token_bytes(32),
                                created_at,
                            ),
                        )
                    else:
                        connection.execute(
                            "INSERT INTO rooms (code, name, created_at) VALUES (?, ?, ?)",
                            (room_code, name, created_at),
                        )
                except sqlite3.IntegrityError:
                    continue
                return {"code": room_code, "name": name}
        raise HTTPException(status_code=500, detail="Could not allocate a room code")

    @app.post("/api/rooms/{room_id}/saves", status_code=201)
    async def upload_save(
        room_id: str,
        _: Annotated[None, Depends(authorize_room)],
        game: Annotated[str, Form()],
        uploaded_by: Annotated[str, Form()] = "Unknown",
        max_saves: Annotated[int | None, Form()] = None,
        archive: Annotated[UploadFile, File()] = ...,
    ) -> dict[str, str | int]:
        if not allow_uploads:
            raise HTTPException(
                status_code=403,
                detail="This host does not allow clients to upload saves",
            )
        game = game.strip()
        uploaded_by = uploaded_by.strip() or "Unknown"
        if not game or len(game) > 100:
            raise HTTPException(status_code=400, detail="Game name must be 1-100 characters")
        if len(uploaded_by) > 80:
            raise HTTPException(status_code=400, detail="Uploader name is too long")
        if max_saves is not None and not 1 <= max_saves <= MAX_SAVES_PER_PLAYER_GAME:
            raise HTTPException(
                status_code=400,
                detail=f"max_saves must be between 1 and {MAX_SAVES_PER_PLAYER_GAME}",
            )

        save_id = uuid.uuid4().hex
        room_directory = root / room_id
        room_directory.mkdir(parents=True, exist_ok=True)
        final_path = room_directory / f"{save_id}.zip"
        temporary_path = room_directory / f"{save_id}.part"
        size = 0
        pruned_archives: list[str] = []
        try:
            with temporary_path.open("wb") as target:
                while chunk := await archive.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="Archive exceeds upload limit")
                    target.write(chunk)
            if size == 0 or not zipfile.is_zipfile(temporary_path):
                raise HTTPException(status_code=400, detail="Upload must be a non-empty ZIP archive")
            try:
                with zipfile.ZipFile(temporary_path) as archive_file:
                    members = archive_file.infolist()
                    if len(members) > MAX_ARCHIVE_ENTRIES:
                        raise HTTPException(status_code=413, detail="Archive contains too many files")
                    expanded_size = sum(member.file_size for member in members)
                    if expanded_size > MAX_EXPANDED_BYTES:
                        raise HTTPException(status_code=413, detail="Expanded archive exceeds 10 GiB")
                    bad_member = archive_file.testzip()
                    if bad_member is not None:
                        raise HTTPException(status_code=400, detail="ZIP archive is corrupted")
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail="ZIP archive is invalid") from exc

            temporary_path.replace(final_path)
            created_at = datetime.now(timezone.utc).isoformat()
            with connect_db() as connection:
                connection.execute(
                    "INSERT INTO saves (id, room_id, game, uploaded_by, created_at, filename) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (save_id, room_id, game, uploaded_by, created_at, final_path.name),
                )
                if max_saves is not None:
                    old_rows = connection.execute(
                        "SELECT id, filename FROM saves "
                        "WHERE room_id = ? AND game = ? AND uploaded_by = ? "
                        "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
                        (room_id, game, uploaded_by, max_saves),
                    ).fetchall()
                    pruned_archives = [row[1] for row in old_rows]
                    connection.executemany(
                        "DELETE FROM saves WHERE id = ?",
                        [(row[0],) for row in old_rows],
                    )
        except Exception:
            temporary_path.unlink(missing_ok=True)
            if final_path.exists():
                final_path.unlink()
            raise
        finally:
            await archive.close()
        for filename in pruned_archives:
            try:
                (room_directory / filename).unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Old save metadata was removed but archive cleanup failed: {exc}",
                ) from exc
        return {
            "id": save_id,
            "created_at": created_at,
            "deleted_old_saves": len(pruned_archives),
        }

    @app.get("/api/rooms/{room_id}/saves")
    def list_saves(
        room_id: str,
        _: Annotated[None, Depends(authorize_room)],
        game: str | None = None,
    ) -> list[dict[str, str]]:
        query = (
            "SELECT id, game, uploaded_by, created_at FROM saves WHERE room_id = ?"
        )
        parameters: list[str] = [room_id]
        if game:
            query += " AND game = ?"
            parameters.append(game)
        query += " ORDER BY created_at DESC LIMIT 200"
        with connect_db() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {"id": row[0], "game": row[1], "uploaded_by": row[2], "created_at": row[3]}
            for row in rows
        ]

    @app.get("/api/rooms/{room_id}/saves/{save_id}/download")
    def download_save(
        room_id: str,
        save_id: str,
        _: Annotated[None, Depends(authorize_room)],
    ) -> FileResponse:
        row = get_save(save_id, room_id)
        archive_path = root / room_id / row[5]
        if not archive_path.is_file():
            raise HTTPException(status_code=404, detail="Save archive is missing")
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{row[2]}-{row[4][:19].replace(':', '-')}.zip",
        )

    return app


app = create_app()
