"""
YumeAI — bản TỐI GIẢN (giao diện 1 khung chat kiểu ChatGPT)
- Gõ điều bạn muốn → làm luôn. Có ảnh = SỬA ảnh (Kontext). Không ảnh = TẠO ảnh mới.
- Mọi cài đặt gom trong 1 mục "⚙️ Nâng cao" (gập lại, không cần đụng vẫn chạy).
- Có self-check + assert chống bug chạy mỗi lần khởi động.

Cài:  pip install gradio httpx pillow
Chạy: python app_min.py
"""

import gradio as gr

print(">>> YumeAI app_min — BAN DA VA GRADIO 6.0 <<<  (thay file thanh cong)")

# ── Fix A: Bypass brotli middleware (nén nhưng không sửa Content-Length)
try:
    import gradio.brotli_middleware as _bm
    async def _no_compress(self, scope, receive, send):
        await self.app(scope, receive, send)
    _bm.BrotliMiddleware.__call__ = _no_compress
except Exception: pass

# ── Fix B: Patch h11 — tắt raise lỗi Content-Length (Gradio 6.x cosmetic bug)
try:
    from h11._writers import ContentLengthWriter as _CLW
    _orig_eom = _CLW.send_eom
    def _silent_eom(self, headers, write):
        try: _orig_eom(self, headers, write)
        except Exception: pass
    _CLW.send_eom = _silent_eom
except Exception: pass

# ── Fix C: Logging filter dự phòng
import logging, threading
class _H11F(logging.Filter):
    _K = ("Content-Length", "Too little data", "Too much data")
    def filter(self, r):
        if any(k in r.getMessage() for k in self._K): return False
        if r.exc_info and r.exc_info[1] and any(k in str(r.exc_info[1]) for k in self._K): return False
        return True
def _fx():
    f = _H11F()
    for n in ("uvicorn","uvicorn.error","uvicorn.access"):
        lg = logging.getLogger(n)
        lg.filters = [x for x in lg.filters if not isinstance(x, _H11F)]
        lg.addFilter(f)
threading.Thread(target=lambda: (_fx(), __import__('time').sleep(2), _fx()), daemon=True).start()

import httpx, uuid, time, io, random, inspect
from PIL import Image

# ══════════════════════════════════
# CẤU HÌNH
# ══════════════════════════════════
OLLAMA_URL   = "http://localhost:11434"
COMFY_URL    = "http://127.0.0.1:8188"
OLLAMA_MODEL = "llama3.2:1b"


def _pick_translate_model():
    """Ưu tiên model dịch lớn hơn (chính xác hơn); fallback nếu chưa cài."""
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


# ══════════════════════════════════
# HELPERS
# ══════════════════════════════════
def translate_vi_en(vi_text: str) -> str:
    """Dịch VI→EN dạng tag ngắn (cho TẠO ảnh mới)."""
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
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": vi_text}],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 50}
            })
            en = r.json().get("message", {}).get("content", "").strip()
            en = en.split("\n")[0].replace('"','').replace("'","").strip()
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


def _fetch_model_list(node_class: str, input_field: str) -> list:
    """Lấy danh sách model từ ComfyUI object_info (hỗ trợ cả format cũ & mới 2025+)."""
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/{node_class}")
            raw = r.json().get(node_class, {}) \
                          .get("input", {}).get("required", {}) \
                          .get(input_field, None)
        candidate = raw
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict) and "options" in item:
                    candidate = item["options"]
                    break
        while isinstance(candidate, list) and len(candidate) > 0 and isinstance(candidate[0], list):
            candidate = candidate[0]
        if isinstance(candidate, str):
            candidate = [candidate]
        if not isinstance(candidate, list):
            return []
        valid_ext = (".pth", ".safetensors", ".bin", ".onnx", ".ckpt", ".pt", ".gguf")
        return [m for m in candidate if isinstance(m, str) and m.lower().endswith(valid_ext)]
    except Exception as e:
        print(f"⚠️ _fetch_model_list({node_class}) error: {e}")
        return []


SDXL_PATTERNS = ["xl", "pony", "sdxl", "illustrious", "_il", "realskin",
                 "mango", "moodypro", "_eps", "coreshift", "noobai", "animagine"]
BROKEN_PATTERNS = ["animabase", "anima_base", "unet", "diffusion_pytorch"]
STYLE_KEYWORDS = {
    "Realistic": ["cyberrealistic", "realistic", "epicrealism", "majicmix", "chillout", "deliberate"],
    "Anime":     ["anythingand", "meina", "counterfeit", "anything", "anima"],
}


def _is_sdxl(name: str) -> bool:
    nl = name.lower()
    return any(p in nl for p in SDXL_PATTERNS)


def get_checkpoint_model(style="Anime") -> str:
    """Chọn checkpoint SD 1.5 hoàn chỉnh theo style (loại SDXL/LoRA/inpaint/UNet-only)."""
    models = _fetch_model_list("CheckpointLoaderSimple", "ckpt_name")
    if not models:
        return ""
    def _ok(m):
        ml = m.lower()
        return (not _is_sdxl(m) and "lora" not in ml and "inpaint" not in ml
                and not any(b in ml for b in BROKEN_PATTERNS))
    sd15 = [m for m in models if _ok(m)]
    pool = sd15 if sd15 else ([m for m in models if "inpaint" not in m.lower()
            and not any(b in m.lower() for b in BROKEN_PATTERNS)] or models)
    for kw in STYLE_KEYWORDS.get(style, STYLE_KEYWORDS["Anime"]):
        for m in pool:
            if kw in m.lower():
                return m
    return pool[0]


def upload_image_to_comfy(pil_image: Image.Image) -> str:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    buf.seek(0)
    fname = f"yumeai_{uuid.uuid4().hex[:8]}.png"
    with httpx.Client(timeout=30) as c:
        files = {"image": (fname, buf, "image/png")}
        r = c.post(f"{COMFY_URL}/upload/image", files=files, data={"overwrite": "true"})
        if r.status_code != 200:
            raise gr.Error(f"Upload ảnh lỗi: {r.text}")
        uploaded = r.json().get("name", fname)
        print(f"📤 Uploaded: {uploaded}")
        return uploaded


def submit_workflow(workflow: dict) -> str:
    client_id = str(uuid.uuid4())
    with httpx.Client(timeout=15) as c:
        r = c.post(f"{COMFY_URL}/prompt", json={"prompt": workflow, "client_id": client_id})
        if r.status_code != 200:
            raise gr.Error(f"ComfyUI từ chối: {r.text}")
        pid = r.json()["prompt_id"]
        print(f"📤 Job: {pid}")
        return pid


def poll_and_download(prompt_id: str, progress=None, start_pct=0.3, timeout=300) -> Image.Image:
    start = time.time()
    img_info = None
    with httpx.Client(timeout=10) as c:
        while time.time() - start < timeout:
            time.sleep(2)
            elapsed = time.time() - start
            if progress:
                pct = min(start_pct + elapsed / 150, 0.92)
                progress(pct, desc=f"⏳ Đang vẽ... {elapsed:.0f}s (4GB VRAM cần thời gian)")
            try:
                hist = c.get(f"{COMFY_URL}/history/{prompt_id}").json()
                if prompt_id not in hist:
                    continue
                job = hist[prompt_id]
                if job.get("status", {}).get("status_str") == "error":
                    raise gr.Error(f"ComfyUI lỗi: {job.get('status', {}).get('messages', [])}")
                for node_out in job.get("outputs", {}).values():
                    imgs = node_out.get("images", [])
                    if imgs:
                        img_info = imgs[0]
                        print(f"✅ Done in {elapsed:.0f}s")
                        break
                if img_info:
                    break
            except gr.Error:
                raise
            except Exception as e:
                print(f"⚠️ Poll: {e}")
    if not img_info:
        raise gr.Error("Timeout — ComfyUI chạy quá lâu")
    if progress:
        progress(0.95, desc="📥 Đang tải ảnh...")
    with httpx.Client(timeout=20) as c:
        params = {"filename": img_info["filename"],
                  "subfolder": img_info.get("subfolder", ""),
                  "type": img_info.get("type", "output")}
        ir = c.get(f"{COMFY_URL}/view", params=params)
        img = Image.open(io.BytesIO(ir.content))
        print(f"🖼️ {img.size} — {len(ir.content):,} bytes")
    if progress:
        progress(1.0, desc="✅ Xong!")
    return img


def _dummy_progress(*a, **kw): pass


# ── Flux Kontext: chọn quant + LoRA thêm ──
def _pick_best_quant(files: list) -> str:
    """Chọn quant TỐT NHẤT có sẵn (Q4–Q6 ưu tiên; Q3 chỉ khi không có gì hơn).
    Nhờ vậy: tải Q4_K_M vào models/unet là app TỰ dùng thay Q3 (không cần sửa code)."""
    order = ["q6_k", "q5_k_m", "q5_k_s", "q5_1", "q5_0",
             "q4_k_m", "q4_k_s", "q4_1", "q4_0",
             "q3_k_l", "q3_k_m", "q3_k_s", "q2_k",
             "q8_0", "fp16", "bf16", "f16"]
    for kw in order:
        for f in files:
            if kw in f.lower().replace("-", "_"):
                return f
    return files[0]


_SELECTED_KONTEXT = {"unet": None}             # dropdown set; None = tự chọn quant tốt nhất
_EXTRA_LORA = {"name": None, "strength": 0.9}  # LoRA Flux thêm, chồng lên Turbo
_KONTEXT_AUTO = "(tự chọn quant tốt nhất)"
_NO_EXTRA_LORA = "(không dùng)"


def list_kontext_unets() -> list:
    return [m for m in _fetch_model_list("UnetLoaderGGUF", "unet_name") if "kontext" in m.lower()]


def list_kontext_choices() -> list:
    return [_KONTEXT_AUTO] + list_kontext_unets()


def list_all_loras() -> list:
    return _fetch_model_list("LoraLoaderModelOnly", "lora_name")


def list_extra_lora_choices() -> list:
    return [_NO_EXTRA_LORA] + list_all_loras()


def _chain_loras_into(wf, turbo_lora, lora_strength):
    """Nối Turbo LoRA + LoRA thêm (nếu có) vào input 'model' của KSampler node '50'. Sửa wf tại chỗ."""
    model_src = ["1", 0]
    nid = 10
    if turbo_lora:
        wf[str(nid)] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": model_src, "lora_name": turbo_lora,
                                   "strength_model": lora_strength}}
        model_src = [str(nid), 0]; nid += 1
    extra = _EXTRA_LORA.get("name")
    if extra:
        es = max(0.0, min(2.0, float(_EXTRA_LORA.get("strength", 1.0))))
        wf[str(nid)] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": model_src, "lora_name": extra,
                                   "strength_model": es}}
        model_src = [str(nid), 0]; nid += 1
        print(f"➕ LoRA thêm: {extra} x{es}")
    wf["50"]["inputs"]["model"] = model_src


def set_kontext_model(name):
    _SELECTED_KONTEXT["unet"] = None if (not name or name == _KONTEXT_AUTO) else name


def set_extra_lora(name, strength):
    _EXTRA_LORA["name"] = None if (not name or name == _NO_EXTRA_LORA) else name
    _EXTRA_LORA["strength"] = float(strength)


def refresh_models_lists():
    return gr.update(choices=list_kontext_choices()), gr.update(choices=list_extra_lora_choices())


def get_flux_models() -> dict:
    """Trả về {unet, t5, clip_l, vae} nếu Flux Kontext đủ model, else {}."""
    try:
        kontexts = list_kontext_unets()
        if not kontexts:
            return {}
        chosen = _SELECTED_KONTEXT.get("unet")
        kontext = chosen if (chosen and chosen in kontexts) else _pick_best_quant(kontexts)
        clips  = _fetch_model_list("DualCLIPLoader", "clip_name1")
        t5     = next((m for m in clips if "t5" in m.lower()), None)
        clip_l = next((m for m in clips if "clip_l" in m.lower()), None)
        vaes = _fetch_model_list("VAELoader", "vae_name")
        vae  = next((m for m in vaes if m.lower().startswith("ae.") or "ae." in m.lower()), None)
        if kontext and t5 and clip_l and vae:
            return {"unet": kontext, "t5": t5, "clip_l": clip_l, "vae": vae}
    except Exception as e:
        print(f"⚠️ get_flux_models error: {e}")
    return {}


def get_flux_turbo_lora() -> str:
    """Tìm LoRA tăng tốc Flux (Alimama Turbo-Alpha/Hyper/Lightning...). '' nếu không có."""
    loras = _fetch_model_list("LoraLoaderModelOnly", "lora_name")
    for kw in ("turbo-alpha", "turbo_alpha", "flux.1-turbo", "flux1-turbo",
               "alimama", "turbo", "hyper", "lightning", "8step", "8-step"):
        for m in loras:
            if kw in m.lower():
                return m
    return ""


def translate_for_kontext(vi_text: str) -> str:
    """Dịch lệnh VI → CÂU LỆNH EDIT tiếng Anh kiểu Kontext (tự nhiên, rõ, giữ phân biệt từng người)."""
    system = """Convert a Vietnamese image-editing request into ONE clear, SPECIFIC English editing instruction for the Flux Kontext image editor.
Rules:
- Start with "Change", "Replace", "Dress", "Add" or "Remove" (NEVER "Transform").
- Translate clothing PRECISELY. For a woman, "đồ bơi" = swimsuit / bikini / one-piece (NEVER "swim trunks"). "mát mẻ" = revealing/skimpy. "kín đáo" = modest/conservative. Keep stated colors and materials.
- If the request gives DIFFERENT clothes to DIFFERENT characters, KEEP them separate and name each one by a visible feature (hair color or dress color), e.g. "the blonde-haired girl ... ; the dark-haired girl ...". NEVER merge them into "both".
- End with "while keeping the same faces, hairstyle, body and pose" to preserve identity, UNLESS the request is about changing the face/hair.
- Output ONLY the instruction sentence(s), on a single line. No quotes, no explanation, no extra text.

Examples:
"cho mặc đồ bơi" → Dress them in swimsuits while keeping the same faces, hairstyle, body and pose
"nhân vật váy đỏ mặc đồ bơi mát mẻ, nhân vật váy đen mặc đồ bơi kín đáo" → The girl in the red dress wears a revealing two-piece bikini; the girl in the black dress wears a modest one-piece swimsuit; keep the same faces, hairstyle, body and pose
"cô tóc vàng mặc bikini hồng, cô tóc đen mặc áo tắm đen" → The blonde-haired girl wears a pink bikini; the dark-haired girl wears a black one-piece swimsuit; keep the same faces, hairstyle, body and pose
"cho mặc đồ thể thao đá banh" → Dress both characters in colorful soccer jerseys, shorts and cleats while keeping the same faces, hairstyle, body and pose
"đổi nền thành bãi biển" → Change the background to a sunny beach with the sea, while keeping the subjects in the exact same position and pose
"đổi tóc thành màu đỏ" → Change the hair color to bright red while keeping the same face and outfit
"xóa người phía sau" → Remove the person in the background while keeping everyone else and the scene unchanged"""
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{OLLAMA_URL}/api/chat", json={
                "model": TRANSLATE_MODEL,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": vi_text}],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 160}
            })
            en = r.json().get("message", {}).get("content", "").strip()
            en = en.split("\n")[0].replace('"', "").replace("`", "").strip()
            print(f"🧠 Kontext VI: {vi_text}")
            print(f"🧠 Kontext EN: {en}")
            return en if len(en) > 5 else f"Edit the image based on: {vi_text}"
    except Exception as e:
        print(f"❌ Kontext translate error: {e}")
        return f"Edit the image based on: {vi_text}"


def flux_kontext_fn(input_image, instruction_vi, progress=None,
                    guidance=3.5, steps=28, use_turbo=True, lora_strength=1.0):
    """SỬA ảnh bằng Flux Kontext theo lệnh (đổi đồ / đổi nền / xóa vật). Chậm trên 4GB nhưng chất lượng cao."""
    if progress: progress(0.02, desc="Kiểm tra model Flux...")
    flux = get_flux_models()
    if not flux:
        raise Exception("Flux Kontext chưa đủ model (cần GGUF + clip_l + t5 + ae trong ComfyUI)")

    guidance = max(1.0, min(6.0, float(guidance)))
    lora_strength = max(0.0, min(2.0, float(lora_strength)))

    turbo_lora = get_flux_turbo_lora() if use_turbo else ""
    if turbo_lora:
        steps = 8 if int(steps) > 12 else max(4, int(steps))
    else:
        steps = max(12, min(40, int(steps)))
        if use_turbo:
            print("⚠️ Bật Turbo nhưng chưa thấy LoRA tăng tốc — chạy bình thường (chậm). "
                  "Tải FLUX.1-Turbo-Alpha.safetensors vào models/loras để dùng.")

    if progress: progress(0.05, desc="Upload ảnh...")
    img_name = upload_image_to_comfy(input_image)

    instruction_en = translate_for_kontext(instruction_vi)
    seed = random.randint(1, 2**31)
    print(f"🧠 Flux Kontext: {flux['unet']} | guidance={guidance} | steps={steps}"
          + (f" | ⚡Turbo x{lora_strength}" if turbo_lora else ""))

    wf = {
        "1":  {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": flux["unet"]}},
        "2":  {"class_type": "DualCLIPLoader",
               "inputs": {"clip_name1": flux["clip_l"], "clip_name2": flux["t5"], "type": "flux"}},
        "3":  {"class_type": "VAELoader", "inputs": {"vae_name": flux["vae"]}},
        "20": {"class_type": "LoadImage", "inputs": {"image": img_name}},
        "21": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["20", 0]}},
        "22": {"class_type": "VAEEncode", "inputs": {"pixels": ["21", 0], "vae": ["3", 0]}},
        "30": {"class_type": "CLIPTextEncode", "inputs": {"text": instruction_en, "clip": ["2", 0]}},
        "31": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["30", 0], "latent": ["22", 0]}},
        "32": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["31", 0], "guidance": guidance}},
        "33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["30", 0]}},
        "50": {"class_type": "KSampler", "inputs": {
                   "model": ["1", 0], "positive": ["32", 0], "negative": ["33", 0],
                   "latent_image": ["22", 0], "seed": seed, "steps": steps, "cfg": 1.0,
                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "60": {"class_type": "VAEDecode", "inputs": {"samples": ["50", 0], "vae": ["3", 0]}},
        "70": {"class_type": "SaveImage", "inputs": {"images": ["60", 0], "filename_prefix": "yumeai_flux"}},
    }
    _chain_loras_into(wf, turbo_lora, lora_strength)

    if progress: progress(0.15, desc="Đang chạy Flux Kontext (rất chậm trên 4GB)...")
    pid = submit_workflow(wf)
    img = poll_and_download(pid, progress=progress, start_pct=0.15, timeout=900)
    info = (f"🧠 Đã sửa ảnh (Kontext)\n📝 {instruction_en}\n"
            f"🎚️ guidance:{guidance} | steps:{steps} | 🎲 seed:{seed}")
    return img, info


def simple_generate(prompt_vi, style="Anime", progress=None):
    """TẠO ảnh mới từ mô tả (SD 1.5, nhanh). Dùng khi không có ảnh đầu vào."""
    model = get_checkpoint_model(style=style)
    if not model:
        raise Exception("Không tìm thấy checkpoint SD trong ComfyUI/models/checkpoints")
    tags = translate_vi_en(prompt_vi)
    style_tag = {"Anime": "anime style, 2d illustration, vibrant colors, detailed",
                 "Realistic": "photorealistic, 8k, ultra detailed, cinematic lighting"}.get(style, "anime style")
    pos = f"{tags}, {style_tag}, masterpiece, best quality, highly detailed"
    neg = "worst quality, low quality, blurry, deformed, ugly, watermark, text, bad anatomy, extra fingers, missing fingers"
    seed = random.randint(1, 2**31)
    if progress: progress(0.1, desc="Chuẩn bị...")
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
                  "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                  "latent_image": ["4", 0], "seed": seed, "steps": 28, "cfg": 7.0,
                  "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "yumeai"}},
    }
    if progress: progress(0.2, desc="Đang tạo ảnh...")
    pid = submit_workflow(wf)
    img = poll_and_download(pid, progress=progress, start_pct=0.2, timeout=300)
    return img, f"🎨 Đã tạo ảnh\n📝 {tags}\n🎲 seed:{seed}"


def check_status() -> str:
    """Một dòng trạng thái gọn để hiện trên UI."""
    out = []
    try:
        with httpx.Client(timeout=4) as c: c.get(f"{COMFY_URL}/system_stats")
        out.append("🟢 ComfyUI")
    except Exception:
        out.append("🔴 ComfyUI (chưa chạy?)")
    try:
        with httpx.Client(timeout=4) as c: c.get(f"{OLLAMA_URL}/api/tags")
        out.append("🟢 Ollama")
    except Exception:
        out.append("🔴 Ollama")
    flux = get_flux_models()
    if flux:
        turbo = get_flux_turbo_lora()
        out.append(f"🟢 Kontext: {flux['unet']}" + (f" + ⚡Turbo" if turbo else " (chưa Turbo)"))
    else:
        out.append("🔴 Kontext (thiếu model)")
    return " · ".join(out)


def self_check(n_inputs=None, n_params=None) -> bool:
    """NET AN TOÀN: chạy mỗi lần khởi động — báo PASS/FAIL các mục dễ hỏng (arity, kết nối, model)."""
    print("\n" + "=" * 56 + "\n🔎 YumeAI self-check\n" + "=" * 56)
    ok = True
    if n_inputs is not None and n_params is not None:
        a = (n_inputs == n_params)
        print(f"{'✅' if a else '❌'} Gradio inputs ({n_inputs}) khớp tham số unified_chat ({n_params})")
        ok = ok and a
    for name, path, required in (("ComfyUI", "/system_stats", True), ("Ollama", "/api/tags", False)):
        url = COMFY_URL if name == "ComfyUI" else OLLAMA_URL
        try:
            with httpx.Client(timeout=4) as c: c.get(url + path)
            print(f"✅ {name} kết nối được")
        except Exception:
            print(f"⚠️ {name} chưa kết nối ({url}) — bật trước khi dùng")
            if required: ok = False
    flux = get_flux_models()
    print(f"{'✅' if flux else '⚠️'} Flux Kontext: " + (flux["unet"] if flux else "THIẾU → sửa ảnh sẽ báo lỗi"))
    if flux:
        turbo = get_flux_turbo_lora()
        print(f"{'✅' if turbo else '⚠️'} Turbo LoRA: " + (turbo if turbo else "chưa có (chạy chậm)"))
    ckpt = get_checkpoint_model()
    print(f"{'✅' if ckpt else '⚠️'} Checkpoint tạo ảnh: " + (ckpt if ckpt else "THIẾU → tạo ảnh mới sẽ báo lỗi"))
    print("=" * 56)
    print("✅ Sẵn sàng." if ok else "⚠️ Có mục cần khắc phục ở trên (app vẫn mở).")
    print("=" * 56 + "\n")
    return ok


# ══════════════════════════════════
# BỘ NÃO: 1 router duy nhất
# ══════════════════════════════════
def unified_chat(message, history, image, style, guidance, use_turbo):
    """Có ảnh → SỬA (Kontext lo hết). Không ảnh → TẠO ảnh mới."""
    history = history or []
    text = (message or "").strip()
    has_img = image is not None

    if not text and not has_img:
        yield history, None
        return

    # ── CÓ ẢNH → sửa ảnh (mặc đồ / đổi nền / xóa vật... đều qua Kontext) ──
    if has_img:
        shown = text if text else "(nâng chất lượng)"
        user_msg = {"role": "user", "content": f"🖼️ {shown}"}
        if not get_flux_models():
            yield history + [user_msg, {"role": "assistant",
                "content": "⚠️ Chưa đủ model Flux Kontext (cần GGUF + clip_l + t5 + ae). "
                           "Mở ⚙️ Nâng cao để chọn model, hoặc kiểm tra ComfyUI/models."}], None
            return
        yield history + [user_msg, {"role": "assistant",
            "content": "🧠 Đang sửa ảnh bằng Kontext... (4GB nên hơi chậm, kiên nhẫn nhé)"}], None
        try:
            img, info = flux_kontext_fn(image, text or "improve the image quality and details",
                                        _dummy_progress, guidance=guidance, steps=28, use_turbo=use_turbo)
            yield history + [user_msg, {"role": "assistant", "content": f"✅ Xong!\n{info}"}], img
        except Exception as e:
            yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi: {e}"}], None
        return

    # ── KHÔNG ẢNH → tạo ảnh mới từ mô tả ──
    user_msg = {"role": "user", "content": text}
    yield history + [user_msg, {"role": "assistant", "content": "🎨 Đang tạo ảnh..."}], None
    try:
        img, info = simple_generate(text, style, _dummy_progress)
        yield history + [user_msg, {"role": "assistant", "content": f"✅ Xong!\n{info}"}], img
    except Exception as e:
        yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi: {e}"}], None


# ══════════════════════════════════
# GIAO DIỆN (1 khung chat kiểu ChatGPT)
# ══════════════════════════════════
with gr.Blocks(title="YumeAI") as demo:
    gr.Markdown("## 🌙 YumeAI — *gõ là làm*")
    status_md = gr.Markdown(check_status())

    chatbot = gr.Chatbot(height=420, show_label=False)  # Gradio 6.0: 'messages' là mặc định, bỏ type=
    gen_image = gr.Image(label="Ảnh kết quả", height=320)

    with gr.Row():
        image_input = gr.Image(label="Ảnh để sửa (bỏ trống = tạo mới)", type="pil", height=130, scale=1)
        with gr.Column(scale=3):
            msg_input = gr.Textbox(
                show_label=False, lines=2,
                placeholder="Gõ điều bạn muốn:  «mặc cho cô tóc vàng bikini đỏ»  ·  «đổi nền thành bãi biển»  ·  «xóa người phía sau»\n"
                            "Bỏ trống ô ảnh và gõ «vẽ cô gái tóc bạc đứng dưới mưa» để TẠO ảnh mới.")
            with gr.Row():
                send_btn = gr.Button("Gửi", variant="primary", scale=4)
                clear_btn = gr.Button("🗑️ Xóa", scale=1)

    with gr.Accordion("⚙️ Nâng cao (không cần đụng vẫn chạy)", open=False):
        style = gr.Radio(["Anime", "Realistic"], value="Anime", label="Phong cách (khi TẠO ảnh mới)")
        guidance = gr.Slider(1.0, 5.0, value=3.5, step=0.1,
                             label="Độ bám lệnh Kontext — 3.5 hợp Turbo; cao hơn = đổi mạnh hơn, >4.5 dễ méo")
        use_turbo = gr.Checkbox(value=True, label="⚡ Turbo (nhanh ~5x — cần FLUX.1-Turbo-Alpha trong models/loras)")
        kontext_model_dd = gr.Dropdown(label="Model Kontext (Q4/Q5 đẹp hơn Q3)",
                                       choices=list_kontext_choices(), value=_KONTEXT_AUTO)
        extra_lora_dd = gr.Dropdown(label="LoRA thêm (Flux — style/trang phục đã train/tải)",
                                    choices=list_extra_lora_choices(), value=_NO_EXTRA_LORA)
        extra_lora_str = gr.Slider(0.0, 1.5, value=0.9, step=0.05, label="LoRA thêm strength")
        refresh_btn = gr.Button("🔄 Tải lại model/LoRA + trạng thái", size="sm")

    # ── nối sự kiện ──
    shared_inputs = [msg_input, chatbot, image_input, style, guidance, use_turbo]

    # 🔒 NET AN TOÀN (chống bug thứ tự): số input Gradio PHẢI khớp số tham số unified_chat.
    # Nếu lệch → app báo lỗi NGAY khi khởi động, không chạy mò.
    assert len(shared_inputs) == len(inspect.signature(unified_chat).parameters), (
        f"Gradio inputs ({len(shared_inputs)}) ≠ unified_chat params "
        f"({len(inspect.signature(unified_chat).parameters)}) — kiểm tra lại danh sách shared_inputs!")

    send_btn.click(unified_chat, inputs=shared_inputs, outputs=[chatbot, gen_image]).then(
        lambda: "", outputs=[msg_input])
    msg_input.submit(unified_chat, inputs=shared_inputs, outputs=[chatbot, gen_image]).then(
        lambda: "", outputs=[msg_input])
    clear_btn.click(lambda: ([], None, "", None),
                    outputs=[chatbot, gen_image, msg_input, image_input])

    kontext_model_dd.change(set_kontext_model, inputs=[kontext_model_dd])
    extra_lora_dd.change(set_extra_lora, inputs=[extra_lora_dd, extra_lora_str])
    extra_lora_str.change(set_extra_lora, inputs=[extra_lora_dd, extra_lora_str])
    refresh_btn.click(refresh_models_lists, outputs=[kontext_model_dd, extra_lora_dd]).then(
        check_status, outputs=[status_md])


if __name__ == "__main__":
    self_check(n_inputs=len(shared_inputs),
               n_params=len(inspect.signature(unified_chat).parameters))
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True,
                theme=gr.themes.Soft())