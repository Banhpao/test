"""
YumeAI Chat Context — dựng messages gửi Ollama, DÙNG CHUNG cho mọi entrypoint.

Trước đây app_gradio.py tự lọc lịch sử theo cách riêng còn main.py thì không có
lịch sử gì cả → cùng một nhân vật nhưng chat qua Gradio và chat qua REST API
hành xử khác hẳn nhau. File này là nguồn DUY NHẤT cho phần đó.

Hai lỗi của bản cũ được sửa ở đây:

1) Bản cũ loại bỏ mọi message bắt đầu bằng 📎 ✅ ❌ ⏳ 🧠. Vì message của người
   dùng khi có ảnh luôn bắt đầu bằng "📎 [ảnh hiện tại]", nên TOÀN BỘ lượt nói
   có kèm ảnh bị xoá khỏi ngữ cảnh → nhân vật quên sạch những đoạn đó.
   Nay: chỉ bóc phần trang trí của UI, GIỮ LẠI câu chữ thật của người dùng.

2) Bản cũ không giới hạn độ dài lịch sử → chat lâu là tràn context window và
   hỏng âm thầm (model bắt đầu trả lời lạc đề mà không báo lỗi gì).
   Nay: cắt cửa sổ theo CHAT_HISTORY_MAX_MESSAGES.
"""
import hashlib
import re

from yumeai.core.config import (
    CHAT_HISTORY_MAX_MESSAGES, CHAT_NUM_CTX, CHAT_TEMPERATURE, CHAT_TOP_P,
    CHAT_REPEAT_PENALTY, CHAT_NUM_PREDICT, CHAT_FEWSHOT, CHAT_SUMMARY,
    CHAT_SUMMARY_EVERY, CHAT_SUMMARY_MAX_MESSAGES, ollama_options,
)
from yumeai.core.persona_prompt import build_system_prompt, build_fewshot_messages

# Dòng do UI tự chèn vào để hiển thị, không phải lời người dùng gõ.
UI_DECORATION_PREFIXES = (
    "📎 [",      # 📎 [ảnh hiện tại]
    "👕 [",      # 👕 [dùng ảnh trang phục tham chiếu]
    "🔎 ",       # 🔎 current=... | outfit=...
    "✨ [",      # ✨ [auto đổi đồ]
    "[mặc theo ảnh",
    "[vùng]",
    "[region]",
    "[garment]",
    "[outfit_ref=",
)

# Message của assistant chỉ mang tính trạng thái/log, đưa vào ngữ cảnh chỉ tổ
# làm model bắt chước giọng máy móc.
STATUS_PREFIXES = ("⏳", "✅", "❌", "⚠️", "🧠", "🧭", "🛡️")


def strip_ui_markers(content: str) -> str:
    """Bóc các dòng trang trí của UI, giữ lại đúng câu người dùng đã gõ."""
    if not isinstance(content, str):
        return ""
    kept = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(UI_DECORATION_PREFIXES):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def _content_text(content):
    """Lấy phần chữ của content một message.

    Gradio 6 trả lịch sử Chatbot với content là DANH SÁCH khối
    [{"type": "text", "text": "..."}] chứ không còn là str. Bản cũ chỉ nhận str nên
    bỏ sạch mọi lượt cũ — nhân vật không thấy lịch sử hội thoại nào. Khối không
    phải chữ (file/ảnh) bị bỏ qua.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    if isinstance(content, (list, tuple)):
        return "\n".join(_content_text(part) for part in content if part is not None).strip()
    return ""


# Emoji: hồ sơ nhân vật cấm ("Không dùng emoji") mà qwen3:4b vẫn chèn ✖️, 🚫 cuối câu
# (chạy lại 28/10, D28). Lọc ngay trên luồng token để người dùng không thấy và DB không lưu.
# Cùng dải ký tự với luật "Không emoji" của bench_chat.py.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D]")


def strip_emoji(text: str) -> str:
    """Bỏ emoji khỏi một mẩu câu trả lời (an toàn với token bị cắt giữa emoji)."""
    return _EMOJI_RE.sub("", text or "")


def clean_history(history) -> list[dict]:
    """
    Lọc lịch sử chat của UI thành list message sạch cho Ollama.
    Nhận cả 2 dạng: [{"role":..,"content":..}] và [(user, assistant), ...];
    content có thể là str hoặc list khối {"type": "text"} của Gradio 6.
    """
    out: list[dict] = []
    for m in history or []:
        # Dạng tuple/list (Chatbot kiểu cũ của Gradio)
        if isinstance(m, (list, tuple)) and len(m) == 2:
            pairs = (("user", m[0]), ("assistant", m[1]))
        elif isinstance(m, dict):
            pairs = ((m.get("role", ""), _content_text(m.get("content", ""))),)
        else:
            continue

        for role, content in pairs:
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "assistant" and content.strip().startswith(STATUS_PREFIXES):
                continue
            text = strip_ui_markers(content)
            if text:
                out.append({"role": role, "content": text})
    return out


def build_chat_messages(system_prompt: str, history, user_text: str,
                        max_messages: int = None) -> list[dict]:
    """
    Dựng danh sách messages hoàn chỉnh gửi Ollama:
    [system] + [N lượt gần nhất đã làm sạch] + [câu vừa gõ]
    """
    limit = CHAT_HISTORY_MAX_MESSAGES if max_messages is None else int(max_messages)
    msgs = clean_history(history)
    if limit > 0:
        msgs = msgs[-limit:]
    return (
        [{"role": "system", "content": system_prompt}]
        + msgs
        + [{"role": "user", "content": (user_text or "").strip()}]
    )


def chat_options(temperature: float = None, num_predict: int = None) -> dict:
    """Options Ollama dùng chung — num_ctx phải đủ chứa cửa sổ lịch sử ở trên.

    repeat_penalty quan trọng với model nhỏ: 1b–3b rất hay lặp lại nguyên câu
    vừa nói, làm nhân vật nghe như máy hỏng.
    """
    return ollama_options({
        "temperature": CHAT_TEMPERATURE if temperature is None else temperature,
        "num_predict": CHAT_NUM_PREDICT if num_predict is None else num_predict,
        "top_p": CHAT_TOP_P,
        "repeat_penalty": CHAT_REPEAT_PENALTY,
        "num_ctx": CHAT_NUM_CTX,
    })


def history_load_limit(total_messages: int):
    """Số text message CUỐI cần đọc từ DB để dựng ngữ cảnh y hệt như khi đọc cả hội thoại.

    Trước đây mỗi lượt chat đọc TOÀN BỘ hội thoại (Gradio dùng một hội thoại không bao
    giờ kết thúc cho mỗi nhân vật), nên chi phí mỗi lượt tăng mãi theo độ dài.
    Cần: cửa sổ gần nhất (CHAT_HISTORY_MAX_MESSAGES) + cửa sổ tóm tắt
    (CHAT_SUMMARY_MAX_MESSAGES). Điểm bắt đầu được làm tròn xuống bội số của
    CHAT_SUMMARY_EVERY để ranh giới lô trong summarize_old_history() trùng với khi đọc
    cả hội thoại — lệch ranh giới là khoá cache tóm tắt đổi mỗi lượt, đúng lỗi D13.
    Trả None = phải đọc hết (cửa sổ không giới hạn).
    """
    total = max(0, int(total_messages))
    if CHAT_HISTORY_MAX_MESSAGES <= 0:
        return None
    if not CHAT_SUMMARY:
        return min(total, CHAT_HISTORY_MAX_MESSAGES)
    every = max(1, CHAT_SUMMARY_EVERY)
    bucket_end = ((total - CHAT_HISTORY_MAX_MESSAGES) // every) * every
    if bucket_end <= 0:
        return total
    start = max(0, ((bucket_end - CHAT_SUMMARY_MAX_MESSAGES) // every) * every)
    return total - start


# ══════════════════════════════════════════════════════════════════════════
# TRÍ NHỚ DÀI — nén các lượt đã rơi khỏi cửa sổ lịch sử
# ══════════════════════════════════════════════════════════════════════════
_SUMMARY_CACHE: dict = {}
# Khoá cache gắn với nội dung nên số entry tăng theo độ dài hội thoại. Cận này chỉ
# để tiến trình chạy nhiều ngày không phình bộ nhớ; vượt thì xoá sạch, lần sau tính lại.
_SUMMARY_CACHE_MAX = 256


def clear_memory_cache():
    _SUMMARY_CACHE.clear()


def summarize_old_history(history, persona_name: str = "") -> str:
    """Tóm tắt phần hội thoại ĐÃ BỊ CẮT khỏi cửa sổ lịch sử.

    Không tóm tắt lại mỗi lượt (mỗi lần là một lời gọi LLM). Chỉ làm lại khi có
    thêm CHAT_SUMMARY_EVERY message nữa bị đẩy ra ngoài cửa sổ.
    """
    if not CHAT_SUMMARY:
        return ""
    cleaned = clean_history(history)
    overflow = cleaned[:-CHAT_HISTORY_MAX_MESSAGES] if CHAT_HISTORY_MAX_MESSAGES > 0 else []
    if not overflow:
        return ""

    every = max(1, CHAT_SUMMARY_EVERY)
    bucket = len(overflow) // every
    # Chưa đủ một lô CHAT_SUMMARY_EVERY thì chưa tóm tắt. Không mất gì: mấy message
    # đó sẽ nằm trong cửa sổ ngay khi lô đầu tiên đầy, chỉ chậm vài lượt.
    if bucket <= 0:
        return ""

    # Cửa sổ tóm tắt phải ỔN ĐỊNH trong suốt một lô, nếu không lượt nào cũng là một
    # lời gọi LLM mới. Cắt ở ranh giới lô gần nhất rồi mới giới hạn độ dài.
    #
    # Bản trước băm TOÀN BỘ overflow để làm khoá cache. Overflow đổi mỗi lượt nên
    # khoá đổi mỗi lượt → cache không bao giờ trúng. Tệ hơn:
    # summarize_conversation() cắt transcript ở 6000 ký tự nên từ khoảng lượt 50 trở
    # đi input gửi vào LLM GIỐNG HỆT NHAU, tức mỗi lượt trả giá một lời gọi LLM để
    # tính lại đúng kết quả cũ. Nay băm đúng phần thật sự được gửi đi.
    window = overflow[: bucket * every][-CHAT_SUMMARY_MAX_MESSAGES:]
    fingerprint_source = "\x1f".join(
        f"{item['role']}\x1e{item['content']}" for item in window
    )
    fingerprint = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()[:20]
    # persona_name vẫn nằm trong khoá: hai nhân vật đọc cùng một đoạn hội thoại vẫn
    # phải ra hai tóm tắt khác nhau.
    key = (persona_name, fingerprint)
    if key in _SUMMARY_CACHE:
        return _SUMMARY_CACHE[key]

    # import muộn để tránh phụ thuộc vòng và để module này nhẹ khi chỉ cần lọc lịch sử
    from yumeai.services.ollama_service import summarize_conversation
    # Có tên nhân vật thì model mới tách được nhân vật với người dùng (D25).
    summary = summarize_conversation(window, persona_name)
    # KHÔNG cache chuỗi rỗng: Ollama chết cũng trả "", cache lại là khoá cứng
    # "không có trí nhớ" cho tới hết phiên dù dịch vụ đã sống lại.
    if summary:
        # Cache chỉ để tránh gọi lại trong cùng một lô, không cần giữ mãi.
        if len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_MAX:
            _SUMMARY_CACHE.clear()
        _SUMMARY_CACHE[key] = summary
    return summary


def build_persona_messages(persona: dict, history, user_text: str, *,
                           memory_summary: str = None, user_name: str = "",
                           use_fewshot: bool = None) -> list[dict]:
    """Dựng messages ĐẦY ĐỦ cho một lượt chat nhập vai:

        [system prompt giàu chi tiết + trí nhớ dài]
        [few-shot: vài lượt mẫu đúng giọng nhân vật]
        [N lượt gần nhất đã làm sạch]
        [câu vừa gõ]

    Bản cũ chỉ có [4 dòng system] + [lịch sử] — thiếu cả few-shot lẫn trí nhớ,
    nên model nhỏ rất nhanh tuột vai và quên sạch mọi thứ ngoài 12 lượt gần nhất.
    """
    if memory_summary is None:
        memory_summary = summarize_old_history(history, persona.get("name", ""))
    if use_fewshot is None:
        use_fewshot = CHAT_FEWSHOT

    msgs = [{"role": "system",
             "content": build_system_prompt(persona, memory_summary, user_name)}]
    if use_fewshot:
        msgs += build_fewshot_messages(persona)

    recent = clean_history(history)
    if CHAT_HISTORY_MAX_MESSAGES > 0:
        recent = recent[-CHAT_HISTORY_MAX_MESSAGES:]
    msgs += recent
    msgs.append({"role": "user", "content": (user_text or "").strip()})
    return msgs
