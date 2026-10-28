"""
YumeAI Image Prompt — dựng prompt text2img, DÙNG CHUNG cho mọi entrypoint.

Trước đây app_gradio.py và main.py mỗi bên tự ghép prompt một kiểu, nên cùng một
câu người dùng gõ mà hai cửa vào cho ra hai ảnh khác nhau:

    app_gradio: "{en_tags}, {style_tags}, masterpiece, best quality, highly detailed, sharp focus"
                negative: "... extra fingers, jpeg artifacts"
                → KHÔNG có ngoại hình nhân vật

    main.py:    "{persona.appearance}, {en_tags}, masterpiece, best quality, highly detailed"
                negative: "... extra fingers, mutated hands"
                → KHÔNG có style tag

Nay cả hai gọi build_positive_prompt() / NEGATIVE_PROMPT ở đây.

Lưu ý về hướng candy.ai: `appearance` được đưa lên ĐẦU prompt là cố ý — muốn ảnh
lần nào cũng ra đúng nhân vật đó thì mô tả ngoại hình phải luôn có mặt và đứng
trước. Đây mới là bước tối thiểu; character consistency thật sự cần LoRA riêng cho
mỗi nhân vật hoặc IP-Adapter khoá khuôn mặt.
"""

import re

STYLE_TAGS = {
    "Anime": "anime style, 2d illustration, vibrant colors, clean lineart",
    "Realistic": "photorealistic, 8k, detailed skin texture, soft lighting, cinematic",
    "Semi": "semi-realistic, digital art, painterly, detailed",
}

DEFAULT_STYLE = "Anime"

QUALITY_SUFFIX = "masterpiece, best quality, highly detailed, sharp focus"

# Gộp cả "jpeg artifacts" (bản app_gradio) lẫn "mutated hands" (bản main.py).
NEGATIVE_PROMPT = (
    "worst quality, low quality, blurry, deformed, ugly, watermark, text, "
    "bad anatomy, extra fingers, mutated hands, jpeg artifacts"
)


def style_tags(style: str = DEFAULT_STYLE) -> str:
    return STYLE_TAGS.get(style, STYLE_TAGS[DEFAULT_STYLE])


def build_positive_prompt(en_tags: str, style: str = DEFAULT_STYLE,
                          appearance: str = "") -> str:
    """Ghép prompt dương theo thứ tự: ngoại hình → nội dung → style → chất lượng."""
    parts = [appearance, en_tags, style_tags(style), QUALITY_SUFFIX]
    return ", ".join(p.strip().strip(",") for p in parts if p and p.strip())


# ── Ảnh vẽ chính nhân vật (D27) ──────────────────────────────────────────────
# Model dịch không biết "Pao" là ai: dùng thật 27/10 nó viết tag `1pao` thay vì
# `1girl`. Tên nhân vật chẳng giúp gì Stable Diffusion (ngoại hình đã có trong
# persona["appearance"]), nên gỡ tên ở cả câu vào lẫn prompt ra.

def _name_pattern(persona: dict | None, suffix: str = "") -> re.Pattern | None:
    name = str((persona or {}).get("name", "")).strip()
    if not name:
        return None
    return re.compile(rf"(?<!\w){re.escape(name)}{suffix}(?!\w)", re.IGNORECASE)


def persona_neutral_text(vi_text: str, persona: dict | None) -> str:
    """"vẽ Pao đang ngồi vẽ" → "vẽ cô ấy đang ngồi vẽ" trước khi đưa cho model dịch."""
    pattern = _name_pattern(persona)
    if not pattern:
        return vi_text
    return re.sub(r"\s{2,}", " ", pattern.sub("cô ấy", vi_text)).strip()


def scrub_persona_name(en_tags: str, persona: dict | None) -> str:
    """Gỡ tên nhân vật khỏi prompt tiếng Anh: `1pao` → `1girl`, bỏ "Pao"/"Pao's"."""
    name = str((persona or {}).get("name", "")).strip()
    if not name or not en_tags:
        return en_tags
    text = re.sub(rf"(?<!\w)1\s*{re.escape(name)}(?!\w)", "1girl", en_tags, flags=re.IGNORECASE)
    text = _name_pattern(persona, r"(?:'s|’s)?").sub("", text)
    parts = [re.sub(r"\s{2,}", " ", p).strip() for p in text.split(",")]
    return ", ".join(p for p in parts if p)


def image_caption(persona: dict | None, seed: int) -> str:
    """Câu nhân vật nói kèm tấm ảnh gửi trong hội thoại. Chọn theo seed để
    cùng một ảnh luôn ra cùng một câu (dễ kiểm, không cần lưu thêm trạng thái)."""
    lines = [str(x).strip() for x in (persona or {}).get("image_lines") or [] if str(x).strip()]
    if not lines:
        return ""
    return lines[seed % len(lines)]
