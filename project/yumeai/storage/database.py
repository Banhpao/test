"""SQLite repository cho dữ liệu sản phẩm YumeAI.

Module chỉ dùng thư viện chuẩn để bản local không phải chạy thêm Postgres,
Redis hay vector database. Mỗi thao tác mở một connection ngắn; cách này an
toàn khi FastAPI xử lý request ở nhiều thread và vẫn đủ nhanh cho một người dùng
trên máy cá nhân.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterator, Mapping
from uuid import uuid4


SCHEMA_VERSION = 3
ALLOWED_MESSAGE_ROLES = {"user", "assistant", "system"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class YumeDatabase:
    """Repository SQLite cho user, nhân vật, hội thoại, trí nhớ và ảnh."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(
        self,
        personas: Mapping[str, dict] | None = None,
        renamed_ids: Mapping[str, str] | None = None,
    ) -> None:
        """Tạo schema idempotent và đồng bộ các persona dựng sẵn.

        renamed_ids: {id cũ: id mới} của nhân vật đã đổi tên (PERSONA_ID_ALIASES).
        Dữ liệu gắn id cũ được chuyển sang id mới TRƯỚC khi đồng bộ persona.
        """
        with self._connect() as conn, conn:
            # WAL cho phép UI đọc lịch sử trong lúc stream đang ghi tin nhắn mới.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    persona_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    character_id TEXT NOT NULL REFERENCES characters(id),
                    title TEXT NOT NULL,
                    memory_summary TEXT NOT NULL DEFAULT '',
                    memory_processed_message_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
                    ON conversations(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
                    ON messages(conversation_id, id);

                CREATE TABLE IF NOT EXISTS memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, character_id, fact_key)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_facts_owner
                    ON memory_facts(user_id, character_id);

                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
                    -- Ảnh thuộc về NGƯỜI DÙNG, hội thoại chỉ là nơi nó xuất hiện.
                    -- Trước đây gallery lọc theo user bằng JOIN conversations, nên ảnh
                    -- sinh ra ngoài hội thoại (conversation_id NULL) bị INNER JOIN loại
                    -- và biến mất khỏi gallery vĩnh viễn dù vẫn nằm trong DB lẫn trên đĩa.
                    user_id TEXT NOT NULL DEFAULT '',
                    character_id TEXT NOT NULL REFERENCES characters(id),
                    file_path TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    translated_prompt TEXT NOT NULL DEFAULT '',
                    model_used TEXT NOT NULL DEFAULT '',
                    style TEXT NOT NULL DEFAULT '',
                    seed INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_images_conversation_created
                    ON images(conversation_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_images_character_created
                    ON images(character_id, created_at DESC);
                """
            )

            # Migration v1 → v2. Không dựa riêng vào user_version vì người dùng
            # có thể copy DB giữa các bản; kiểm tra column mới là nguồn sự thật.
            conversation_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "memory_processed_message_id" not in conversation_columns:
                conn.execute(
                    """
                    ALTER TABLE conversations
                    ADD COLUMN memory_processed_message_id INTEGER NOT NULL DEFAULT 0
                    """
                )

            # Migration v2 → v3: images.user_id.
            image_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(images)").fetchall()
            }
            if "user_id" not in image_columns:
                conn.execute(
                    "ALTER TABLE images ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
                )
                # Ảnh gắn hội thoại thì suy ra được chủ.
                conn.execute(
                    """
                    UPDATE images
                    SET user_id = COALESCE(
                        (SELECT c.user_id FROM conversations c WHERE c.id = images.conversation_id),
                        ''
                    )
                    WHERE user_id = ''
                    """
                )
                # Ảnh mồ côi (conversation_id NULL) thì không suy ra được. Nếu DB chỉ có
                # đúng một người dùng — trường hợp thường gặp của bản local — thì gán cho
                # người đó thay vì để trống.
                owners = conn.execute("SELECT id FROM users LIMIT 2").fetchall()
                if len(owners) == 1:
                    conn.execute(
                        "UPDATE images SET user_id = ? WHERE user_id = ''",
                        (owners[0]["id"],),
                    )

            # Index tạo ở đây chứ không trong executescript phía trên: với DB cũ thì cột
            # user_id chưa tồn tại lúc script đó chạy, CREATE INDEX sẽ nổ.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_images_user_created
                    ON images(user_id, created_at DESC)
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        # Đổi tên chạy trước sync_personas: sync chỉ upsert, không bao giờ xoá, nên
        # nếu chạy sau thì dòng nhân vật cũ vẫn nằm lại và giữ toàn bộ lịch sử.
        if renamed_ids:
            new_names = {
                new_id: ((personas or {}).get(new_id) or {}).get("name")
                for new_id in renamed_ids.values()
            }
            self.rename_characters(renamed_ids, new_names=new_names)

        if personas:
            self.sync_personas(personas)

    def sync_personas(self, personas: Mapping[str, dict]) -> None:
        """Upsert persona để DB phản ánh hồ sơ đang dùng trong code."""
        now = _utc_now()
        with self._connect() as conn, conn:
            for persona_id, persona in personas.items():
                conn.execute(
                    """
                    INSERT INTO characters(id, name, persona_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        persona_json = excluded.persona_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        persona_id,
                        persona.get("name", persona_id),
                        json.dumps(persona, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

    def rename_characters(
        self,
        renames: Mapping[str, str],
        *,
        new_names: Mapping[str, str | None] | None = None,
    ) -> Path | None:
        """Chuyển hội thoại, trí nhớ và ảnh của mỗi nhân vật id cũ sang id mới.

        renames: {id cũ: id mới}. new_names: {id mới: tên mới}, dùng để đổi tiêu đề
        hội thoại tự sinh. Trả đường dẫn bản sao lưu nếu có chuyển dữ liệu; None nếu
        không có gì để làm (DB mới, hoặc đã chuyển từ lần trước — chạy lại vô hại).

        Không UPDATE characters.id tại chỗ: khoá ngoại không có ON UPDATE CASCADE và
        foreign_keys đang bật, nên đổi id cha trước sẽ bị chặn. Thứ tự an toàn là tạo
        dòng cha mới → chuyển các bảng con → xoá dòng cha cũ. Mọi nhân vật đổi tên đi
        chung MỘT bản sao lưu và MỘT transaction: mất điện giữa chừng thì DB vẫn
        nguyên như trước, không có chuyện đổi được người này mà sót người kia.
        """
        pairs = [
            ((old_id or "").strip(), (new_id or "").strip())
            for old_id, new_id in renames.items()
        ]
        pairs = [(o, n) for o, n in pairs if o and n and o != n]
        if not pairs:
            return None
        with self._connect() as conn:
            old_names = {
                row["id"]: row["name"]
                for row in conn.execute(
                    f"SELECT id, name FROM characters WHERE id IN ({','.join('?' * len(pairs))})",
                    [o for o, _ in pairs],
                ).fetchall()
            }
        pairs = [(o, n) for o, n in pairs if o in old_names]
        if not pairs:
            return None

        # Sửa hàng loạt dữ liệu thật của người dùng → chụp bản sao trước đã.
        label = "doi-ten-" + "-".join(f"{o}-thanh-{n}" for o, n in pairs)
        backup_path = self._backup_before(label)
        names = new_names or {}
        with self._connect() as conn, conn:
            for old_id, new_id in pairs:
                self._move_character(
                    conn, old_id, new_id,
                    old_name=old_names[old_id], new_name=names.get(new_id),
                )
        return backup_path

    @staticmethod
    def _move_character(
        conn: sqlite3.Connection,
        old_id: str,
        new_id: str,
        *,
        old_name: str,
        new_name: str | None,
    ) -> None:
        """Một nhân vật trong rename_characters(); chạy bên trong transaction của nó."""
        conn.execute(
            """
            INSERT OR IGNORE INTO characters(id, name, persona_json, created_at, updated_at)
            SELECT ?, name, persona_json, created_at, ? FROM characters WHERE id = ?
            """,
            (new_id, _utc_now(), old_id),
        )
        conn.execute(
            "UPDATE conversations SET character_id = ? WHERE character_id = ?",
            (new_id, old_id),
        )
        if new_name:
            # Chỉ đổi tiêu đề tự sinh ("Trò chuyện với Yuki"); tiêu đề người dùng
            # tự đặt thì giữ nguyên chữ của họ.
            conn.execute(
                """
                UPDATE conversations SET title = ?
                WHERE character_id = ? AND title = ?
                """,
                (f"Trò chuyện với {new_name}", new_id, f"Trò chuyện với {old_name}"),
            )
        # UNIQUE(user_id, character_id, fact_key): nếu id mới đã có fact cùng khoá
        # thì giữ bản của id mới, bỏ bản cũ trùng khoá thay vì làm hỏng cả lần chuyển.
        conn.execute(
            "UPDATE OR IGNORE memory_facts SET character_id = ? WHERE character_id = ?",
            (new_id, old_id),
        )
        conn.execute("DELETE FROM memory_facts WHERE character_id = ?", (old_id,))
        conn.execute(
            "UPDATE images SET character_id = ? WHERE character_id = ?",
            (new_id, old_id),
        )
        conn.execute("DELETE FROM characters WHERE id = ?", (old_id,))

    def _backup_before(self, label: str) -> Path | None:
        """Sao lưu DB vào data/backups/ trước một thay đổi dữ liệu hàng loạt."""
        if not self.path.exists():
            return None
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"truoc-{label}-{stamp}.db"
        # Dùng backup API của sqlite chứ không copy file: DB chạy WAL, copy riêng file
        # .db có thể thiếu những trang còn nằm trong file -wal.
        with self._connect() as source:
            copy = sqlite3.connect(str(target))
            try:
                source.backup(copy)
            finally:
                copy.close()
        return target

    def ensure_user(self, user_id: str, display_name: str = "") -> dict:
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("user_id không được để trống")
        now = _utc_now()
        with self._connect() as conn, conn:
            conn.execute(
                """
                INSERT INTO users(id, display_name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = CASE
                        WHEN excluded.display_name <> '' THEN excluded.display_name
                        ELSE users.display_name
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, display_name.strip(), now, now),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_dict(row) or {}

    def create_conversation(
        self,
        user_id: str,
        character_id: str,
        *,
        title: str = "",
        display_name: str = "",
    ) -> dict:
        self.ensure_user(user_id, display_name)
        now = _utc_now()
        conversation_id = uuid4().hex
        with self._connect() as conn, conn:
            character = conn.execute(
                "SELECT name FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
            if character is None:
                raise ValueError("Nhân vật không tồn tại")
            clean_title = title.strip() or f"Trò chuyện với {character['name']}"
            conn.execute(
                """
                INSERT INTO conversations(
                    id, user_id, character_id, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, user_id, character_id, clean_title, now, now),
            )
        return self.get_conversation(conversation_id) or {}

    def get_or_create_conversation(
        self,
        user_id: str,
        character_id: str,
        *,
        display_name: str = "",
    ) -> dict:
        """Hội thoại đang mở của (user, nhân vật); chưa có thì tạo mới.

        Dành cho UI Gradio: ở đó không có khái niệm chọn hội thoại, người dùng chỉ
        mở app rồi nói chuyện tiếp. Lấy hội thoại được đụng gần nhất để mở lại app
        là nói tiếp câu chuyện cũ chứ không bắt đầu từ con số không.
        """
        self.ensure_user(user_id, display_name)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM conversations
                WHERE user_id = ? AND character_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (user_id, character_id),
            ).fetchone()
        if row is not None:
            existing = self.get_conversation(row["id"])
            if existing:
                return existing
        return self.create_conversation(
            user_id, character_id, display_name=display_name
        )

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, ch.name AS character_name, u.display_name AS user_name,
                       (SELECT COUNT(*) FROM messages m
                        WHERE m.conversation_id = c.id) AS message_count
                FROM conversations c
                JOIN characters ch ON ch.id = c.character_id
                JOIN users u ON u.id = c.user_id
                WHERE c.id = ?
                """,
                (conversation_id,),
            ).fetchone()
        return _row_dict(row)

    def list_conversations(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, ch.name AS character_name,
                       (SELECT COUNT(*) FROM messages m
                        WHERE m.conversation_id = c.id) AS message_count,
                       (SELECT content FROM messages lm
                        WHERE lm.conversation_id = c.id
                        ORDER BY lm.id DESC LIMIT 1) AS last_message
                FROM conversations c
                JOIN characters ch ON ch.id = c.character_id
                WHERE c.user_id = ?
                ORDER BY c.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        content_type: str = "text",
    ) -> dict:
        if role not in ALLOWED_MESSAGE_ROLES:
            raise ValueError(f"Role tin nhắn không hợp lệ: {role}")
        content = (content or "").strip()
        if not content:
            raise ValueError("Nội dung tin nhắn không được để trống")
        now = _utc_now()
        with self._connect() as conn, conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Cuộc hội thoại không tồn tại")
            cur = conn.execute(
                """
                INSERT INTO messages(conversation_id, role, content, content_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, content_type, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_dict(row) or {}

    def list_messages(
        self,
        conversation_id: str,
        limit: int | None = None,
        *,
        content_type: str | None = None,
    ) -> list[dict]:
        # content_type để lọc NGAY trong SQL: LIMIT áp lên cả message ảnh thì N message
        # cuối có thể chứa ít text hơn số mà ngữ cảnh chat cần.
        where = "conversation_id = ?"
        params: list = [conversation_id]
        if content_type is not None:
            where += " AND content_type = ?"
            params.append(content_type)
        with self._connect() as conn:
            if limit is not None and limit > 0:
                rows = conn.execute(
                    f"""
                    SELECT * FROM (
                        SELECT * FROM messages
                        WHERE {where} ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (*params, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM messages WHERE {where} ORDER BY id ASC",
                    params,
                ).fetchall()
        return [dict(row) for row in rows]

    def count_messages(
        self,
        conversation_id: str,
        *,
        content_type: str | None = None,
    ) -> int:
        where = "conversation_id = ?"
        params: list = [conversation_id]
        if content_type is not None:
            where += " AND content_type = ?"
            params.append(content_type)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}", params
            ).fetchone()
        return int(row[0]) if row else 0

    def update_memory_summary(self, conversation_id: str, summary: str) -> None:
        with self._connect() as conn, conn:
            conn.execute(
                "UPDATE conversations SET memory_summary = ?, updated_at = ? WHERE id = ?",
                ((summary or "").strip(), _utc_now(), conversation_id),
            )

    def pending_memory_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 24,
    ) -> list[dict]:
        """Lấy text message chưa được fact extractor xử lý, cũ trước mới sau."""
        with self._connect() as conn:
            conversation = conn.execute(
                """
                SELECT memory_processed_message_id FROM conversations WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                return []
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = ?
                  AND id > ?
                  AND content_type = 'text'
                ORDER BY id ASC
                LIMIT ?
                """,
                (
                    conversation_id,
                    conversation["memory_processed_message_id"],
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_memory_processed(self, conversation_id: str, message_id: int) -> None:
        """Chỉ tiến con trỏ về phía trước để hai background job không ghi lùi."""
        with self._connect() as conn, conn:
            conn.execute(
                """
                UPDATE conversations
                SET memory_processed_message_id = MAX(memory_processed_message_id, ?)
                WHERE id = ?
                """,
                (int(message_id), conversation_id),
            )

    def upsert_memory_fact(
        self,
        user_id: str,
        character_id: str,
        fact_key: str,
        fact_value: str,
        *,
        confidence: float = 1.0,
        source_message_id: int | None = None,
    ) -> dict:
        clean_key = (fact_key or "").strip()
        clean_value = (fact_value or "").strip()
        if not clean_key or not clean_value:
            raise ValueError("Khoá và giá trị trí nhớ không được để trống")
        clean_confidence = float(confidence)
        if not math.isfinite(clean_confidence):
            clean_confidence = 0.8
        clean_confidence = max(0.0, min(clean_confidence, 1.0))
        now = _utc_now()
        with self._connect() as conn, conn:
            conn.execute(
                """
                INSERT INTO memory_facts(
                    user_id, character_id, fact_key, fact_value, confidence,
                    source_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, character_id, fact_key) DO UPDATE SET
                    fact_value = excluded.fact_value,
                    confidence = excluded.confidence,
                    source_message_id = excluded.source_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    character_id,
                    clean_key,
                    clean_value,
                    clean_confidence,
                    source_message_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM memory_facts
                WHERE user_id = ? AND character_id = ? AND fact_key = ?
                """,
                (user_id, character_id, clean_key),
            ).fetchone()
        return _row_dict(row) or {}

    def list_memory_facts(self, user_id: str, character_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_facts
                WHERE user_id = ? AND character_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id, character_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_memory_fact(
        self,
        user_id: str,
        character_id: str,
        fact_key: str,
    ) -> bool:
        with self._connect() as conn, conn:
            cur = conn.execute(
                """
                DELETE FROM memory_facts
                WHERE user_id = ? AND character_id = ? AND fact_key = ?
                """,
                (user_id, character_id, fact_key.strip()),
            )
        return cur.rowcount > 0

    def record_image(
        self,
        *,
        character_id: str,
        file_path: str,
        prompt: str,
        user_id: str = "",
        conversation_id: str | None = None,
        translated_prompt: str = "",
        model_used: str = "",
        style: str = "",
        seed: int | None = None,
        metadata: dict | None = None,
        image_id: str | None = None,
    ) -> dict:
        image_id = image_id or uuid4().hex
        now = _utc_now()
        owner = (user_id or "").strip()
        with self._connect() as conn, conn:
            # Không truyền user_id nhưng có hội thoại thì suy ra chủ từ hội thoại, để
            # ảnh không bao giờ rơi vào trạng thái "không có chủ" và mất khỏi gallery.
            if not owner and conversation_id:
                found = conn.execute(
                    "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if found is not None:
                    owner = found["user_id"]
            conn.execute(
                """
                INSERT INTO images(
                    id, conversation_id, user_id, character_id, file_path, prompt,
                    translated_prompt, model_used, style, seed, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    conversation_id,
                    owner,
                    character_id,
                    file_path,
                    prompt.strip(),
                    translated_prompt.strip(),
                    model_used,
                    style,
                    seed,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            if conversation_id:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
            row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        return _row_dict(row) or {}

    def list_images(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses = []
        params: list[object] = []
        if conversation_id:
            clauses.append("i.conversation_id = ?")
            params.append(conversation_id)
        if user_id:
            # Lọc thẳng trên images.user_id. Bản cũ JOIN sang conversations, mà đó là
            # INNER JOIN nên mọi ảnh có conversation_id NULL đều bị loại — sinh ảnh
            # ngoài hội thoại là ảnh biến mất khỏi gallery vĩnh viễn.
            # user_id = '' là ảnh cũ trước migration v3 không suy ra được chủ; bản local
            # chưa có đăng nhập nên vẫn cho hiện thay vì giấu mất của người dùng.
            clauses.append("(i.user_id = ? OR i.user_id = '')")
            params.append(user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT i.* FROM images i
                {where}
                ORDER BY i.created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # ── Xoá và xuất dữ liệu (P3: người dùng phải xoá/xuất được dữ liệu của mình) ──

    def get_image(self, image_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        return _row_dict(row)

    def delete_image(self, image_id: str) -> dict | None:
        """Xoá ảnh khỏi gallery và khỏi hội thoại đang chứa nó.

        Trả dòng ảnh vừa xoá (kèm số tin nhắn ảnh đã gỡ), None nếu không có ảnh đó.
        File PNG do tầng gọi xoá — tầng DB không biết thư mục ảnh nằm đâu.
        Gỡ cả tin nhắn ảnh trỏ tới nó: để lại thì mở hội thoại ra là một khung ảnh hỏng.
        """
        with self._connect() as conn, conn:
            row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            if row is None:
                return None
            removed = 0
            if row["conversation_id"]:
                candidates = conn.execute(
                    """
                    SELECT id, content FROM messages
                    WHERE conversation_id = ? AND content_type = 'image'
                    """,
                    (row["conversation_id"],),
                ).fetchall()
                for message in candidates:
                    try:
                        data = json.loads(message["content"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(data, dict) and data.get("image_id") == image_id:
                        conn.execute("DELETE FROM messages WHERE id = ?", (message["id"],))
                        removed += 1
            conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        result = dict(row)
        result["messages_removed"] = removed
        return result

    def delete_conversation(self, conversation_id: str) -> bool:
        """Xoá hội thoại cùng mọi tin nhắn của nó.

        Ảnh sinh trong hội thoại vẫn ở lại gallery (conversation_id → NULL): ảnh thuộc
        về người dùng, không thuộc về hội thoại (D14). Muốn bỏ ảnh thì xoá từ gallery.
        """
        with self._connect() as conn, conn:
            cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cursor.rowcount > 0

    def _owned_image_clause(self, conn: sqlite3.Connection) -> str:
        # Ảnh user_id = '' (từ trước migration v3, không suy ra được chủ) chỉ được coi là
        # của người này khi DB chỉ có một người dùng — đúng như lúc migration gán chủ.
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return "(user_id = ? OR user_id = '')" if users <= 1 else "user_id = ?"

    def export_user(self, user_id: str) -> dict:
        """Toàn bộ dữ liệu của một người dùng, dạng dict sẵn để json.dumps."""
        with self._connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            conversations = [
                dict(row) for row in conn.execute(
                    """
                    SELECT c.*, ch.name AS character_name FROM conversations c
                    JOIN characters ch ON ch.id = c.character_id
                    WHERE c.user_id = ? ORDER BY c.created_at
                    """,
                    (user_id,),
                ).fetchall()
            ]
            for conversation in conversations:
                conversation["messages"] = [
                    dict(row) for row in conn.execute(
                        """
                        SELECT id, role, content, content_type, created_at FROM messages
                        WHERE conversation_id = ? ORDER BY id
                        """,
                        (conversation["id"],),
                    ).fetchall()
                ]
            facts = [
                dict(row) for row in conn.execute(
                    """
                    SELECT character_id, fact_key, fact_value, confidence, created_at, updated_at
                    FROM memory_facts WHERE user_id = ? ORDER BY character_id, fact_key
                    """,
                    (user_id,),
                ).fetchall()
            ]
            images = [
                dict(row) for row in conn.execute(
                    f"SELECT * FROM images WHERE {self._owned_image_clause(conn)} ORDER BY created_at",
                    (user_id,),
                ).fetchall()
            ]
        return {
            "user": _row_dict(user),
            "conversations": conversations,
            "memory_facts": facts,
            "images": images,
        }

    def delete_user_data(self, user_id: str) -> dict:
        """Xoá toàn bộ dữ liệu của một người dùng: hội thoại, tin nhắn, ký ức, ảnh, hồ sơ.

        Trả số lượng đã xoá và danh sách file ảnh để tầng gọi xoá trên đĩa.
        KHÔNG tự sao lưu: người dùng chủ động xoá thì phải là xoá thật. Sau khi xoá chạy
        VACUUM, vì SQLite giữ nội dung dòng đã xoá trong trang trống tới lúc đó.
        """
        with self._connect() as conn, conn:
            clause = self._owned_image_clause(conn)
            images = conn.execute(
                f"SELECT id, file_path FROM images WHERE {clause}", (user_id,)
            ).fetchall()
            conversations = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            facts = conn.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            conn.execute(f"DELETE FROM images WHERE {clause}", (user_id,))
            # messages theo hội thoại và memory_facts theo user đều ON DELETE CASCADE.
            conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM memory_facts WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        return {
            "conversations": conversations,
            "memory_facts": facts,
            "images": len(images),
            "image_files": [row["file_path"] for row in images],
        }
