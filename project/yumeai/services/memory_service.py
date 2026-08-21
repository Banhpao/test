"""Trí nhớ bền vững cho companion, tối ưu cho máy cấu hình thấp.

Fact extraction chạy theo lô sau khi response đã stream xong. Nó không nằm trên
critical path của chat và không cần vector database. SQLite vẫn là nguồn sự thật;
LLM chỉ đề xuất fact có cấu trúc, repository quyết định dữ liệu được lưu.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import threading
import time

import httpx

from yumeai.core.config import (
    MEMORY_EXTRACTION_BATCH_SIZE,
    MEMORY_EXTRACTION_EVERY_MESSAGES,
    MEMORY_EXTRACTION_TIMEOUT_SEC,
    MEMORY_FACTS_ENABLED,
    OLLAMA_MODEL,
    OLLAMA_URL,
)


MAX_FACTS_PER_BATCH = 8
FAILURE_COOLDOWN_SEC = 300
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_FAILURE_UNTIL: dict[str, float] = {}

# Không tự động lưu bí mật hoặc định danh có thể gây hại nếu DB bị lộ. Người dùng
# vẫn có thể chủ động thêm một fact khác qua API, nhưng extractor phải bảo thủ.
_BLOCKED_KEY_PARTS = {
    "password", "passcode", "pin", "otp", "secret", "token", "api_key",
    "credit_card", "bank_account", "cccd", "cmnd", "passport",
    "exact_address", "precise_location",
}


@dataclass(frozen=True)
class MemoryExtractionResult:
    success: bool
    facts: list[dict]
    error: str = ""


def _conversation_lock(conversation_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(conversation_id, threading.Lock())


def _normalize_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower())
    return re.sub(r"_+", "_", key).strip("_")[:64]


def _is_blocked_key(key: str) -> bool:
    return any(
        key == part or key.startswith(f"{part}_") or key.endswith(f"_{part}")
        for part in _BLOCKED_KEY_PARTS
    )


def parse_memory_response(raw: str) -> MemoryExtractionResult:
    """Parse JSON bảo thủ; response sai format thì không đánh dấu đã xử lý."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except Exception as exc:
        return MemoryExtractionResult(False, [], f"JSON không hợp lệ: {exc}")

    if not isinstance(payload, dict) or "facts" not in payload:
        return MemoryExtractionResult(False, [], "Thiếu object JSON có trường facts")
    candidates = payload.get("facts")
    if not isinstance(candidates, list):
        return MemoryExtractionResult(False, [], "Trường facts không phải danh sách")

    facts = []
    seen = set()
    for item in candidates[:MAX_FACTS_PER_BATCH]:
        if not isinstance(item, dict):
            continue
        key = _normalize_key(str(item.get("key", "")))
        value = str(item.get("value", "")).strip()[:300]
        if not key or not value or key in seen or _is_blocked_key(key):
            continue
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        facts.append({
            "key": key,
            "value": value,
            "confidence": max(0.0, min(confidence, 1.0)),
        })
        seen.add(key)
    return MemoryExtractionResult(True, facts)


def extract_memory_facts(messages: list[dict]) -> MemoryExtractionResult:
    """Gọi model chat đang có để trích tối đa 8 fact ổn định về người dùng."""
    transcript = "\n".join(
        f"{item.get('role', '?')}: {item.get('content', '')}"
        for item in messages
        if item.get("content_type", "text") == "text"
    )[:7000]
    if not transcript.strip():
        return MemoryExtractionResult(True, [])

    system = """Bạn là bộ trích xuất trí nhớ cho ứng dụng AI companion local.
Chỉ lưu điều NGƯỜI DÙNG nói rõ và có ích ở những lần trò chuyện sau: tên muốn
được gọi, sở thích/ghét, nghề nghiệp, mục tiêu, sự kiện đang diễn ra, cách xưng hô,
lời hứa hoặc ranh giới. Không suy diễn từ lời nhân vật. Không lưu mật khẩu, mã OTP,
API key, tài khoản ngân hàng, giấy tờ định danh hay địa chỉ chính xác.

Trả về JSON đúng dạng:
{"facts":[{"key":"snake_case_english","value":"giá trị ngắn bằng tiếng Việt","confidence":0.0}]}
Tối đa 8 fact. Nếu không có gì đáng nhớ, trả {"facts":[]}. Không markdown."""

    try:
        with httpx.Client(timeout=MEMORY_EXTRACTION_TIMEOUT_SEC) as client:
            response = client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": transcript},
                ],
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_ctx": 2048,
                    "num_predict": 320,
                },
            })
            response.raise_for_status()
            raw = response.json().get("message", {}).get("content", "")
    except Exception as exc:
        return MemoryExtractionResult(False, [], str(exc))
    return parse_memory_response(raw)


def format_memory_context(facts: list[dict]) -> str:
    """Dạng ngắn đưa vào system prompt, giới hạn để model nhỏ không tràn context."""
    lines = []
    for fact in facts[:24]:
        key = str(fact.get("fact_key", fact.get("key", ""))).strip()
        value = str(fact.get("fact_value", fact.get("value", ""))).strip()
        if key and value:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def refresh_conversation_memory(database, conversation_id: str) -> dict:
    """Xử lý một batch nếu đủ ngưỡng; an toàn khi được gọi đồng thời."""
    if not MEMORY_FACTS_ENABLED:
        return {"status": "disabled", "facts": 0}
    now = time.monotonic()
    if _FAILURE_UNTIL.get(conversation_id, 0) > now:
        return {"status": "cooldown", "facts": 0}

    lock = _conversation_lock(conversation_id)
    if not lock.acquire(blocking=False):
        return {"status": "busy", "facts": 0}
    try:
        messages = database.pending_memory_messages(
            conversation_id,
            limit=MEMORY_EXTRACTION_BATCH_SIZE,
        )
        if len(messages) < MEMORY_EXTRACTION_EVERY_MESSAGES:
            return {"status": "waiting", "facts": 0, "pending": len(messages)}

        conversation = database.get_conversation(conversation_id)
        if not conversation:
            return {"status": "missing", "facts": 0}

        result = extract_memory_facts(messages)
        if not result.success:
            _FAILURE_UNTIL[conversation_id] = time.monotonic() + FAILURE_COOLDOWN_SEC
            print(f"⚠️ Memory extraction lỗi: {result.error}")
            return {"status": "error", "facts": 0, "error": result.error}

        source_message_id = messages[-1]["id"]
        for fact in result.facts:
            database.upsert_memory_fact(
                conversation["user_id"],
                conversation["character_id"],
                fact["key"],
                fact["value"],
                confidence=fact["confidence"],
                source_message_id=source_message_id,
            )
        # Kể cả batch không có fact, đánh dấu đã xem để không gọi lại vô hạn.
        database.mark_memory_processed(conversation_id, source_message_id)
        _FAILURE_UNTIL.pop(conversation_id, None)
        if result.facts:
            print(
                f"🧠 Đã lưu {len(result.facts)} memory fact cho conversation "
                f"{conversation_id[:8]}"
            )
        return {"status": "processed", "facts": len(result.facts)}
    finally:
        lock.release()
