"""Test offline cho sao_luu.py (lộ trình P3.4): sao lưu và khôi phục DB + ảnh."""

import json
import socket
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import sao_luu
from yumeai.storage import YumeDatabase

PERSONAS = {"pao": {"name": "Pao"}, "su": {"name": "Su"}}
NO_PORTS = ()   # không kiểm cổng — trừ test riêng về cổng


class SaoLuuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "data" / "yumeai.db"
        self.images = root / "data" / "images"
        self.backups = root / "data" / "backups"
        self.images.mkdir(parents=True)
        self.db = YumeDatabase(self.db_path)
        self.db.initialize(PERSONAS)
        self.conv = self.db.create_conversation("user_001", "pao", display_name="Minh")
        self.db.append_message(self.conv["id"], "user", "Hôm nay em vẽ gì thế?")
        self.db.upsert_memory_fact("user_001", "pao", "ten_goi", "Minh")
        for name in ("a.png", "b.png"):
            (self.images / name).write_bytes(b"\x89PNG " + name.encode())
            self.db.record_image(image_id=name[0], user_id="user_001", character_id="pao",
                                 file_path=f"images/{name}", prompt=name)

    def tearDown(self):
        self.tmp.cleanup()

    def backup(self, **kwargs):
        return sao_luu.create_backup(self.db_path, self.images, self.backups, **kwargs)

    def restore(self, zip_path, ports=NO_PORTS):
        return sao_luu.restore_backup(zip_path, self.db_path, self.images, self.backups, ports=ports)

    def count(self, table):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ── Sao lưu ─────────────────────────────────────────────
    def test_backup_contains_db_images_and_manifest(self):
        path = self.backup()
        self.assertTrue(path.name.startswith("sao-luu-"))
        self.assertFalse(list(self.backups.glob("*.part")))
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(names, {"yumeai.db", "images/a.png", "images/b.png", "manifest.json"})
        self.assertEqual((manifest["conversations"], manifest["messages"], manifest["memory_facts"]), (1, 1, 1))
        self.assertEqual((manifest["images"], manifest["image_files"]), (2, 2))

    def test_backup_while_db_is_open_in_wal_mode(self):
        # Ứng dụng đang chạy giữ DB ở chế độ WAL, dữ liệu mới có thể còn nằm trong -wal.
        writer = sqlite3.connect(self.db_path)
        try:
            writer.execute("PRAGMA journal_mode = WAL")
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute("INSERT INTO users(id, display_name, created_at, updated_at) VALUES ('u2', 'Lan', 't', 't')")
            writer.commit()
            path = self.backup()
        finally:
            writer.close()
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(json.loads(zf.read("manifest.json"))["users"], 2)

    def test_two_backups_in_the_same_second_do_not_overwrite(self):
        first, second = self.backup(), self.backup()
        self.assertNotEqual(first, second)
        self.assertEqual(len(sao_luu.list_backups(self.backups)), 2)

    def test_backup_without_db_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            sao_luu.create_backup(self.db_path.with_name("khong-co.db"), self.images, self.backups)

    # ── Khôi phục ───────────────────────────────────────────
    def test_restore_brings_back_db_and_images_and_keeps_a_safety_copy(self):
        path = self.backup()
        # Sau khi sao lưu: xoá hội thoại, thêm một ảnh, xoá một ảnh cũ.
        self.db.delete_conversation(self.conv["id"])
        (self.images / "c.png").write_bytes(b"\x89PNG c")
        (self.images / "a.png").unlink()

        result = self.restore(path)

        self.assertEqual(self.count("conversations"), 1)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(sorted(p.name for p in self.images.iterdir()), ["a.png", "b.png"])
        self.assertEqual(result["images_restored"], 2)
        safety = result["safety_backup"]
        self.assertTrue(safety.name.startswith("truoc-khoi-phuc-"))
        with zipfile.ZipFile(safety) as zf:
            self.assertIn("images/c.png", zf.namelist())
            self.assertEqual(json.loads(zf.read("manifest.json"))["conversations"], 0)

    def test_restore_drops_stale_wal_files(self):
        path = self.backup()
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        wal.write_bytes(b"nhat ky cu khong khop")
        self.restore(path)
        self.assertFalse(wal.exists())
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_restore_refused_while_app_is_running(self):
        path = self.backup()
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            with self.assertRaises(sao_luu.Refused) as ctx:
                self.restore(path, ports=(port,))
        self.assertIn(str(port), str(ctx.exception))
        self.assertFalse(list(self.backups.glob("truoc-khoi-phuc-*")))

    def test_broken_or_foreign_files_are_refused_before_touching_anything(self):
        before = self.db_path.read_bytes()
        not_zip = self.backups / "hong.zip"
        self.backups.mkdir(parents=True, exist_ok=True)
        not_zip.write_bytes(b"khong phai zip")
        no_db = self.backups / "thieu-db.zip"
        with zipfile.ZipFile(no_db, "w") as zf:
            zf.writestr("images/a.png", b"x")
        bad_db = self.backups / "db-hong.zip"
        with zipfile.ZipFile(bad_db, "w") as zf:
            zf.writestr("yumeai.db", b"SQLite format 3\x00" + b"\x00" * 200)
        for path in (not_zip, no_db, bad_db, self.backups / "khong-ton-tai.zip"):
            with self.subTest(path=path.name), self.assertRaises(sao_luu.Refused):
                self.restore(path)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_newer_schema_is_refused(self):
        path = self.backup()
        newer = self.backups / "ban-moi-hon.zip"
        with zipfile.ZipFile(path) as src, tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "yumeai.db"
            db.write_bytes(src.read("yumeai.db"))
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("PRAGMA user_version = 999")
            with zipfile.ZipFile(newer, "w") as dst:
                dst.write(db, "yumeai.db")
        with self.assertRaises(sao_luu.Refused) as ctx:
            self.restore(newer)
        self.assertIn("v999", str(ctx.exception))

    def test_zip_entries_cannot_escape_the_images_folder(self):
        path = self.backup()
        sneaky = self.backups / "len-loi.zip"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(sneaky, "w") as dst:
            dst.writestr("yumeai.db", src.read("yumeai.db"))
            dst.writestr("images/../../ra-ngoai.png", b"x")
        self.restore(sneaky)
        self.assertFalse((Path(self.tmp.name) / "ra-ngoai.png").exists())
        self.assertTrue((self.images / "ra-ngoai.png").exists())

    # ── Dòng lệnh ───────────────────────────────────────────
    def test_cli_asks_before_restoring(self):
        path = self.backup()
        with (
            patch.object(sao_luu, "inspect_backup", wraps=sao_luu.inspect_backup),
            patch.object(sao_luu, "restore_backup") as restore,
            patch("builtins.input", return_value="khong"),
        ):
            code = sao_luu.main(["--khoi-phuc", str(path)])
        self.assertEqual(code, 2)
        restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
