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
    prefer = ["qwen2.5:3b", "qwen2.5:7b", "gemma2:2b", "llama3.2:3b"]
    try:
        with httpx.Client(timeout=5) as c:
            tags = c.get(f"{OLLAMA_URL}/api/tags").json().get("models", [])
            installed = [m.get("name", "") for m in tags]
            for p in prefer:
                if any(p in name for name in installed):
                    print(f"🌐 Translate model: {p}")
                    return p
    except Exception:
        pass
    print(f"🌐 Translate model: {OLLAMA_MODEL} (mặc định)")
    return OLLAMA_MODEL

TRANSLATE_MODEL = _pick_translate_model()

def translate_vi_en(vi_text: str) -> str:
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
                "model": TRANSLATE_MODEL,
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
            return en if len(en) > 3 else vi_text
    except Exception as e:
        print(f"❌ Translation error: {e}")
        return vi_text

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
    for m in {TRANSLATE_MODEL, OLLAMA_MODEL}:
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
                "model": TRANSLATE_MODEL,
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
            return en if len(en) > 10 else translate_vi_en(vi_text)
    except Exception as e:
        print(f"❌ Enrich error: {e} → lùi về dịch thường")
        return translate_vi_en(vi_text)

