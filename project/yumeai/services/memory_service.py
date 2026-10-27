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

from yumeai.core.chat_context import summarize_old_history
from yumeai.core.config import (
    CHAT_NUM_CTX,
    MEMORY_EXTRACTION_BATCH_SIZE,
    MEMORY_EXTRACTION_EVERY_MESSAGES,
    MEMORY_EXTRACTION_TIMEOUT_SEC,
    MEMORY_FACTS_ENABLED,
    OLLAMA_MODEL,
    OLLAMA_URL,
    ollama_options,
)


MAX_FACTS_PER_BATCH = 8
FAILURE_COOLDOWN_SEC = 300
# 8 fact tiếng Việt dạng JSON cần ~400-500 token. Bản cũ cho 320 nên JSON hay bị cắt
# giữa chừng → parse lỗi → con trỏ không tiến → CÙNG lô đó lỗi lại mãi sau mỗi cooldown.
MEMORY_NUM_PREDICT = 600
# Sau từng này lần LIÊN TIẾP model trả JSON hỏng trên cùng một hội thoại thì bỏ qua lô
# đó (vẫn tiến con trỏ) thay vì kẹt vĩnh viễn. Lỗi mạng/Ollama tắt KHÔNG tính vào đây.
MAX_PARSE_FAILURES = 3
# Ước lượng thô, cố ý thấp (tiếng Việt có dấu tốn token hơn tiếng Anh) để transcript
# không vượt num_ctx — vượt là Ollama cắt input âm thầm.
_CHARS_PER_TOKEN = 2.5
_PROMPT_OVERHEAD_TOKENS = 64
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_FAILURE_UNTIL: dict[str, float] = {}
_PARSE_FAILURES: dict[str, int] = {}

# Ollama >= 0.5 nhận JSON schema ở `format` và ép output theo đúng schema (grammar),
# chặt hơn "json" trơn: model không thể bịa khoá khác hay sinh quá 8 fact.
FACTS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "maxItems": MAX_FACTS_PER_BATCH,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["key", "value", "confidence"],
            },
        },
    },
    "required": ["facts"],
}

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
    # True = Ollama CÓ trả lời nhưng nội dung không parse được (khác lỗi mạng).
    parse_failed: bool = False


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
        return MemoryExtractionResult(False, [], f"JSON không hợp lệ: {exc}", parse_failed=True)

    if not isinstance(payload, dict) or "facts" not in payload:
        return MemoryExtractionResult(
            False, [], "Thiếu object JSON có trường facts", parse_failed=True
        )
    candidates = payload.get("facts")
    if not isinstance(candidates, list):
        return MemoryExtractionResult(
            False, [], "Trường facts không phải danh sách", parse_failed=True
        )

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


def _transcript_budget_chars(system_prompt: str, num_ctx: int = CHAT_NUM_CTX) -> int:
    """Số ký tự transcript tối đa để system + transcript + output vừa num_ctx."""
    system_tokens = int(len(system_prompt) / _CHARS_PER_TOKEN)
    free_tokens = num_ctx - MEMORY_NUM_PREDICT - system_tokens - _PROMPT_OVERHEAD_TOKENS
    return max(500, int(free_tokens * _CHARS_PER_TOKEN))


def build_memory_transcript(messages: list[dict], budget_chars: int) -> str:
    """Ghép transcript, giữ các dòng MỚI NHẤT vừa ngân sách, cắt theo ranh giới message.

    Bản cũ dùng [:7000] — giữ 7000 ký tự ĐẦU — đúng kiểu cắt AGENTS.md mục 7 cấm:
    phần mới nhất của lô bị vứt mà con trỏ vẫn tiến qua nó, nên các message đó không
    bao giờ được đọc. Thêm nữa 7000 ký tự vượt context 2K mà extractor dùng.
    Nay ngân sách tính từ num_ctx; lô vẫn dài hơn thì bỏ vài message CŨ NHẤT của lô
    (có in cảnh báo) — fact mới nhất mới là thứ đáng giữ.
    """
    lines = [
        f"{item.get('role', '?')}: {item.get('content', '')}"
        for item in messages
        if item.get("content_type", "text") == "text"
    ]
    kept: list[str] = []
    budget = budget_chars
    for line in reversed(lines):
        if budget - len(line) - 1 < 0:
            break
        kept.append(line)
        budget -= len(line) + 1
    if not kept and lines:
        # Một message duy nhất dài hơn cả ngân sách: vẫn gửi phần cuối của nó.
        return lines[-1][-budget_chars:]
    if len(kept) < len(lines):
        print(f"⚠️ Lô trí nhớ dài hơn context: bỏ {len(lines) - len(kept)} message cũ nhất")
    return "\n".join(reversed(kept))


def _format_schema_rejected(response) -> bool:
    """True nếu Ollama trả 400 vì không hiểu JSON schema trong trường `format`."""
    if getattr(response, "status_code", 200) != 400:
        return False
    return "format" in str(getattr(response, "text", "")).lower()


def extract_memory_facts(messages: list[dict]) -> MemoryExtractionResult:
    """Gọi model chat đang có để trích tối đa 8 fact ổn định về người dùng."""
    system = """Bạn là bộ trích xuất trí nhớ cho ứng dụng AI companion local.
Chỉ lưu điều NGƯỜI DÙNG nói rõ và có ích ở những lần trò chuyện sau: tên muốn
được gọi, sở thích/ghét, nghề nghiệp, mục tiêu, sự kiện đang diễn ra, cách xưng hô,
lời hứa hoặc ranh giới. Không suy diễn từ lời nhân vật. Không lưu mật khẩu, mã OTP,
API key, tài khoản ngân hàng, giấy tờ định danh hay địa chỉ chính xác.

Trả về JSON đúng dạng:
{"facts":[{"key":"snake_case_english","value":"giá trị ngắn bằng tiếng Việt","confidence":0.0}]}
Tối đa 8 fact. Nếu không có gì đáng nhớ, trả {"facts":[]}. Không markdown."""

    transcript = build_memory_transcript(messages, _transcript_budget_chars(system))
    if not transcript.strip():
        return MemoryExtractionResult(True, [])

    try:
        with httpx.Client(timeout=MEMORY_EXTRACTION_TIMEOUT_SEC) as client:
            payload = {
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": transcript},
                ],
                "format": FACTS_JSON_SCHEMA,
                "stream": False,
                "think": False,
                # num_ctx PHẢI bằng của chat (CHAT_NUM_CTX): cùng model mà khác num_ctx
                # là Ollama nạp lại model cho mỗi lô, rồi nạp lại lần nữa ở lượt chat
                # kế tiếp. Bản cũ dùng 2048 trong khi chat dùng 4096.
                "options": ollama_options({
                    "temperature": 0.1,
                    "num_ctx": CHAT_NUM_CTX,
                    "num_predict": MEMORY_NUM_PREDICT,
                }),
            }
            response = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            if _format_schema_rejected(response):
                # Ollama < 0.5 chỉ hiểu format="json", gặp JSON schema thì trả 400.
                # Bản trước không thử lại nên trên Ollama cũ không lô nào trích được
                # fact. Thử lại MỘT lần với "json" — prompt hệ thống đã mô tả đúng dạng.
                print("⚠️ Ollama không nhận JSON schema trong 'format' → thử lại với format=\"json\"")
                response = client.post(
                    f"{OLLAMA_URL}/api/chat", json={**payload, "format": "json"},
                )
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


def build_conversation_memory(
    database,
    conversation: dict,
    history,
    persona_name: str = "",
) -> str:
    """Dựng khối trí nhớ dài cho MỘT lượt chat — dùng chung cho cả hai cửa vào.

    Hàm này nằm ở đây thay vì viết lại trong main.py và app_gradio.py, vì đúng chỗ
    này là nơi dự án từng lệch pha: hai entrypoint tự dựng ngữ cảnh theo hai cách rồi
    cùng một câu cho ra hai kết quả (xem D6).

    Chặn đồng bộ: có thể gọi LLM tóm tắt. FastAPI phải bọc asyncio.to_thread.
    """
    facts = database.list_memory_facts(
        conversation["user_id"], conversation["character_id"]
    )
    fact_context = format_memory_context(facts)

    session_summary = summarize_old_history(history, persona_name)
    stored_summary = (conversation.get("memory_summary") or "").strip()
    # Ghi lại để tóm tắt SỐNG QUA restart. Trước D13 cột memory_summary được đọc
    # nhưng không nơi nào ghi, nên mở lại app là tính lại từ đầu.
    if session_summary and session_summary != stored_summary:
        database.update_memory_summary(conversation["id"], session_summary)

    # Dùng bản vừa tính, KHÔNG ghép với bản đã lưu — hai bản gần trùng nhau nên ghép
    # lại là nhét trí nhớ hai lần vào system prompt. Bản đã lưu chỉ là lưới đỡ cho lúc
    # Ollama chết (session_summary rỗng) hoặc vừa mở lại app.
    long_memory = session_summary or stored_summary
    return "\n".join(part for part in (long_memory, fact_context) if part)


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
        source_message_id = messages[-1]["id"]
        if not result.success:
            if result.parse_failed:
                failures = _PARSE_FAILURES.get(conversation_id, 0) + 1
                if failures >= MAX_PARSE_FAILURES:
                    # Cùng một lô làm model trả JSON hỏng nhiều lần liền: bỏ qua lô này
                    # để các message SAU nó còn được xử lý, thay vì kẹt vĩnh viễn ở đây.
                    database.mark_memory_processed(conversation_id, source_message_id)
                    _PARSE_FAILURES.pop(conversation_id, None)
                    _FAILURE_UNTIL.pop(conversation_id, None)
                    print(
                        f"⚠️ Memory extraction: JSON hỏng {failures} lần liền → bỏ qua lô "
                        f"{len(messages)} message ({result.error})"
                    )
                    return {"status": "skipped", "facts": 0, "error": result.error}
                _PARSE_FAILURES[conversation_id] = failures
            _FAILURE_UNTIL[conversation_id] = time.monotonic() + FAILURE_COOLDOWN_SEC
            print(f"⚠️ Memory extraction lỗi: {result.error}")
            return {"status": "error", "facts": 0, "error": result.error}

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
        _PARSE_FAILURES.pop(conversation_id, None)
        if result.facts:
            print(
                f"🧠 Đã lưu {len(result.facts)} memory fact cho conversation "
                f"{conversation_id[:8]}"
            )
        return {"status": "processed", "facts": len(result.facts)}
    finally:
        lock.release()
