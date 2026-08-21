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

    def initialize(self, personas: Mapping[str, dict] | None = None) -> None:
        """Tạo schema idempotent và đồng bộ các persona dựng sẵn."""
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

    def list_messages(self, conversation_id: str, limit: int | None = None) -> list[dict]:
        with self._connect() as conn:
            if limit is not None and limit > 0:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM messages
                        WHERE conversation_id = ? ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (conversation_id, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                    (conversation_id,),
                ).fetchall()
        return [dict(row) for row in rows]

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
