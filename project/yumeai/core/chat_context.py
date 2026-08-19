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
from yumeai.core.config import (
    CHAT_HISTORY_MAX_MESSAGES, CHAT_NUM_CTX, CHAT_TEMPERATURE, CHAT_TOP_P,
    CHAT_REPEAT_PENALTY, CHAT_NUM_PREDICT, CHAT_FEWSHOT, CHAT_SUMMARY,
    CHAT_SUMMARY_EVERY,
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


def clean_history(history) -> list[dict]:
    """
    Lọc lịch sử chat của UI thành list message sạch cho Ollama.
    Nhận cả 2 dạng: [{"role":..,"content":..}] và [(user, assistant), ...].
    """
    out: list[dict] = []
    for m in history or []:
        # Dạng tuple/list (Chatbot kiểu cũ của Gradio)
        if isinstance(m, (list, tuple)) and len(m) == 2:
            pairs = (("user", m[0]), ("assistant", m[1]))
        elif isinstance(m, dict):
            pairs = ((m.get("role", ""), m.get("content", "")),)
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
    return {
        "temperature": CHAT_TEMPERATURE if temperature is None else temperature,
        "num_predict": CHAT_NUM_PREDICT if num_predict is None else num_predict,
        "top_p": CHAT_TOP_P,
        "repeat_penalty": CHAT_REPEAT_PENALTY,
        "num_ctx": CHAT_NUM_CTX,
    }


# ══════════════════════════════════════════════════════════════════════════
# TRÍ NHỚ DÀI — nén các lượt đã rơi khỏi cửa sổ lịch sử
# ══════════════════════════════════════════════════════════════════════════
_SUMMARY_CACHE: dict = {}


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

    bucket = len(overflow) // max(1, CHAT_SUMMARY_EVERY)
    key = (persona_name, bucket)
    if key in _SUMMARY_CACHE:
        return _SUMMARY_CACHE[key]

    # import muộn để tránh phụ thuộc vòng và để module này nhẹ khi chỉ cần lọc lịch sử
    from yumeai.services.ollama_service import summarize_conversation
    summary = summarize_conversation(overflow)
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
