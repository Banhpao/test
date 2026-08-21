"""
YumeAI Ollama Service
- Chọn model dịch tốt nhất có sẵn
- Dịch prompt tiếng Việt sang English tags
- Gọi chat Ollama có retry
- Warmup model
- Enrich prompt khi tạo ảnh mới
"""
import time
import httpx

from yumeai.core.config import OLLAMA_URL, OLLAMA_MODEL

def _pick_translate_model():
    """Trả về (model, chắc_chắn).

    chắc_chắn=False nghĩa là KHÔNG hỏi được Ollama nên đang đoán bừa — kết quả
    này không được cache lại.
    """
    prefer = ["qwen2.5:3b", "qwen2.5:7b", "gemma2:2b", "llama3.2:3b"]
    try:
        with httpx.Client(timeout=5) as c:
            tags = c.get(f"{OLLAMA_URL}/api/tags").json().get("models", [])
            installed = [m.get("name", "") for m in tags]
            for p in prefer:
                if any(p in name for name in installed):
                    print(f"🌐 Translate model: {p}")
                    return p, True
            # Ollama TRẢ LỜI được nhưng không có model ưu tiên nào → chốt luôn.
            print(f"🌐 Translate model: {OLLAMA_MODEL} (không có model ưu tiên)")
            return OLLAMA_MODEL, True
    except Exception as e:
        print(f"🌐 Translate model: {OLLAMA_MODEL} (tạm thời — chưa hỏi được Ollama: {e})")
        return OLLAMA_MODEL, False


_TRANSLATE_MODEL_CACHE = None


def get_translate_model(force_refresh: bool = False) -> str:
    """Dò model dịch LƯỜI (lần dùng đầu tiên), và chỉ cache khi thật sự hỏi được Ollama.

    Bản cũ chạy _pick_translate_model() ngay lúc import. Nếu Ollama chưa bật khi
    khởi động app thì nó rơi về llama3.2:1b và KHOÁ CỨNG cả phiên — bật Ollama
    sau đó có qwen2.5:3b cũng vô ích, phải restart app mới nhận.
    Nay: chưa hỏi được thì lần gọi sau tự thử lại.
    """
    global _TRANSLATE_MODEL_CACHE
    if force_refresh:
        _TRANSLATE_MODEL_CACHE = None
    if _TRANSLATE_MODEL_CACHE is None:
        model, confident = _pick_translate_model()
        if confident:
            _TRANSLATE_MODEL_CACHE = model
        return model
    return _TRANSLATE_MODEL_CACHE


# CỐ Ý không còn hằng TRANSLATE_MODEL ở cấp module nữa: `from ... import TRANSLATE_MODEL`
# sẽ chốt giá trị ngay lúc import, đúng cái bug cần sửa. Dùng get_translate_model().


def translate_vi_en(vi_text: str) -> str:
    """Giữ chữ ký cũ. Muốn biết dịch có thành công không thì dùng translate_vi_en_ex."""
    return translate_vi_en_ex(vi_text)[0]


def translate_vi_en_ex(vi_text: str) -> tuple[str, bool]:
    """Dịch VI→EN. Trả về (text, ok).

    ok=False nghĩa là KHÔNG dịch được và `text` vẫn đang là tiếng Việt.
    Bản cũ chỉ trả về chuỗi nên lỗi dịch hoàn toàn im lặng: prompt tiếng Việt đi
    thẳng vào Stable Diffusion, người dùng chỉ thấy ảnh rác mà không hiểu vì sao.
    """
    system = """You are a precise Vietnamese→English translator for image prompts.
Translate ONLY what is written. Do NOT add objects, styles, settings, or concepts not in the input.
Output ONLY a short comma-separated list of English keywords (max 8). No sentences, no quotes, no explanation.

Examples:
"mặc áo dài đỏ" → red ao dai
"cười dưới nắng" → smiling, sunlight
"tạo dáng ở bãi biển" → posing, beach
"mặc đồ bơi" → swimsuit
"hai cô gái ôm nhau" → two girls, hugging
"mặc đồ thể thao" → sportswear, athletic clothes"""

    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{OLLAMA_URL}/api/chat", json={
                "model": get_translate_model(),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": vi_text}
                ],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 50}
            })
            en = r.json().get("message", {}).get("content", "").strip()
            en = en.split("\n")[0].replace('"','').replace("'","").strip()
            # Cắt bớt nếu Llama vẫn trả quá nhiều tag (giữ tối đa 8)
            parts = [p.strip() for p in en.split(",") if p.strip()]
            if len(parts) > 8:
                parts = parts[:8]
            en = ", ".join(parts)
            print(f"🔤 VI: {vi_text}")
            print(f"🔤 EN: {en}")
            if len(en) > 3:
                return en, True
            print("⚠️ Ollama trả về chuỗi rỗng/quá ngắn → giữ nguyên tiếng Việt")
            return vi_text, False
    except Exception as e:
        print(f"❌ Translation error: {e} → giữ nguyên tiếng Việt (ảnh sẽ SAI)")
        return vi_text, False

def _ollama_chat(model, system, user, *, temperature=0.2, num_predict=160,
                 timeout=30, retries=2):
    """Gọi Ollama /api/chat có RETRY (lần đầu sau idle dễ chậm/timeout).
    Trả về text (đã strip) hoặc '' nếu thất bại hết."""
    last_err = None
    for attempt in range(1, retries + 2):   # retries=2 → tối đa 3 lần
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(f"{OLLAMA_URL}/api/chat", json={
                    "model": model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": num_predict}
                })
                return r.json().get("message", {}).get("content", "").strip()
        except Exception as e:
            last_err = e
            print(f"⚠️ Ollama chat lỗi (lần {attempt}/{retries + 1}): {e}")
            time.sleep(1.2 * attempt)
    print(f"❌ Ollama chat thất bại sau {retries + 1} lần: {last_err}")
    return ""

def warmup_ollama():
    """Nạp sẵn model dịch + chat vào RAM để lần gọi đầu không chậm/timeout.
    Ollama nạp model lazy — call rỗng này 'làm nóng' trước. Chạy nền (daemon thread)."""
    for m in {get_translate_model(), OLLAMA_MODEL}:
        try:
            with httpx.Client(timeout=60) as c:
                c.post(f"{OLLAMA_URL}/api/chat", json={
                    "model": m,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "options": {"num_predict": 1}
                })
            print(f"🔥 Warmup Ollama: {m} sẵn sàng")
        except Exception as e:
            print(f"⚠️ Warmup {m} lỗi (bỏ qua): {e}")

def enrich_for_create(vi_text: str, style: str = "Anime") -> str:
    """Giữ chữ ký cũ. Cần biết thành/bại thì dùng enrich_for_create_ex."""
    return enrich_for_create_ex(vi_text, style)[0]


def enrich_for_create_ex(vi_text: str, style: str = "Anime") -> tuple[str, bool]:
    """LLM 'LÀM GIÀU' prompt — học cách ChatGPT/Gemini mở rộng câu ngắn thành mô tả chi tiết (cho TẠO ảnh mới).
    Giữ ĐÚNG nội dung người dùng nêu (chủ thể, trang phục, bối cảnh), chỉ THÊM chi tiết bổ trợ:
    ngoại hình, biểu cảm, ánh sáng, không khí, bố cục/ống kính, từ khóa chất lượng.
    Hỏng → tự lùi về dịch tag thường (translate_vi_en) → cuối cùng giữ nguyên câu gốc."""
    look = ("anime art style, clean line art, vibrant cel shading, soft anime lighting"
            if style != "Realistic"
            else "photorealistic, natural skin texture, realistic lighting, sharp focus")
    system = f"""You are an expert text-to-image prompt engineer for the Flux / Stable Diffusion image models.
Expand the user's short Vietnamese description into ONE rich, detailed English prompt.

Think about and include, when relevant: the subject and how many people, detailed appearance (face, hair, eyes, body),
clothing (be precise and KEEP any stated colors/materials), pose and facial expression, the setting / background,
lighting, mood and atmosphere, and composition / camera (shot type, angle, framing).
Add tasteful complementary detail, but DO NOT change what the user actually asked for: keep their subject, their
clothing, their setting. Never invent a different scene or different clothes than stated.
Match this visual style: {look}.

OUTPUT RULES:
- Output ONLY the final English prompt.
- Comma-separated descriptive phrases (NOT full sentences), about 30-55 words.
- No preamble, no quotes, no explanation, no line breaks, no markdown, no "Prompt:" prefix.

Examples:
"co gai toc bac dung duoi mua" -> 1girl, solo, long silver hair, detailed eyes, melancholic expression, standing in the rain, wet hair and clothes, city street at night, neon reflections on wet ground, soft rim lighting, cinematic atmosphere, depth of field, highly detailed
"co gai toc vang mac bikini do tren bai bien" -> 1girl, solo, long blonde hair, blue eyes, gentle smile, red bikini, slim figure, standing on a sunny tropical beach, turquoise sea, golden hour sunlight, gentle wind, full body shot, shallow depth of field, vibrant colors, highly detailed
"hai co gai ngoi uong tra trong quan cafe" -> 2girls, sitting at a wooden table, cozy cafe interior, holding teacups, warm relaxed expressions, casual outfits, soft window light, plants and bookshelves in background, shallow depth of field, warm color palette, highly detailed"""
    try:
        with httpx.Client(timeout=40) as c:
            r = c.post(f"{OLLAMA_URL}/api/chat", json={
                "model": get_translate_model(),
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": vi_text}],
                "stream": False,
                "options": {"temperature": 0.6, "num_predict": 220}
            })
            en = r.json().get("message", {}).get("content", "").strip()
            en = " ".join(en.splitlines())                       # gộp về 1 dòng
            en = en.replace('"', "").replace("`", "").replace("*", "").replace("#", "").strip()
            for pre in ("prompt:", "english:", "output:", "english prompt:"):
                if en.lower().startswith(pre):
                    en = en[len(pre):].strip()
            print(f"🧠 Enrich VI: {vi_text}")
            print(f"🧠 Enrich EN: {en}")
            if len(en) > 10:
                return en, True
            print("⚠️ Enrich trả về quá ngắn → lùi về dịch tag thường")
            return translate_vi_en_ex(vi_text)
    except Exception as e:
        print(f"❌ Enrich error: {e} → lùi về dịch thường")
        return translate_vi_en_ex(vi_text)



def summarize_conversation(messages: list) -> str:
    """Nén các lượt cũ thành vài gạch đầu dòng SỰ KIỆN để nhét vào system prompt.

    Đây là "trí nhớ dài" phiên bản rẻ tiền: thay vì giữ nguyên 50 lượt cũ (tràn
    context, model càng nhớ càng loạn), ta giữ lại phần cốt lõi dưới dạng fact.
    """
    if not messages:
        return ""
    lines = [
        f"{'Người dùng' if m.get('role') == 'user' else 'Nhân vật'}: {m.get('content', '')}"
        for m in messages
    ]
    # Cắt từ ĐẦU chứ không cắt từ cuối. Bản cũ dùng [:6000] — giữ 6000 ký tự ĐẦU —
    # nên khi hội thoại dài ra, phần MỚI NHẤT không bao giờ lọt vào tóm tắt và trí
    # nhớ dài đóng băng ở đoạn đầu cuộc trò chuyện, im lặng, không lỗi.
    # Cắt theo ranh giới dòng để không đưa nửa câu vào prompt.
    kept: list[str] = []
    budget = 6000
    for line in reversed(lines):
        if budget - len(line) - 1 < 0:
            break
        kept.append(line)
        budget -= len(line) + 1
    transcript = "\n".join(reversed(kept))
    if not transcript:
        # Một message duy nhất mà dài hơn cả ngân sách: vẫn phải gửi phần cuối của nó.
        transcript = lines[-1][-6000:]

    system = """Bạn là bộ nhớ của một nhân vật trong ứng dụng trò chuyện.
Đọc đoạn hội thoại và rút ra những điều ĐÁNG NHỚ về người dùng và về mối quan hệ.

CHỈ ghi những gì thực sự có trong hội thoại. TUYỆT ĐỐI không suy diễn, không bịa.
Ưu tiên: tên, tuổi, nghề, nơi ở, sở thích, chuyện đang xảy ra trong đời họ,
lời hứa đã trao đổi, chủ đề nhạy cảm cần tránh, cách xưng hô hai bên dùng.

Xuất ra tối đa 8 gạch đầu dòng tiếng Việt, mỗi dòng một ý ngắn.
Không mở đầu, không kết luận, không giải thích. Nếu không có gì đáng nhớ, trả về đúng chữ: (chưa có)"""

    out = _ollama_chat(get_translate_model(), system, transcript,
                       temperature=0.1, num_predict=300, timeout=60, retries=1)
    out = (out or "").strip()
    if not out or out.startswith("(chưa có"):
        return ""
    print(f"🧠 Memory summary:\n{out}")
    return out


# Model chat quá nhỏ thì không cách nào "thông minh" được, dù prompt có tốt đến đâu.
_TINY_MARKERS = ("0.5b", "1b", "1.5b", "tinyllama", "qwen2.5:0.5b", "smollm")
RECOMMENDED_CHAT_MODELS = [
    ("~5GB RAM trở lên", "qwen2.5:7b", "tốt nhất cho tiếng Việt trong tầm chạy được"),
    ("~5GB RAM trở lên", "llama3.1:8b", "hội thoại tự nhiên, tiếng Việt khá"),
    ("~3GB RAM", "qwen2.5:3b", "nhẹ, tiếng Việt vẫn ổn — tối thiểu nên dùng"),
]


def check_chat_model() -> str:
    """Trả về chuỗi cảnh báo nếu model chat đang dùng quá nhỏ, '' nếu ổn."""
    name = (OLLAMA_MODEL or "").lower()
    if not any(m in name for m in _TINY_MARKERS):
        return ""
    lines = [
        f"⚠️  Model chat đang dùng: {OLLAMA_MODEL} — đây là model RẤT NHỎ.",
        "   Nhân vật sẽ trả lời cụt, hay tuột vai và không giữ được mạch hội thoại.",
        "   Đây là nguyên nhân lớn nhất khiến chat có cảm giác 'ngu', lớn hơn cả prompt.",
        "   Nên đổi sang (chạy `ollama pull <tên>` rồi đặt $env:OLLAMA_MODEL):",
    ]
    for ram, model, note in RECOMMENDED_CHAT_MODELS:
        lines.append(f"     • {model:<16} {ram:<20} — {note}")
    return "\n".join(lines)
