"""Test offline cho quyền kiểm soát dữ liệu (lộ trình P3): xoá ảnh, xoá hội thoại,
xuất toàn bộ ra zip, xoá toàn bộ dữ liệu. Không cần Ollama hay ComfyUI."""

import io
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from yumeai.core.personas import PERSONAS
from yumeai.storage import YumeDatabase


class DataControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "api.db"
        self.database = YumeDatabase(self.db_path)
        self.database.initialize(PERSONAS)
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir()
        self.old_database = main.database
        main.database = self.database

    def tearDown(self):
        main.database = self.old_database
        self.tmp.cleanup()

    def request(self, method, path, **kwargs):
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            patch.object(main, "GENERATED_IMAGES_DIR", str(self.images)),
            TestClient(main.app) as client,
        ):
            return client.request(method, path, **kwargs)

    def add_image(self, image_id, *, user_id="user_001", conversation_id=None, in_chat=False):
        (self.images / f"{image_id}.png").write_bytes(b"\x89PNG fake " + image_id.encode())
        self.database.record_image(
            image_id=image_id, conversation_id=conversation_id, user_id=user_id,
            character_id="pao", file_path=f"images/{image_id}.png", prompt=f"ảnh {image_id}",
        )
        if in_chat:
            self.database.append_message(
                conversation_id, "assistant",
                json.dumps({"image_id": image_id, "image_url": f"/generated/{image_id}.png", "prompt": "x"}),
                content_type="image",
            )

    # ── Xoá một ảnh ─────────────────────────────────────────
    def test_delete_image_removes_row_file_and_chat_message(self):
        conv = self.database.create_conversation("user_001", "pao")
        self.database.append_message(conv["id"], "user", "vẽ cho anh một tấm")
        self.add_image("a1", conversation_id=conv["id"], in_chat=True)
        self.add_image("a2", conversation_id=conv["id"], in_chat=True)

        res = self.request("DELETE", "/api/gallery/a1", params={"user_id": "user_001"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["messages_removed"], 1)
        self.assertFalse((self.images / "a1.png").exists())
        self.assertTrue((self.images / "a2.png").exists())
        self.assertEqual([i["id"] for i in self.database.list_images(user_id="user_001")], ["a2"])
        contents = [m["content"] for m in self.database.list_messages(conv["id"])]
        self.assertEqual(len(contents), 2)
        self.assertNotIn("a1", "".join(contents))

    def test_cannot_delete_someone_elses_image(self):
        self.add_image("b1", user_id="user_002")
        res = self.request("DELETE", "/api/gallery/b1", params={"user_id": "user_001"})
        self.assertEqual(res.status_code, 404)
        self.assertTrue((self.images / "b1.png").exists())
        self.assertEqual(self.request("DELETE", "/api/gallery/khong_co").status_code, 404)

    # ── Xoá một hội thoại ───────────────────────────────────
    def test_delete_conversation_keeps_its_images_in_gallery(self):
        conv = self.database.create_conversation("user_001", "pao")
        self.database.append_message(conv["id"], "user", "chào em")
        self.add_image("c1", conversation_id=conv["id"], in_chat=True)

        res = self.request("DELETE", f"/api/conversations/{conv['id']}", params={"user_id": "user_001"})

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(self.database.get_conversation(conv["id"]))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        images = self.database.list_images(user_id="user_001")
        self.assertEqual([(i["id"], i["conversation_id"]) for i in images], [("c1", None)])
        self.assertTrue((self.images / "c1.png").exists())

    def test_cannot_delete_someone_elses_conversation(self):
        conv = self.database.create_conversation("user_002", "pao")
        res = self.request("DELETE", f"/api/conversations/{conv['id']}", params={"user_id": "user_001"})
        self.assertEqual(res.status_code, 404)
        self.assertIsNotNone(self.database.get_conversation(conv["id"]))

    # ── Xuất toàn bộ ────────────────────────────────────────
    def test_export_zip_has_everything(self):
        conv = self.database.create_conversation("user_001", "pao", display_name="Minh")
        self.database.append_message(conv["id"], "user", "Hôm nay em vẽ gì thế?")
        self.database.upsert_memory_fact("user_001", "su", "tra_yeu_thich", "trà hoa cúc")
        self.add_image("d1", conversation_id=conv["id"], in_chat=True)
        self.database.record_image(image_id="d2", user_id="user_001", character_id="su",
                                   file_path="images/d2.png", prompt="file đã mất")

        res = self.request("GET", "/api/data/export", params={"user_id": "user_001"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "application/zip")
        self.assertIn("yumeai-du-lieu-", res.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            self.assertIn("images/d1.png", zf.namelist())
            data = json.loads(zf.read("data.json").decode("utf-8"))
        self.assertEqual(data["user"]["display_name"], "Minh")
        self.assertEqual(data["conversations"][0]["character_name"], "Pao")
        self.assertEqual(data["conversations"][0]["messages"][0]["content"], "Hôm nay em vẽ gì thế?")
        self.assertEqual(data["memory_facts"][0]["fact_value"], "trà hoa cúc")
        self.assertEqual(sorted(i["id"] for i in data["images"]), ["d1", "d2"])
        self.assertEqual(data["missing_image_files"], ["images/d2.png"])

    # ── Xoá toàn bộ ─────────────────────────────────────────
    def test_delete_all_requires_typed_confirmation(self):
        self.database.create_conversation("user_001", "pao")
        for confirm in ("", "co", "delete"):
            res = self.request("POST", "/api/data/delete", json={"user_id": "user_001", "confirm": confirm})
            self.assertEqual(res.status_code, 400)
        self.assertEqual(len(self.database.list_conversations("user_001")), 1)

    def test_delete_all_removes_only_this_users_data(self):
        mine = self.database.create_conversation("user_001", "pao")
        self.database.append_message(mine["id"], "user", "bí mật của anh")
        self.database.upsert_memory_fact("user_001", "pao", "ten_goi", "Minh")
        self.add_image("e1", conversation_id=mine["id"], in_chat=True)
        theirs = self.database.create_conversation("user_002", "su")
        self.database.upsert_memory_fact("user_002", "su", "ten_goi", "Lan")
        self.add_image("e2", user_id="user_002")
        # Ảnh cũ không rõ chủ: DB có hai người dùng nên KHÔNG được coi là của user_001.
        self.add_image("e3", user_id="")

        res = self.request("POST", "/api/data/delete", json={"user_id": "user_001", "confirm": "XOÁ"})

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual((body["conversations"], body["memory_facts"], body["images"]), (1, 1, 1))
        self.assertFalse((self.images / "e1.png").exists())
        self.assertTrue((self.images / "e2.png").exists())
        self.assertTrue((self.images / "e3.png").exists())
        self.assertEqual(self.database.list_conversations("user_001"), [])
        self.assertEqual(self.database.list_memory_facts("user_001", "pao"), [])
        self.assertEqual(len(self.database.list_conversations("user_002")), 1)
        self.assertEqual(self.database.list_memory_facts("user_002", "su")[0]["fact_value"], "Lan")
        self.assertIsNotNone(self.database.get_conversation(theirs["id"]))
        with closing(sqlite3.connect(self.db_path)) as conn:
            users = {r[0] for r in conn.execute("SELECT id FROM users")}
            characters = {r[0] for r in conn.execute("SELECT id FROM characters")}
        self.assertNotIn("user_001", users)
        self.assertEqual(characters, set(PERSONAS))
        # VACUUM đã chạy: chữ của tin nhắn đã xoá không còn nằm trong file DB.
        self.assertNotIn("bí mật của anh".encode("utf-8"), self.db_path.read_bytes())

    def test_single_user_delete_all_also_takes_unowned_old_images(self):
        self.database.create_conversation("user_001", "pao")
        self.add_image("f1", user_id="")
        res = self.request("POST", "/api/data/delete", json={"user_id": "user_001", "confirm": "xoa"})
        self.assertEqual(res.json()["images"], 1)
        self.assertFalse((self.images / "f1.png").exists())


if __name__ == "__main__":
    unittest.main()
