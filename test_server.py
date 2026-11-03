import asyncio
import io
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from server import create_app


def archive_bytes(content: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("world/save.dat", content)
    return output.getvalue()


class SaveRetentionTests(unittest.TestCase):
    def test_host_can_disable_client_uploads(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Path(temporary), allow_uploads=False)
            upload = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/api/rooms/{room_id}/saves"
            )

            async def submit():
                return await upload(
                    "test-room",
                    None,
                    "Minecraft",
                    "Alice",
                    10,
                    UploadFile(
                        filename="save.zip",
                        file=io.BytesIO(archive_bytes("save")),
                    ),
                )

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(submit())

            self.assertEqual(raised.exception.status_code, 403)

    def test_limit_keeps_newest_archives_per_player_and_game(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(root, room_tokens={"test-room": "test-password"})
            upload = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/api/rooms/{room_id}/saves"
            )

            async def submit(game: str, player: str, content: str, limit: int):
                return await upload(
                    "test-room",
                    None,
                    game,
                    player,
                    limit,
                    UploadFile(
                        filename="save.zip",
                        file=io.BytesIO(archive_bytes(content)),
                    ),
                )

            first = asyncio.run(submit("Minecraft", "Alice", "first", 2))
            second = asyncio.run(submit("Minecraft", "Alice", "second", 2))
            third = asyncio.run(submit("Minecraft", "Alice", "third", 2))
            asyncio.run(submit("Minecraft", "Bob", "bob", 2))
            asyncio.run(submit("Terraria", "Alice", "terraria", 2))

            connection = sqlite3.connect(root / "saves.sqlite3")
            try:
                alice_minecraft = connection.execute(
                    "SELECT id, filename FROM saves "
                    "WHERE room_id = ? AND game = ? AND uploaded_by = ? "
                    "ORDER BY created_at",
                    ("test-room", "Minecraft", "Alice"),
                ).fetchall()
                other_saves = connection.execute(
                    "SELECT COUNT(*) FROM saves "
                    "WHERE room_id = ? AND NOT (game = ? AND uploaded_by = ?)",
                    ("test-room", "Minecraft", "Alice"),
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(
                {row[0] for row in alice_minecraft},
                {second["id"], third["id"]},
            )
            self.assertEqual(other_saves, 2)
            self.assertFalse((root / "test-room" / f"{first['id']}.zip").exists())
            self.assertTrue((root / "test-room" / f"{second['id']}.zip").is_file())
            self.assertTrue((root / "test-room" / f"{third['id']}.zip").is_file())

    def test_upload_without_limit_preserves_legacy_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(root, room_tokens={"test-room": "test-password"})
            upload = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/api/rooms/{room_id}/saves"
            )

            async def submit(content: str):
                return await upload(
                    "test-room",
                    None,
                    "Minecraft",
                    "Alice",
                    None,
                    UploadFile(
                        filename="save.zip",
                        file=io.BytesIO(archive_bytes(content)),
                    ),
                )

            asyncio.run(submit("first"))
            asyncio.run(submit("second"))

            connection = sqlite3.connect(root / "saves.sqlite3")
            try:
                count = connection.execute("SELECT COUNT(*) FROM saves").fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
