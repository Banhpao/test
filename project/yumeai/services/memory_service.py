"""Trí nhớ bền vững cho companion, tối ưu cho máy cấu hình thấp.

Fact extraction chạy theo lô sau khi response đã stream xong. Nó không nằm trên
critical path của chat và không cần vector database. SQLite vẫn là nguồn sự thật;
LLM chỉ đề xuất fact có cấu trúc, repository quyết định dữ liệu được lưu.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import threading
import time

import httpx

from yumeai.core.chat_context import clean_history, summarize_old_history, summary_enabled
from yumeai.core.personas import PERSONAS
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

# Bộ khoá CỐ ĐỊNH cho fact tự trích (D25) → nhãn tiếng Việt hiện ở bảng "… nhớ về bạn".
# Bản cũ để model tự đặt khoá: dùng thật 27/10 nó đẻ ra `honorific`, `relationship_style`,
# `memory_commitment`, `boundary: không muốn nhắc đến việc nuôi chó hoặc mèo`... — phần lớn là
# chuyện của chính nhân vật hoặc suy diễn. Khoá cố định vừa chặn kiểu bịa đó, vừa cho UI một
# nhãn tiếng Việt thay vì in thẳng khoá tiếng Anh. Fact người dùng TỰ thêm vẫn được đặt khoá tuỳ ý.
FACT_KEYS: dict[str, str] = {
    "name": "Tên",
    "preferred_address": "Cách xưng hô",
    "occupation": "Nghề nghiệp",
    "home_city": "Nơi sống",
    "likes": "Thích",
    "dislikes": "Không thích",
    "favorite_food_drink": "Món / đồ uống yêu thích",
    "pet": "Thú cưng",
    "family": "Gia đình",
    "goal": "Mục tiêu",
    "current_event": "Chuyện đang diễn ra",
    "promise": "Lời hứa",
    "boundary": "Không muốn nhắc tới",
}
# Khoá model hay dùng khi chạy chế độ format="json" (Ollama < 0.5 không ép được enum).
# Quy về khoá chuẩn thay vì vứt đi.
_KEY_ALIASES = {
    "user_name": "name", "nickname": "preferred_address", "honorific": "preferred_address",
    "job": "occupation", "profession": "occupation", "work": "occupation",
    "location": "home_city", "city": "home_city", "hometown": "home_city",
    "hobby": "likes", "hobbies": "likes", "interest": "likes", "interests": "likes", "like": "likes",
    "dislike": "dislikes",
    "favorite_drink": "favorite_food_drink", "favorite_food": "favorite_food_drink",
    "drink": "favorite_food_drink", "food": "favorite_food_drink",
    "pets": "pet",
    "current_activity": "current_event", "event": "current_event", "plan": "current_event",
    "promises": "promise", "boundaries": "boundary",
}

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
                    "key": {"type": "string", "enum": list(FACT_KEYS)},
                    "value": {"type": "string"},
                    # Nguyên văn câu NGƯỜI DÙNG làm bằng chứng — đối chiếu được (D25).
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["key", "value", "evidence", "confidence"],
            },
        },
    },
    "required": ["facts"],
}

# Ngưỡng đối chiếu fact với lời người dùng (D25). Đếm theo từ, bỏ hư từ.
_EVIDENCE_MIN_OVERLAP = 0.6   # bằng chứng phải khớp >= 60% từ của MỘT câu người dùng
_VALUE_MIN_OVERLAP = 0.5      # giá trị phải có >= 50% từ xuất hiện trong lời người dùng
_STOPWORDS = {
    "là", "và", "của", "có", "không", "ở", "thì", "mà", "đã", "đang", "sẽ", "một", "những",
    "các", "cho", "với", "rất", "nhé", "nha", "à", "ạ", "này", "đó", "vẫn", "rồi", "cũng",
    "lắm", "quá", "nữa", "được", "bị", "nhất",
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


def _content_words(text: str) -> set[str]:
    words = re.findall(r"\w+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _overlap(part: str, whole_words: set[str]) -> float:
    """Tỷ lệ từ (bỏ hư từ) của `part` có mặt trong `whole_words`. Không có từ nào → 0."""
    words = _content_words(part)
    if not words:
        return 0.0
    return len(words & whole_words) / len(words)


def _canonical_key(raw_key: str) -> str:
    key = _normalize_key(raw_key)
    key = _KEY_ALIASES.get(key, key)
    return key if key in FACT_KEYS else ""


_QUESTION_END = re.compile(r"(?<!\w)(không|chưa|gì|sao|à|hả|nhỉ|chứ|nào|ai|đâu)\W*$", re.IGNORECASE)
# Chỉ những động từ hầu như luôn là lời nhờ khi đứng đầu câu. KHÔNG có "làm", "nói", "đọc",
# "hát": "Làm kỹ sư được 3 năm", "Nói chung mình thích…", "Đọc sách là sở thích của mình"
# là câu kể về bản thân.
_REQUEST_START = re.compile(
    r"^\W*(kể|liệt kê|vẽ|giúp|cho|thử|gửi|tả|giải thích)(?!\w)", re.IGNORECASE)
_REQUEST_END = re.compile(r"(?<!\w)đi\W*$", re.IGNORECASE)


def _is_question(text: str) -> bool:
    """Câu hỏi của người dùng không phải fact về họ ("Tối nay Tokyo có mưa không?")."""
    t = (text or "").strip()
    return "?" in t or bool(_QUESTION_END.search(t))


def _is_request(text: str) -> bool:
    """Lời nhờ nhân vật làm gì ("Kể mình nghe chuyện con mèo hoang đi") không phải fact.

    27/10: chữ trong câu nhờ kể chuyện mèo hoang đủ để "likes: vẽ mưa đêm và mèo hoang"
    và "dislikes: nói về con mèo hoang" lọt qua bước đối chiếu.
    """
    t = (text or "").strip()
    return bool(_REQUEST_START.search(t) or _REQUEST_END.search(t))


def _grounded_in_user(value: str, evidence: str, user_texts: list[str],
                      persona_name: str = "") -> bool:
    """Fact có thật sự đến từ lời NGƯỜI DÙNG không (D25).

    Model nhỏ hay chép chuyện của nhân vật thành fact về người dùng ("vẽ mưa đêm và mèo
    hoang", "hoàn thành bài vẽ deadline", "boundary: không muốn nhắc đến việc nuôi chó hoặc
    mèo" — 27/10). Prompt dặn rồi vẫn sai, nên kiểm thêm bằng máy:
      - giá trị chứa tên nhân vật → không phải chuyện của người dùng ("xưng hô: Pao");
      - chỉ tính câu KHẲNG ĐỊNH của người dùng — câu hỏi hay lời nhờ nhân vật không phải fact;
      - giá trị phải dựng từ chữ của MỘT câu người dùng (gộp mọi câu thì quá lỏng: người
        dùng hỏi "mèo hoang", "mưa ở Tokyo" là đủ chữ cho fact sai lọt qua);
      - có bằng chứng thì bằng chứng phải gần nguyên văn một câu như vậy.
    Dòng của nhân vật KHÔNG bao giờ được tính.
    """
    if persona_name and re.search(rf"(?<!\w){re.escape(persona_name)}(?!\w)", value, re.IGNORECASE):
        return False
    statements = [_content_words(t) for t in user_texts if not (_is_question(t) or _is_request(t))]
    if not statements:
        return False
    if max(_overlap(value, words) for words in statements) < _VALUE_MIN_OVERLAP:
        return False
    if evidence.strip():
        if max(_overlap(evidence, words) for words in statements) < _EVIDENCE_MIN_OVERLAP:
            return False
    return True


# Từ chỉ "ai" ở đầu dòng tóm tắt ("Người dùng tên Tuấn", "Họ sống ở…") — không tính khi
# đối chiếu, vì người dùng không bao giờ tự gọi mình như vậy.
_SUMMARY_SUBJECT_WORDS = {"người", "dùng", "họ", "anh", "ấy", "chị", "bạn", "mình", "tôi", "tên"}


def ground_summary(summary: str, user_texts: list[str], persona_name: str = "") -> str:
    """Chỉ giữ dòng tóm tắt dựng được từ lời NGƯỜI DÙNG (D28).

    Prompt tóm tắt đã gọi đúng tên hai người mà qwen2.5:3b vẫn viết toàn chuyện của nhân
    vật: "- Pao đang vẽ ký hoạ ngoài trời dưới mưa", "- Pao sống ở Đà Nẵng" (sai cả hai
    phía). Tóm tắt được nhét vào prompt dưới mục "những gì đã biết về người này", nên
    lọc bằng đúng luật của fact: không chứa tên nhân vật, khớp một câu khẳng định của
    người dùng. Dòng nào không qua thì bỏ; không dòng nào qua thì trả "".
    """
    kept = []
    for line in (summary or "").splitlines():
        body = line.strip().lstrip("-•*").strip()
        if not body:
            continue
        words = [w for w in re.findall(r"\w+", body.lower()) if w not in _SUMMARY_SUBJECT_WORDS]
        if words and _grounded_in_user(" ".join(words), "", user_texts, persona_name) and not (
            persona_name and re.search(rf"(?<!\w){re.escape(persona_name)}(?!\w)", body, re.IGNORECASE)
        ):
            kept.append(f"- {body}")
        else:
            print(f"🧹 Bỏ dòng tóm tắt không dựng được từ lời người dùng: {body}")
    return "\n".join(kept)


# Khoá chung: dễ bị model ghi lại đúng điều đã có ở một khoá cụ thể hơn. Chạy lại 28/10:
# "family: có nuôi một con chó tên là Bơ" cạnh "pet: chó tên Bơ", "likes: cà phê sữa đá"
# cạnh "favorite_food_drink: cà phê sữa đá" — trong khi "Nơi sống" bị bỏ sót.
_GENERIC_KEYS = {"likes", "family"}
_SAME_THING_OVERLAP = 0.8


def _same_thing(a: str, b: str) -> bool:
    """Hai giá trị nói cùng một điều: từ có nghĩa của bên NGẮN hơn gần như nằm trọn trong bên kia."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= _SAME_THING_OVERLAP


def parse_memory_response(raw: str, user_texts: list[str] | None = None,
                          persona_name: str = "") -> MemoryExtractionResult:
    """Parse JSON bảo thủ; response sai format thì không đánh dấu đã xử lý.

    Chỉ giữ khoá trong FACT_KEYS (quy khoá quen thuộc về khoá chuẩn). Có `user_texts`
    (lời người dùng trong lô) thì bỏ luôn fact không dựng được từ lời người dùng (D25).
    """
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
    # Khoá chung xét SAU khoá cụ thể, để bản trùng ("likes: cà phê sữa đá" khi đã có
    # "favorite_food_drink: cà phê sữa đá") bị bỏ chứ không chiếm chỗ (D29).
    candidates = sorted(
        candidates[:MAX_FACTS_PER_BATCH],
        key=lambda item: _canonical_key(str(item.get("key", ""))) in _GENERIC_KEYS
        if isinstance(item, dict) else True,
    )
    for item in candidates:
        if not isinstance(item, dict):
            continue
        key = _canonical_key(str(item.get("key", "")))
        value = str(item.get("value", "")).strip()[:300]
        if not key or not value or key in seen or _is_blocked_key(key):
            continue
        if user_texts is not None and not _grounded_in_user(
            value, str(item.get("evidence", "") or ""), user_texts, persona_name
        ):
            print(f"🧠 Bỏ fact không có trong lời người dùng: {key} = {value!r}")
            continue
        if key in _GENERIC_KEYS and any(_same_thing(value, f["value"]) for f in facts):
            print(f"🧠 Bỏ fact trùng với fact cụ thể hơn: {key} = {value!r}")
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


def _speaker_label(role: str, persona_name: str = "") -> str:
    """Nhãn người nói trong transcript gửi extractor / tóm tắt.

    Bản cũ ghi "user:" / "assistant:" — model nhỏ không phân biệt được ai là ai và chép
    chuyện của nhân vật thành fact về người dùng (27/10). Nay gọi thẳng tên nhân vật.
    """
    if role == "user":
        return "NGƯỜI DÙNG"
    return f"{persona_name} (nhân vật)" if persona_name else "NHÂN VẬT"


def build_memory_transcript(messages: list[dict], budget_chars: int, persona_name: str = "") -> str:
    """Ghép transcript, giữ các dòng MỚI NHẤT vừa ngân sách, cắt theo ranh giới message.

    Bản cũ dùng [:7000] — giữ 7000 ký tự ĐẦU — đúng kiểu cắt AGENTS.md mục 7 cấm:
    phần mới nhất của lô bị vứt mà con trỏ vẫn tiến qua nó, nên các message đó không
    bao giờ được đọc. Thêm nữa 7000 ký tự vượt context 2K mà extractor dùng.
    Nay ngân sách tính từ num_ctx; lô vẫn dài hơn thì bỏ vài message CŨ NHẤT của lô
    (có in cảnh báo) — fact mới nhất mới là thứ đáng giữ.
    """
    lines = [
        f"{_speaker_label(item.get('role', '?'), persona_name)}: {item.get('content', '')}"
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


def _extraction_prompt(persona_name: str = "") -> str:
    who = persona_name or "nhân vật"
    keys = "\n".join(f"- {key}: {label}" for key, label in FACT_KEYS.items())
    return f"""Bạn là bộ trích xuất trí nhớ cho ứng dụng trò chuyện với nhân vật {who}.
Đoạn hội thoại có hai người: NGƯỜI DÙNG và {who} (nhân vật). Đây là HAI NGƯỜI KHÁC NHAU.

Chỉ ghi điều NGƯỜI DÙNG tự nói rõ về CHÍNH HỌ trong các dòng "NGƯỜI DÙNG:".
Các dòng của {who} chỉ để hiểu ngữ cảnh — TUYỆT ĐỐI không lấy chuyện, sở thích, lịch học,
thời tiết hay cảm xúc của {who} làm fact. Không suy diễn. Câu hỏi của người dùng không phải fact.
Không lưu mật khẩu, mã OTP, API key, tài khoản ngân hàng, giấy tờ định danh hay địa chỉ chính xác.

Chỉ dùng các khoá sau:
{keys}
"boundary" chỉ khi người dùng NÓI THẲNG là không muốn nhắc tới điều gì.
"current_event": việc đang hoặc sắp xảy ra trong đời NGƯỜI DÙNG — đi công tác, ốm, thi, chuyển nhà,
dự án đang làm... Giữ nguyên mốc thời gian họ nói ("mai", "tuần sau", "vài hôm").
"promise": điều người dùng hứa hoặc hẹn ("tối mai mình kể tiếp", "về sẽ mua quà").

Một câu có thể chứa nhiều điều — ghi ĐỦ từng điều, mỗi điều một khoá.
Mỗi điều chỉ ghi MỘT lần, vào khoá cụ thể nhất: con vật nuôi → "pet" (không ghi lại ở "family"),
món/đồ uống yêu thích → "favorite_food_drink" (không ghi lại ở "likes").

Ví dụ 1: NGƯỜI DÙNG: "Mình là Lan, giáo viên ở Huế."
→ {{"key":"name","value":"Lan",...}}, {{"key":"occupation","value":"giáo viên",...}}, {{"key":"home_city","value":"Huế",...}}
Ví dụ 2: NGƯỜI DÙNG: "Tuần sau mình phải đi Hà Nội họp, chắc bận lắm."
→ {{"key":"current_event","value":"tuần sau đi Hà Nội họp, sẽ bận","evidence":"Tuần sau mình phải đi Hà Nội họp, chắc bận lắm.","confidence":0.9}}

Trả về JSON đúng dạng:
{{"facts":[{{"key":"một khoá ở trên","value":"giá trị ngắn bằng tiếng Việt","evidence":"chép nguyên văn câu người dùng làm bằng chứng","confidence":0.0}}]}}
Mỗi khoá tối đa một fact. Tối đa {MAX_FACTS_PER_BATCH} fact. Không có gì đáng nhớ thì trả {{"facts":[]}}. Không markdown."""


def extract_memory_facts(messages: list[dict], persona_name: str = "") -> MemoryExtractionResult:
    """Gọi model chat đang có để trích tối đa 8 fact ổn định về người dùng."""
    system = _extraction_prompt(persona_name)

    transcript = build_memory_transcript(
        messages, _transcript_budget_chars(system), persona_name
    )
    if not transcript.strip():
        return MemoryExtractionResult(True, [])
    user_texts = [
        str(item.get("content", ""))
        for item in messages
        if item.get("role") == "user" and item.get("content_type", "text") == "text"
    ]

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
    return parse_memory_response(raw, user_texts, persona_name)


# Fact có mốc thời gian ("mai đi công tác") cần biết GHI KHI NÀO thì mới hiểu được (D29).
_DATED_KEYS = {"current_event", "promise"}


def _local_day(iso_text) -> str:
    """'2026-10-28T11:08:23+00:00' → '28/10/2026' theo giờ máy. Không đọc được thì ''."""
    try:
        moment = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%d/%m/%Y")


def format_memory_context(facts: list[dict], today: str | None = None) -> str:
    """Dạng ngắn đưa vào system prompt, giới hạn để model nhỏ không tràn context.

    Chuyện đang diễn ra và lời hứa kèm ngày ghi, và khi có chúng thì thêm một dòng
    "hôm nay" — "mai đi công tác Sài Gòn" ghi từ ba hôm trước là chuyện đã qua.
    `today` chỉ để test truyền vào; mặc định là ngày của máy.
    """
    lines = []
    dated = False
    for fact in facts[:24]:
        key = str(fact.get("fact_key", fact.get("key", ""))).strip()
        value = str(fact.get("fact_value", fact.get("value", ""))).strip()
        if key and value:
            # Nhãn tiếng Việt cho khoá chuẩn; khoá người dùng tự đặt giữ nguyên.
            label = FACT_KEYS.get(key, key)
            day = _local_day(fact.get("updated_at")) if key in _DATED_KEYS else ""
            if day:
                label = f"{label} (ghi ngày {day})"
                dated = True
            lines.append(f"- {label}: {value}")
    if dated:
        lines.append(f"(Hôm nay là {today or datetime.now().astimezone().strftime('%d/%m/%Y')}.)")
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

    # Tóm tắt tắt (mặc định từ D29) thì cũng không dùng bản đã lưu: nó không còn được
    # cập nhật nữa, và điều cần nhớ về người dùng đã nằm ở fact.
    if not summary_enabled():
        return fact_context

    # Lọc cả bản vừa tính lẫn bản đã lưu: bản lưu trước 28/10 có thể còn dòng sai.
    user_texts = [item["content"] for item in clean_history(history) if item["role"] == "user"]
    session_summary = ground_summary(summarize_old_history(history, persona_name), user_texts, persona_name)
    stored_summary = ground_summary((conversation.get("memory_summary") or "").strip(), user_texts, persona_name)
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

        persona_name = PERSONAS.get(conversation["character_id"], {}).get("name", "")
        result = extract_memory_facts(messages, persona_name)
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
