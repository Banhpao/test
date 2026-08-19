from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Optional

import httpx

from yumeai.core.config import OLLAMA_URL, OLLAMA_MODEL


# ══════════════════════════════════════════════════════════════════════════
# KHỚP TỪ THEO RANH GIỚI TỪ (word boundary) — KHÔNG dùng `in` nữa.
#
# Bài học: `"hi" in text` khớp với "khi", "chi", "hiểu", "nhiều"...
#          `"đồ" in text` khớp với "đồng ý", "đồng phục", "đồi"...
# → router nhảy lung tung, người dùng thấy app "lúc hiểu lúc không".
#
# (?<!\w) ... (?!\w) = hai bên keyword không được là ký tự chữ/số.
# Python \w là Unicode-aware nên tiếng Việt có dấu vẫn được tính là chữ:
#   contains_word("khi nào em rảnh", "hi")  -> False   ("k" liền trước)
#   contains_word("hi anh", "hi")           -> True
#   contains_word("tôi đồng ý", "đồ")       -> False   ("n" liền sau)
#   contains_word("đổi đồ thể thao", "đồ")  -> True
# ══════════════════════════════════════════════════════════════════════════
def contains_word(text: str, keyword: str) -> bool:
    """True nếu `keyword` xuất hiện như một từ/cụm từ độc lập trong `text`."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    return re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", (text or "").lower()) is not None


def contains_any_word(text: str, keywords: list[str]) -> bool:
    return any(contains_word(text, k) for k in keywords)


# ── Động từ tạo ảnh. Riêng "vẽ" tự nó đã đủ nghĩa tạo ảnh. ──
CREATE_VERBS = [
    "tạo", "vẽ", "generate", "draw", "make", "create", "render", "chụp",
]

# ── Danh từ chỉ ảnh. Động từ + danh từ mới chắc chắn là yêu cầu tạo ảnh. ──
IMAGE_NOUNS = [
    "ảnh", "hình", "hình ảnh", "bức ảnh", "tấm ảnh", "bức hình",
    "selfie", "image", "picture", "photo", "pic",
]

# ── Xin ảnh mà không có động từ tạo: "cho anh xem ảnh đi", "gửi hình đi" ──
SHOW_IMAGE_HINTS = [
    "xem ảnh", "xem hình", "gửi ảnh", "gửi hình", "khoe ảnh", "khoe hình",
    "selfie", "tự sướng", "cho xem ảnh", "cho xem hình",
]

# ── Tín hiệu chat rõ ràng (chỉ dùng để tăng độ tin cậy, KHÔNG còn quyết định) ──
CHAT_HINTS = [
    "chào", "hello", "hi", "hey", "nói chuyện", "trò chuyện", "chat",
    "bạn là ai", "em là ai", "cảm ơn", "tạm biệt",
]

REMOVE_HINTS = ["xóa", "bỏ", "loại bỏ", "remove"]
REPLACE_HINTS = ["thay", "đổi", "replace", "change"]

CLOTHES_WORDS = [
    "áo", "quần", "váy", "trang phục", "quần áo", "đồ", "hoodie",
    "bikini", "swimsuit", "sportswear", "đồ thể thao", "suit", "dress",
    "áo dài", "áo khoác", "áo thun", "jacket", "pants", "shirt",
]

STYLE_HINTS = {
    "Anime": ["anime", "2d", "manga", "hoạt hình", "anime style"],
    "Realistic": ["realistic", "photorealistic", "ảnh thật", "chân thực"],
    "Semi": ["semi", "semi-realistic", "bán thực", "digital art"],
}

IDENTITY_HINTS = [
    "giữ mặt", "giữ khuôn mặt", "giữ người", "giữ nguyên người",
    "giống người trong ảnh", "same person", "keep face", "keep identity",
]

POSE_HINTS = [
    "giữ dáng", "giữ pose", "giữ tư thế", "same pose", "keep pose",
]

BACKGROUND_HINTS = [
    "giữ nền", "giữ background", "giữ phông nền", "same background", "keep background",
]

OUTFIT_REFERENCE_HINTS = [
    "mặc theo ảnh", "theo ảnh trang phục", "dựa theo ảnh quần áo",
    "outfit reference", "garment transfer",
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

    # "high" = rule khớp tín hiệu rõ ràng, không cần hỏi LLM.
    # "low"  = rơi vào nhánh mặc định, nên nhờ LLM soi lại.
    confidence: str = "high"

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
        if contains_any_word(t, keywords):
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


# Giữ tên cũ để code cũ không gãy, nhưng nay đã khớp theo ranh giới từ.
def _contains_any(text: str, keywords: list[str]) -> bool:
    return contains_any_word(text, keywords)


def _looks_like_clothes_edit(text: str) -> bool:
    t = normalize_text(text)
    has_edit = contains_any_word(t, REPLACE_HINTS + ["mặc", "thay đồ", "đổi đồ", "change outfit"])
    has_clothes = contains_any_word(t, CLOTHES_WORDS)
    return has_edit and has_clothes


def _looks_like_style_edit(text: str) -> bool:
    t = normalize_text(text)
    return contains_any_word(t, ["phong cách", "style", "anime", "realistic", "semi"])


def looks_like_create(text: str) -> tuple[bool, str]:
    """
    Có phải yêu cầu TẠO ẢNH không? Trả về (kết quả, lý do).

    Chặt hơn bản cũ: chỉ riêng động từ "tạo"/"làm" KHÔNG đủ — "tạo động lực cho anh đi"
    không phải lệnh vẽ. Phải là một trong:
      - "vẽ" (tự nó đã là tạo ảnh)
      - động từ tạo + danh từ ảnh  ("tạo ảnh", "chụp cho anh tấm hình")
      - cụm xin ảnh                ("gửi ảnh đi", "cho anh xem hình")
    """
    t = normalize_text(text)

    if contains_word(t, "vẽ") or contains_word(t, "draw"):
        return True, "draw_verb"

    if contains_any_word(t, SHOW_IMAGE_HINTS):
        return True, "show_image_request"

    if contains_any_word(t, CREATE_VERBS) and contains_any_word(t, IMAGE_NOUNS):
        return True, "create_verb+image_noun"

    return False, ""


def _looks_like_create(text: str) -> bool:
    return looks_like_create(text)[0]


def _looks_like_chat(text: str) -> bool:
    return contains_any_word(normalize_text(text), CHAT_HINTS)


def rule_plan_request(
    text: str,
    has_image: bool = False,
    has_outfit_image: bool = False,
    default_style: str = "Anime",
) -> PromptPlan:
    t = normalize_text(text)
    style = detect_style(t, default_style=default_style)

    action, target, replacement = parse_inpaint_command_simple(t)

    # ══════════════════════════════════════════════════════════════════
    # 1) KHÔNG có ảnh.
    #
    # MẶC ĐỊNH LÀ CHAT, không phải text2img.
    # Bản cũ làm ngược: câu nào không khớp hint nào thì đi vẽ ảnh — nên
    # "hôm nay bạn thế nào?", "anh nhớ em không", "em ăn cơm chưa" đều
    # bị đem đi vẽ tranh thay vì trả lời. Với app companion thì đó là
    # hành vi sai nghiêm trọng nhất.
    # ══════════════════════════════════════════════════════════════════
    if not has_image:
        is_create, why = looks_like_create(t)
        if is_create:
            return PromptPlan(
                task="text2img",
                style=style,
                is_create=True,
                confidence="high",
                reason=f"Không có ảnh + yêu cầu tạo ảnh rõ ràng ({why}).",
            )

        return PromptPlan(
            task="chat",
            style=style,
            # Có tín hiệu chat rõ ràng thì khỏi phiền LLM; còn lại để LLM soi lại
            # phòng khi người dùng xin ảnh bằng cách diễn đạt lạ.
            confidence="high" if _looks_like_chat(t) else "low",
            reason="Không có ảnh và không có yêu cầu tạo ảnh rõ ràng → trả lời như chat.",
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
            confidence="high",
            reason="Có ảnh nhân vật + ảnh trang phục → garment transfer.",
        )

    # 3) Có lệnh xóa/thay vật cụ thể
    if action:
        # nếu target liên quan trang phục → ưu tiên edit_clothes
        target_text = f"{target or ''} {replacement or ''}".lower()
        if contains_any_word(target_text, CLOTHES_WORDS):
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
                confidence="high",
                reason="Lệnh thay/xóa có liên quan quần áo → Flux edit clothes.",
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
            confidence="high",
            reason="Lệnh thay/xóa vật thể cụ thể → inpaint.",
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
            confidence="high",
            reason="Prompt giống đổi trang phục.",
        )

    # 5) Giữ mặt / identity
    if contains_any_word(t, IDENTITY_HINTS):
        return PromptPlan(
            task="identity_edit",
            style=style,
            preserve_identity=True,
            preserve_pose=True,   # identity edit luôn giữ tư thế
            preserve_background=contains_any_word(t, BACKGROUND_HINTS),
            use_flux=False,
            review_with_llm=False,
            confidence="high",
            reason="Prompt yêu cầu giữ khuôn mặt/giữ người.",
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
            confidence="high",
            reason="Prompt giống đổi phong cách/edit style.",
        )

    # 7) Có ảnh nhưng không match rule nào mạnh.
    # Người dùng có thể đang chat về bức ảnh chứ không đòi sửa nó → để LLM quyết.
    return PromptPlan(
        task="generic_edit",
        style=style,
        preserve_identity=True,
        preserve_pose=True,
        preserve_background=True,
        use_flux=True,
        review_with_llm=True,
        confidence="low",
        reason="Có ảnh nhưng không match rule mạnh → generic edit (chờ LLM soi lại).",
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
You are the planner for an AI companion app that can chat and also create/edit images.

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
- DEFAULT TO "chat". Ordinary conversation, questions, greetings, feelings, small talk
  and roleplay are ALL "chat" - even when an image is attached. The user is talking to a
  character, not operating an image tool.
- Only choose an image task when the user clearly asks for a picture to be made or changed.
- If no image and the user asks to make/draw/send a picture => text2img.
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
                    # Ép Ollama trả JSON hợp lệ — model nhỏ (1b/3b) rất hay kèm
                    # lời dẫn hoặc markdown làm json.loads gãy.
                    "format": "json",
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 220,
                    },
                },
            )
            raw = r.json().get("message", {}).get("content", "").strip()

        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        task = data.get("task", "chat")

        return PromptPlan(
            task=task,
            style=data.get("style", default_style),
            preserve_identity=bool(data.get("preserve_identity", True)),
            preserve_pose=bool(data.get("preserve_pose", True)),
            preserve_background=bool(data.get("preserve_background", True)),
            use_flux=bool(data.get("use_flux", True)),
            need_outfit_image=bool(data.get("need_outfit_image", False)),
            is_create=(task == "text2img"),
            review_with_llm=bool(data.get("review_with_llm", True)),
            confidence="high",
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
    2. chỉ nhờ LLM soi lại khi rule KHÔNG chắc (confidence="low")

    Bản cũ chỉ gọi LLM khi task ∈ {generic_edit, chat} — nghĩa là quyết định
    text2img sai không bao giờ được kiểm tra lại. Nay dùng confidence nên
    mọi nhánh mặc định (kể cả chat/text2img) đều có cơ hội được sửa.
    """
    rule_plan = rule_plan_request(
        text=text,
        has_image=has_image,
        has_outfit_image=has_outfit_image,
        default_style=default_style,
    )

    if use_llm_fallback and rule_plan.confidence == "low":
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
