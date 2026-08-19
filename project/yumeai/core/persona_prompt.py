"""
YumeAI Persona Prompt — dựng system prompt + few-shot từ hồ sơ nhân vật.

Vì sao tách riêng: model local 3–8B giữ vai kém hơn model lớn rất nhiều. Hai kỹ
thuật bù lại hiệu quả nhất là (1) system prompt CỤ THỂ, có ràng buộc rõ ràng, và
(2) few-shot — cho model xem vài lượt thoại mẫu đúng giọng nhân vật. Bản cũ không
có cái nào, chỉ có 4 dòng mô tả chung chung.

Dùng chung cho app_gradio.py và main.py để hai cửa vào không lệch giọng nhân vật.
"""


def _bullets(items) -> str:
    return "\n".join(f"- {x}" for x in items if str(x).strip())


def build_system_prompt(persona: dict, memory_summary: str = "", user_name: str = "") -> str:
    """Dựng system prompt đầy đủ từ hồ sơ nhân vật.

    memory_summary: tóm tắt những gì đã biết về người dùng / các lượt cũ đã bị cắt
                    khỏi cửa sổ lịch sử. Đây là thứ tạo cảm giác "nhớ".
    """
    # Hồ sơ kiểu cũ (chỉ có key "system") vẫn dùng được.
    if "personality" not in persona:
        base = persona.get("system", "")
        if memory_summary.strip():
            base += f"\n\nNHỮNG GÌ BẠN ĐÃ BIẾT VỀ NGƯỜI NÀY:\n{memory_summary.strip()}"
        return base

    name = persona.get("name", "?")
    parts = [
        f"Bạn là {name}, {persona.get('age', '?')} tuổi, {persona.get('role', '')}.",
        "Bạn đang nhắn tin riêng với một người bạn thân thiết.",
        "",
        "TÍNH CÁCH:",
        persona.get("personality", ""),
        "",
        "CÁCH NÓI CHUYỆN (quan trọng — phải bám sát):",
        persona.get("speech_style", ""),
    ]

    quirks = persona.get("quirks") or []
    if quirks:
        parts += ["", "THÓI QUEN RIÊNG (thỉnh thoảng thể hiện, đừng nhắc hết cùng lúc):", _bullets(quirks)]

    likes = persona.get("likes") or []
    dislikes = persona.get("dislikes") or []
    if likes or dislikes:
        parts += ["", "SỞ THÍCH:"]
        if likes:
            parts.append(f"Thích: {', '.join(likes)}")
        if dislikes:
            parts.append(f"Ghét: {', '.join(dislikes)}")

    parts += [
        "",
        "NGUYÊN TẮC BẮT BUỘC:",
        persona.get("boundaries", ""),
        "",
        "CÁCH TRẢ LỜI:",
        "- Trả lời bằng tiếng Việt tự nhiên, đúng giọng nhân vật.",
        "- Ngắn gọn như tin nhắn thật. Đừng viết thành đoạn văn dài.",
        "- Có phản ứng cảm xúc thật: vui, cáu, ngượng, lo. Đừng lúc nào cũng dễ chịu.",
        "- Được phép hỏi lại, đổi chủ đề, hoặc không đồng ý với người kia.",
        "- Nhớ và nhắc lại chi tiết người kia đã kể trước đó.",
    ]

    if user_name.strip():
        parts += ["", f"Tên người bạn đang nhắn tin: {user_name.strip()}"]

    if memory_summary.strip():
        parts += [
            "",
            "NHỮNG GÌ BẠN ĐÃ BIẾT VỀ NGƯỜI NÀY (từ các lần trò chuyện trước):",
            memory_summary.strip(),
            "Hãy dùng thông tin này một cách tự nhiên, đừng liệt kê lại cho họ nghe.",
        ]

    return "\n".join(p for p in parts if p is not None).strip()


def build_fewshot_messages(persona: dict, limit: int = 4) -> list[dict]:
    """Vài lượt thoại mẫu, chèn TRƯỚC lịch sử thật để khoá giọng nhân vật.

    Với model 3B–8B đây là cách rẻ và hiệu quả nhất để chống tuột vai.
    """
    out = []
    for user_text, reply in (persona.get("examples") or [])[:limit]:
        out.append({"role": "user", "content": user_text})
        out.append({"role": "assistant", "content": reply})
    return out


def opening_line(persona: dict) -> str:
    return persona.get("greeting", "")


def persona_summary_line(persona: dict) -> str:
    """Một dòng ngắn cho UI/log."""
    return f"{persona.get('name', '?')} · {persona.get('age', '?')} · {persona.get('role', '')}"
