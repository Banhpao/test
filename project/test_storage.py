"""Test offline cho tầng SQLite; không cần Ollama, ComfyUI hay FastAPI."""

import tempfile
import unittest
from pathlib import Path
import sqlite3

from yumeai.storage.database import YumeDatabase


PERSONAS = {
    "yuki": {"name": "Yuki", "appearance": "black hair"},
    "hana": {"name": "Hana", "appearance": "brown hair"},
}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "yumeai.db"
        self.db = YumeDatabase(self.db_path)
        self.db.initialize(PERSONAS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_conversation_and_messages_survive_reopen(self):
        conv = self.db.create_conversation(
            "user_001", "yuki", display_name="Minh"
        )
        self.db.append_message(conv["id"], "user", "Chào em")
        self.db.append_message(conv["id"], "assistant", "Hừ, chào.")

        reopened = YumeDatabase(self.db_path)
        reopened.initialize(PERSONAS)
        messages = reopened.list_messages(conv["id"])

        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "Chào em")
        self.assertEqual(reopened.get_conversation(conv["id"])["message_count"], 2)

    def test_list_conversations_is_scoped_by_user(self):
        mine = self.db.create_conversation("user_001", "yuki")
        self.db.create_conversation("user_002", "hana")
        self.db.append_message(mine["id"], "user", "Tin nhắn gần nhất")

        rows = self.db.list_conversations("user_001")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], mine["id"])
        self.assertEqual(rows[0]["last_message"], "Tin nhắn gần nhất")

    def test_memory_fact_upsert_does_not_duplicate_key(self):
        self.db.create_conversation("user_001", "yuki")
        self.db.upsert_memory_fact("user_001", "yuki", "drink", "trà")
        self.db.upsert_memory_fact("user_001", "yuki", "drink", "cà phê")

        facts = self.db.list_memory_facts("user_001", "yuki")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["fact_value"], "cà phê")

    def test_gallery_keeps_generation_metadata(self):
        conv = self.db.create_conversation("user_001", "yuki")
        image = self.db.record_image(
            conversation_id=conv["id"],
            character_id="yuki",
            file_path="images/demo.png",
            prompt="Yuki dưới mưa",
            translated_prompt="Yuki in the rain",
            model_used="sd15",
            style="Anime",
            seed=123,
        )

        rows = self.db.list_images(user_id="user_001")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], image["id"])
        self.assertEqual(rows[0]["seed"], 123)

    def test_invalid_message_role_is_rejected(self):
        conv = self.db.create_conversation("user_001", "yuki")
        with self.assertRaises(ValueError):
            self.db.append_message(conv["id"], "tool", "Không hợp lệ")

    def test_memory_cursor_only_returns_unprocessed_text(self):
        conv = self.db.create_conversation("user_001", "yuki")
        first = self.db.append_message(conv["id"], "user", "Tôi thích cà phê")
        self.db.append_message(conv["id"], "assistant", "Tớ nhớ rồi")
        image = self.db.append_message(
            conv["id"], "assistant", '{"image_id":"x"}', content_type="image"
        )

        pending = self.db.pending_memory_messages(conv["id"])
        self.assertEqual(len(pending), 2)
        self.db.mark_memory_processed(conv["id"], first["id"])
        remaining = self.db.pending_memory_messages(conv["id"])
        self.assertEqual(len(remaining), 1)
        self.assertNotEqual(remaining[0]["id"], image["id"])

    def test_schema_v1_is_migrated_without_losing_conversation(self):
        old_path = Path(self.tmp.name) / "old.db"
        with sqlite3.connect(old_path) as conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE characters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, persona_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    character_id TEXT NOT NULL REFERENCES characters(id),
                    title TEXT NOT NULL, memory_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO users VALUES ('u', '', 't', 't');
                INSERT INTO characters VALUES ('yuki', 'Yuki', '{}', 't', 't');
                INSERT INTO conversations VALUES ('c', 'u', 'yuki', 'old', '', 't', 't');
                PRAGMA user_version = 1;
                """
            )

        migrated = YumeDatabase(old_path)
        migrated.initialize(PERSONAS)

        conversation = migrated.get_conversation("c")
        self.assertEqual(conversation["title"], "old")
        self.assertEqual(conversation["memory_processed_message_id"], 0)

    def test_image_without_conversation_still_shows_in_gallery(self):
        """Ảnh sinh ngoài hội thoại KHÔNG được biến mất khỏi gallery.

        Bản cũ lọc gallery theo user bằng INNER JOIN sang conversations, nên ảnh có
        conversation_id NULL bị loại sạch — mà /api/gallery thì luôn truyền user_id.
        Ảnh vẫn nằm trong DB và trên đĩa nhưng không đường nào xem lại được.
        """
        conv = self.db.create_conversation("user_001", "yuki")
        self.db.record_image(
            character_id="yuki", file_path="trong_hoi_thoai.png", prompt="A",
            user_id="user_001", conversation_id=conv["id"],
        )
        self.db.record_image(
            character_id="yuki", file_path="mo_coi.png", prompt="B", user_id="user_001",
        )

        paths = sorted(r["file_path"] for r in self.db.list_images(user_id="user_001"))
        self.assertEqual(paths, ["mo_coi.png", "trong_hoi_thoai.png"])
        # Vẫn phải tách được theo user, không phải cho hiện tuốt.
        self.assertEqual(self.db.list_images(user_id="user_002"), [])

    def test_image_owner_is_inferred_from_conversation(self):
        """Quên truyền user_id nhưng có hội thoại thì vẫn suy ra được chủ."""
        conv = self.db.create_conversation("user_001", "yuki")
        self.db.record_image(
            character_id="yuki", file_path="suy_ra.png", prompt="A",
            conversation_id=conv["id"],
        )
        image = self.db.list_images(conversation_id=conv["id"])[0]
        self.assertEqual(image["user_id"], "user_001")

    def test_schema_v2_backfills_image_owner(self):
        """Migration v2 → v3 phải gán chủ cho ảnh cũ, kể cả ảnh mồ côi."""
        old_path = Path(self.tmp.name) / "v2.db"
        with sqlite3.connect(old_path) as conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE characters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, persona_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, character_id TEXT NOT NULL,
                    title TEXT NOT NULL, memory_summary TEXT NOT NULL DEFAULT '',
                    memory_processed_message_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE images (
                    id TEXT PRIMARY KEY, conversation_id TEXT, character_id TEXT NOT NULL,
                    file_path TEXT NOT NULL, prompt TEXT NOT NULL,
                    translated_prompt TEXT NOT NULL DEFAULT '',
                    model_used TEXT NOT NULL DEFAULT '', style TEXT NOT NULL DEFAULT '',
                    seed INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                INSERT INTO users VALUES ('u', '', 't', 't');
                INSERT INTO characters VALUES ('yuki', 'Yuki', '{}', 't', 't');
                INSERT INTO conversations VALUES ('c', 'u', 'yuki', 'old', '', 0, 't', 't');
                INSERT INTO images VALUES ('i1', 'c', 'yuki', 'co_hoi_thoai.png', 'A',
                    '', '', '', 1, '{}', 't');
                INSERT INTO images VALUES ('i2', NULL, 'yuki', 'mo_coi.png', 'B',
                    '', '', '', 2, '{}', 't');
                PRAGMA user_version = 2;
                """
            )

        migrated = YumeDatabase(old_path)
        migrated.initialize(PERSONAS)

        owners = {r["file_path"]: r["user_id"] for r in migrated.list_images()}
        self.assertEqual(owners["co_hoi_thoai.png"], "u")   # suy từ hội thoại
        self.assertEqual(owners["mo_coi.png"], "u")         # DB một user -> gán cho user đó
        self.assertEqual(len(migrated.list_images(user_id="u")), 2)


if __name__ == "__main__":
    unittest.main()
