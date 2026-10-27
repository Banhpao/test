"""Test offline cho việc đổi tên nhân vật Yuki → Pao, Hana → Su (D18).

Không cần Ollama hay ComfyUI. Kiểm ba điều người dùng thấy được: lịch sử cũ không
mất, DB được sao lưu trước khi sửa, và client còn gửi id cũ vẫn chạy.
"""

import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from yumeai.core.personas import PERSONA_ID_ALIASES, PERSONAS, resolve_persona_id
from yumeai.storage import YumeDatabase


OLD_PERSONAS = {
    "yuki": {"name": "Yuki", "appearance": "black hair"},
    "hana": {"name": "Hana", "appearance": "brown hair"},
}
NEW_PERSONAS = {
    "pao": {"name": "Pao", "appearance": "black hair"},
    "su": {"name": "Su", "appearance": "brown hair"},
}
RENAMES = {"yuki": "pao", "hana": "su"}


class RenameCharacterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "yumeai.db"
        self.backup_dir = self.db_path.parent / "backups"
        # DB "của bản cũ": dữ liệu đang gắn id yuki và hana.
        old = YumeDatabase(self.db_path)
        old.initialize(OLD_PERSONAS)
        self.auto_conv = old.create_conversation("user_001", "yuki", display_name="Minh")
        self.named_conv = old.create_conversation(
            "user_001", "yuki", title="Chuyện bức tranh Yuki vẽ"
        )
        old.append_message(self.auto_conv["id"], "user", "Chào em")
        old.append_message(self.auto_conv["id"], "assistant", "Hừ, chào.")
        old.upsert_memory_fact("user_001", "yuki", "ten_goi", "Minh")
        old.record_image(
            image_id="img1",
            conversation_id=self.auto_conv["id"],
            user_id="user_001",
            character_id="yuki",
            file_path="images/img1.png",
            prompt="dưới mưa",
        )
        self.hana_conv = old.create_conversation("user_001", "hana")
        old.upsert_memory_fact("user_001", "hana", "tra_yeu_thich", "hoa cúc")

    def tearDown(self):
        self.tmp.cleanup()

    def _open_new(self):
        db = YumeDatabase(self.db_path)
        db.initialize(NEW_PERSONAS, RENAMES)
        return db

    def _backups(self):
        return sorted(self.backup_dir.glob("*.db")) if self.backup_dir.exists() else []

    def test_history_memory_and_images_move_to_new_ids(self):
        db = self._open_new()

        conversations = {c["id"]: c for c in db.list_conversations("user_001")}
        self.assertEqual(conversations[self.auto_conv["id"]]["character_id"], "pao")
        self.assertEqual(conversations[self.named_conv["id"]]["character_id"], "pao")
        self.assertEqual(conversations[self.hana_conv["id"]]["character_id"], "su")
        self.assertEqual(
            [m["content"] for m in db.list_messages(self.auto_conv["id"])],
            ["Chào em", "Hừ, chào."],
        )
        pao_facts = db.list_memory_facts("user_001", "pao")
        su_facts = db.list_memory_facts("user_001", "su")
        self.assertEqual([(f["fact_key"], f["fact_value"]) for f in pao_facts], [("ten_goi", "Minh")])
        self.assertEqual([(f["fact_key"], f["fact_value"]) for f in su_facts], [("tra_yeu_thich", "hoa cúc")])
        self.assertEqual(db.list_memory_facts("user_001", "yuki"), [])
        self.assertEqual(db.list_memory_facts("user_001", "hana"), [])

        with closing(sqlite3.connect(self.db_path)) as conn:
            characters = dict(conn.execute("SELECT id, name FROM characters").fetchall())
            image_owner = conn.execute(
                "SELECT character_id FROM images WHERE id = 'img1'"
            ).fetchone()[0]
            fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(characters, {"pao": "Pao", "su": "Su"})
        self.assertEqual(image_owner, "pao")
        self.assertEqual(fk_problems, [])

    def test_only_auto_titles_are_renamed(self):
        db = self._open_new()
        self.assertEqual(db.get_conversation(self.auto_conv["id"])["title"], "Trò chuyện với Pao")
        self.assertEqual(db.get_conversation(self.hana_conv["id"])["title"], "Trò chuyện với Su")
        # Tiêu đề người dùng tự đặt là chữ của họ — không sửa.
        self.assertEqual(
            db.get_conversation(self.named_conv["id"])["title"], "Chuyện bức tranh Yuki vẽ"
        )

    def test_one_backup_for_all_renames_and_it_holds_old_data(self):
        self._open_new()
        self._open_new()  # mở lại lần hai: không còn gì để chuyển

        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertIn("doi-ten-yuki-thanh-pao-hana-thanh-su", backups[0].name)
        with closing(sqlite3.connect(backups[0])) as conn:
            owners = dict(conn.execute(
                "SELECT character_id, COUNT(*) FROM conversations GROUP BY character_id"
            ).fetchall())
        self.assertEqual(owners, {"yuki": 2, "hana": 1})

    def test_failure_midway_changes_nothing(self):
        """Lỗi ở nhân vật thứ hai thì nhân vật thứ nhất cũng không bị đổi dở."""
        real_move = YumeDatabase._move_character
        calls = []

        def flaky_move(conn, old_id, new_id, **kwargs):
            calls.append(old_id)
            if len(calls) == 2:
                raise sqlite3.OperationalError("giả lập mất điện giữa chừng")
            return real_move(conn, old_id, new_id, **kwargs)

        with patch.object(YumeDatabase, "_move_character", staticmethod(flaky_move)):
            with self.assertRaises(sqlite3.OperationalError):
                self._open_new()

        with closing(sqlite3.connect(self.db_path)) as conn:
            characters = {r[0] for r in conn.execute("SELECT id FROM characters")}
            owners = {r[0] for r in conn.execute("SELECT character_id FROM conversations")}
        self.assertEqual(calls, ["yuki", "hana"])
        self.assertEqual(characters, {"yuki", "hana"})
        self.assertEqual(owners, {"yuki", "hana"})

    def test_only_old_ids_present_are_moved(self):
        """DB chỉ có một trong hai id cũ thì vẫn chuyển được id đó."""
        path = Path(self.tmp.name) / "chi-co-yuki" / "yumeai.db"
        db = YumeDatabase(path)
        db.initialize({"yuki": {"name": "Yuki"}})
        db.create_conversation("user_001", "yuki")

        db = YumeDatabase(path)
        db.initialize(NEW_PERSONAS, RENAMES)
        backups = sorted((path.parent / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        self.assertIn("doi-ten-yuki-thanh-pao-", backups[0].name)
        self.assertNotIn("hana", backups[0].name)
        self.assertEqual(db.list_conversations("user_001")[0]["character_id"], "pao")

    def test_existing_fact_under_new_id_wins_on_conflict(self):
        db = YumeDatabase(self.db_path)
        # Lỡ đã có cả hai id (vd. chạy bản mới trên một bản sao DB chưa chuyển).
        db.initialize({**OLD_PERSONAS, **NEW_PERSONAS})
        db.upsert_memory_fact("user_001", "pao", "ten_goi", "Minh Anh")
        db.upsert_memory_fact("user_001", "pao", "do_uong", "trà")

        db = self._open_new()
        facts = {f["fact_key"]: f["fact_value"] for f in db.list_memory_facts("user_001", "pao")}
        self.assertEqual(facts, {"ten_goi": "Minh Anh", "do_uong": "trà"})

    def test_fresh_database_needs_no_backup(self):
        fresh = YumeDatabase(Path(self.tmp.name) / "moi" / "yumeai.db")
        fresh.initialize(NEW_PERSONAS, RENAMES)
        self.assertFalse((fresh.path.parent / "backups").exists())


class PersonaAliasTests(unittest.TestCase):
    def test_alias_table_points_at_real_personas(self):
        self.assertEqual(list(PERSONAS), ["pao", "su"])
        self.assertEqual(PERSONAS["pao"]["name"], "Pao")
        self.assertEqual(PERSONAS["su"]["name"], "Su")
        for old_id, new_id in PERSONA_ID_ALIASES.items():
            self.assertNotIn(old_id, PERSONAS)
            self.assertIn(new_id, PERSONAS)

    def test_resolve_persona_id(self):
        self.assertEqual(resolve_persona_id("yuki"), "pao")
        self.assertEqual(resolve_persona_id("hana"), "su")
        self.assertEqual(resolve_persona_id("pao"), "pao")
        self.assertEqual(resolve_persona_id("su"), "su")
        self.assertIsNone(resolve_persona_id(None))


class LegacyIdApiTests(unittest.TestCase):
    """Tab /app mở từ trước khi đổi tên vẫn gửi persona_id="yuki" / "hana"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = YumeDatabase(Path(self.tmp.name) / "api.db")
        self.database.initialize(PERSONAS)
        self.old_database = main.database
        main.database = self.database

    def tearDown(self):
        main.database = self.old_database
        self.tmp.cleanup()

    def test_old_ids_still_work_for_conversations_and_memory(self):
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            created = client.post(
                "/api/conversations",
                json={"user_id": "user_001", "persona_id": "yuki"},
            )
            created_su = client.post(
                "/api/conversations",
                json={"user_id": "user_001", "persona_id": "hana"},
            )
            saved = client.put(
                "/api/memory/ten_goi",
                json={"user_id": "user_001", "persona_id": "yuki", "value": "Minh"},
            )
            listed = client.get(
                "/api/memory", params={"user_id": "user_001", "persona_id": "yuki"}
            )
            deleted = client.delete(
                "/api/memory/ten_goi", params={"user_id": "user_001", "persona_id": "yuki"}
            )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["character_id"], "pao")
        self.assertEqual(created_su.json()["character_id"], "su")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(listed.json()["facts"][0]["fact_value"], "Minh")
        self.assertEqual(deleted.json()["deleted"], True)

    def test_default_persona_is_pao(self):
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            created = client.post("/api/conversations", json={"user_id": "user_001"})
            personas = client.get("/api/personas").json()["personas"]

        self.assertEqual(created.json()["character_id"], "pao")
        self.assertEqual(created.json()["title"], "Trò chuyện với Pao")
        self.assertEqual([p["id"] for p in personas], ["pao", "su"])


if __name__ == "__main__":
    unittest.main()
