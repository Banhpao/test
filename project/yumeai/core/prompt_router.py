from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Optional

import httpx

from yumeai.core.config import OLLAMA_URL, OLLAMA_MODEL


CREATE_HINTS = [
    "tạo", "vẽ", "generate", "draw", "make", "create", "render",
    "hãy vẽ", "tạo ảnh", "tạo hình", "vẽ cho", "tạo cho tôi"
]

CHAT_HINTS = [
    "chào", "hello", "hi", "nói chuyện", "trò chuyện", "chat", "bạn là ai"
]

REMOVE_HINTS = ["xóa", "bỏ", "loại bỏ", "remove"]
REPLACE_HINTS = ["thay", "đổi", "replace", "change"]

CLOTHES_WORDS = [
    "áo", "quần", "váy", "trang phục", "quần áo", "đồ", "hoodie",
    "bikini", "swimsuit", "sportswear", "đồ thể thao", "suit", "dress",
    "áo dài", "áo khoác", "áo thun", "jacket", "pants", "shirt"
]

STYLE_HINTS = {
    "Anime": ["anime", "2d", "manga", "hoạt hình", "anime style"],
    "Realistic": ["realistic", "photorealistic", "ảnh thật", "chân thực"],
    "Semi": ["semi", "semi-realistic", "bán thực", "digital art"],
}

IDENTITY_HINTS = [
    "giữ mặt", "giữ khuôn mặt", "giữ người", "giữ nguyên người",
    "giống người trong ảnh", "same person", "keep face", "keep identity"
]

POSE_HINTS = [
    "giữ dáng", "giữ pose", "giữ tư thế", "same pose", "keep pose"
]

BACKGROUND_HINTS = [
    "giữ nền", "giữ background", "giữ phông nền", "same background", "keep background"
]

OUTFIT_REFERENCE_HINTS = [
    "mặc theo ảnh", "theo ảnh trang phục", "dựa theo ảnh quần áo",
    "outfit reference", "garment transfer"
]


@dataclass
class PromptPlan:
    task: str = "chat"
    style: str = "Anime"

    # cho inpaint
    action: Optional[str] = None
    target: Optional[str] = None
    replacement: Optional[str] = None

    # policy/preset
    preserve_identity: bool = False
    preserve_pose: bool = False
    preserve_background: bool = False

    use_flux: bool = False
    need_outfit_image: bool = False
    is_create: bool = False
    review_with_llm: bool = False

    reason: str = ""
    rewritten_prompt: str = ""

    def to_dict(self):
        return asdict(self)


def normalize_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def detect_style(text: str, default_style: str = "Anime") -> str:
    t = normalize_text(text)
    for style_name, keywords in STYLE_HINTS.items():
        if any(k in t for k in keywords):
            return style_name
    return default_style


def parse_inpaint_command_simple(text: str):
    """
    Parse lệnh xóa/thay vật.
    Trả về: (action, target, replacement) hoặc (None, None, None)
    """
    t = normalize_text(text)

    # thay thế
    replace_patterns = [
        r"thay (?:thế )?(.+?) bằng (.+)",
        r"đổi (.+?) thành (.+)",
        r"replace (.+?) with (.+)",
        r"change (.+?) to (.+)",
    ]
    for pat in replace_patterns:
        m = re.search(pat, t)
        if m:
            return "replace", m.group(1).strip(), m.group(2).strip()

    # xóa
    remove_patterns = [
        r"xóa (.+?)(?:\s+ra|\s+khỏi|\s+đi|$)",
        r"bỏ (.+?)(?:\s+ra|\s+khỏi|\s+đi|$)",
        r"loại bỏ (.+?)(?:\s+ra|\s+khỏi|$)",
        r"remove (?:the )?(.+?)(?:\s+from|$)",
    ]
    for pat in remove_patterns:
        m = re.search(pat, t)
        if m:
            return "remove", m.group(1).strip(), None

    return None, None, None


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def _looks_like_clothes_edit(text: str) -> bool:
    t = normalize_text(text)
    has_edit = _contains_any(t, REPLACE_HINTS + ["mặc", "thay đồ", "đổi đồ", "change outfit"])
    has_clothes = _contains_any(t, CLOTHES_WORDS)
    return has_edit and has_clothes


def _looks_like_style_edit(text: str) -> bool:
    t = normalize_text(text)
    return _contains_any(t, ["phong cách", "style", "anime", "realistic", "semi"])


def _looks_like_create(text: str) -> bool:
    t = normalize_text(text)
    return _contains_any(t, CREATE_HINTS)


def _looks_like_chat(text: str) -> bool:
    t = normalize_text(text)
    return _contains_any(t, CHAT_HINTS)


def rule_plan_request(
    text: str,
    has_image: bool = False,
    has_outfit_image: bool = False,
    default_style: str = "Anime",
) -> PromptPlan:
    t = normalize_text(text)
    style = detect_style(t, default_style=default_style)

    action, target, replacement = parse_inpaint_command_simple(t)

    # 1) Không có ảnh
    if not has_image:
        if _looks_like_create(t):
            return PromptPlan(
                task="text2img",
                style=style,
                is_create=True,
                reason="Không có ảnh + prompt mang tính tạo ảnh mới."
            )
        if _looks_like_chat(t):
            return PromptPlan(
                task="chat",
                style=style,
                reason="Không có ảnh + prompt giống chat bình thường."
            )
        # mặc định: nếu không có ảnh mà không giống chat → xem như text2img
        return PromptPlan(
            task="text2img",
            style=style,
            is_create=True,
            reason="Không có ảnh, fallback sang text2img."
        )

    # 2) Có outfit reference riêng
    if has_image and has_outfit_image:
        return PromptPlan(
            task="garment_transfer",
            style=style,
            preserve_identity=True,
            preserve_pose=True,
            preserve_background=True,
            need_outfit_image=True,
            use_flux=True,
            review_with_llm=True,
            reason="Có ảnh nhân vật + ảnh trang phục → garment transfer."
        )

    # 3) Có lệnh xóa/thay vật cụ thể
    if action:
        # nếu target liên quan trang phục → ưu tiên edit_clothes
        target_text = f"{target or ''} {replacement or ''}".lower()
        if _contains_any(target_text, CLOTHES_WORDS):
            return PromptPlan(
                task="edit_clothes",
                style=style,
                action=action,
                target=target,
                replacement=replacement,
                preserve_identity=True,
                preserve_pose=True,
                preserve_background=True,
                use_flux=True,
                review_with_llm=True,
                reason="Lệnh thay/xóa có liên quan quần áo → Flux edit clothes."
            )

        return PromptPlan(
            task="inpaint",
            style=style,
            action=action,
            target=target,
            replacement=replacement,
            preserve_identity=True,
            preserve_pose=True,
            preserve_background=True,
            reason="Lệnh thay/xóa vật thể cụ thể → inpaint."
        )

    # 4) Edit quần áo
    if _looks_like_clothes_edit(t):
        return PromptPlan(
            task="edit_clothes",
            style=style,
            preserve_identity=True,
            preserve_pose=True,
            preserve_background=True,
            use_flux=True,
            review_with_llm=True,
            reason="Prompt giống đổi trang phục."
        )

    # 5) Giữ mặt / identity
    if _contains_any(t, IDENTITY_HINTS):
        return PromptPlan(
            task="identity_edit",
            style=style,
            preserve_identity=True,
            preserve_pose=_contains_any(t, POSE_HINTS) or True,
            preserve_background=_contains_any(t, BACKGROUND_HINTS),
            use_flux=False,
            review_with_llm=False,
            reason="Prompt yêu cầu giữ khuôn mặt/giữ người."
        )

    # 6) Style edit nói chung
    if _looks_like_style_edit(t):
        return PromptPlan(
            task="style_edit",
            style=style,
            preserve_identity=True,
            preserve_pose=True,
            preserve_background=True,
            use_flux=True,
            review_with_llm=True,
            reason="Prompt giống đổi phong cách/edit style."
        )

    # 7) Fallback generic edit
    return PromptPlan(
        task="generic_edit",
        style=style,
        preserve_identity=True,
        preserve_pose=True,
        preserve_background=True,
        use_flux=True,
        review_with_llm=True,
        reason="Có ảnh nhưng không match rule mạnh → generic edit."
    )


def _llm_plan_request(
    text: str,
    has_image: bool,
    has_outfit_image: bool,
    default_style: str = "Anime",
) -> Optional[PromptPlan]:
    """
    Dùng Ollama để suy luận JSON plan nếu rule-based chưa chắc.
    """
    system = f"""
You are the planner for an image-creation and image-editing application.

Return JSON only. No markdown. No explanation.

Allowed task values:
- chat
- text2img
- inpaint
- edit_clothes
- style_edit
- identity_edit
- garment_transfer
- generic_edit

Return JSON with this exact schema:
{{
  "task": "...",
  "style": "Anime|Realistic|Semi",
  "preserve_identity": true,
  "preserve_pose": true,
  "preserve_background": true,
  "use_flux": true,
  "need_outfit_image": false,
  "review_with_llm": true,
  "reason": "short reason"
}}

Rules:
- If no image and the user asks to make or draw an image => text2img.
- If there is an image and the user asks to remove/replace an object => inpaint.
- If there is an image and the user asks to change clothes/outfit => edit_clothes.
- If there is an image and the user asks to convert style (anime/realistic) => style_edit.
- If there is an image and the user asks to keep the same face/person => identity_edit.
- If there is an image and there is also an outfit reference image => garment_transfer.
- Prefer Flux-like editing for edit_clothes, style_edit, generic_edit and garment_transfer.
- Default style is {default_style}.
"""

    user = json.dumps({
        "user_prompt": text,
        "has_image": has_image,
        "has_outfit_image": has_outfit_image,
        "default_style": default_style,
    }, ensure_ascii=False)

    try:
        with httpx.Client(timeout=25) as c:
            r = c.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 220,
                    },
                },
            )
            raw = r.json().get("message", {}).get("content", "").strip()

        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        return PromptPlan(
            task=data.get("task", "generic_edit"),
            style=data.get("style", default_style),
            preserve_identity=bool(data.get("preserve_identity", True)),
            preserve_pose=bool(data.get("preserve_pose", True)),
            preserve_background=bool(data.get("preserve_background", True)),
            use_flux=bool(data.get("use_flux", True)),
            need_outfit_image=bool(data.get("need_outfit_image", False)),
            review_with_llm=bool(data.get("review_with_llm", True)),
            reason=data.get("reason", "LLM planner"),
        )

    except Exception as e:
        print(f"⚠️ LLM planner lỗi: {e}")
        return None


def plan_request(
    text: str,
    has_image: bool = False,
    has_outfit_image: bool = False,
    default_style: str = "Anime",
    use_llm_fallback: bool = True,
) -> dict:
    """
    Hàm chính dùng cho app:
    1. rule-based trước
    2. nếu cần thì nhờ LLM refine
    """
    rule_plan = rule_plan_request(
        text=text,
        has_image=has_image,
        has_outfit_image=has_outfit_image,
        default_style=default_style,
    )

    # Chỉ dùng LLM fallback khi cần
    if use_llm_fallback and rule_plan.task in {"generic_edit", "chat"}:
        llm_plan = _llm_plan_request(
            text=text,
            has_image=has_image,
            has_outfit_image=has_outfit_image,
            default_style=default_style,
        )
        if llm_plan is not None:
            # ưu tiên task/style của LLM, nhưng giữ action/target nếu rule có
            llm_plan.action = llm_plan.action or rule_plan.action
            llm_plan.target = llm_plan.target or rule_plan.target
            llm_plan.replacement = llm_plan.replacement or rule_plan.replacement
            return llm_plan.to_dict()

    return rule_plan.to_dict()