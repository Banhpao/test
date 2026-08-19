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
