"""
YumeAI — Gradio GUI
Chat (Ollama) + Tạo ảnh + img2img + ControlNet (giữ khuôn mặt/tư thế)

Cài: pip install gradio httpx pillow
Chạy: python app_gradio.py
"""

import gradio as gr

# ── Fix A: Bypass brotli middleware (nén nhưng không sửa Content-Length)
try:
    import gradio.brotli_middleware as _bm
    async def _no_compress(self, scope, receive, send):
        await self.app(scope, receive, send)
    _bm.BrotliMiddleware.__call__ = _no_compress
except Exception: pass

# ── Fix B: Patch h11 trực tiếp – tắt raise lỗi Content-Length (Gradio 6.x cosmetic bug)
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

import httpx, json, uuid, time, io, random, os, re
from PIL import Image, ImageFilter
import numpy as np

# ══════════════════════════════════
# CẤU HÌNH
# ══════════════════════════════════
OLLAMA_URL   = "http://localhost:11434"
COMFY_URL    = "http://127.0.0.1:8188"
OLLAMA_MODEL = "llama3.2:1b"

# Model dịch — ưu tiên model lớn hơn (dịch chính xác hơn nhiều).
# Tự kiểm tra: nếu chưa cài qwen2.5:3b thì fallback về OLLAMA_MODEL.
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

# Thư mục model ComfyUI
COMFY_MODELS_DIR = r"D:\baitap\code py\formyhorny\ComfyUI\models"

PERSONA = {
    "name": "Yuki",
    "system": """Bạn là Yuki, 22 tuổi, sinh viên nghệ thuật tại Tokyo.
Tính cách tsundere: hay phủ nhận cảm xúc nhưng thực ra rất quan tâm.
Hay dùng: Baka!, H-hừ..., Không phải tao thích mày đâu nhé.
Trả lời ngắn 1-3 câu bằng tiếng Việt. KHÔNG nói bạn là AI.""",
    "appearance": "1girl, solo, anime style, beautiful detailed face, expressive eyes",
}


# ══════════════════════════════════
# HELPERS — dùng chung
# ══════════════════════════════════
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


def _fetch_model_list(node_class: str, input_field: str) -> list:
    """
    Helper chung: lấy danh sách model từ ComfyUI object_info.
    Hỗ trợ CẢ 2 format:
    - Cũ:  [["a.pth","b.pth"], {...}]  hoặc  ["a.pth"]
    - Mới: ["COMBO", {"multiselect": false, "options": ["a.pth"]}]  ← ComfyUI 2025+
    Luôn trả về list các chuỗi tên file hợp lệ.
    """
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/{node_class}")
            raw = r.json().get(node_class, {}) \
                          .get("input", {}).get("required", {}) \
                          .get(input_field, None)

        candidate = raw

        # Format MỚI: tìm dict có key "options" trong list → lấy options
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict) and "options" in item:
                    candidate = item["options"]
                    break

        # Format CŨ: bóc lớp list lồng nhau cho đến khi gặp list các string
        while isinstance(candidate, list) and len(candidate) > 0 and isinstance(candidate[0], list):
            candidate = candidate[0]

        # Nếu là string đơn → bọc thành list
        if isinstance(candidate, str):
            candidate = [candidate]

        if not isinstance(candidate, list):
            return []

        # Chỉ giữ các phần tử là string và trông giống tên file model
        valid_ext = (".pth", ".safetensors", ".bin", ".onnx", ".ckpt", ".pt", ".gguf")
        result = [m for m in candidate
                  if isinstance(m, str) and m.lower().endswith(valid_ext)]
        return result
    except Exception as e:
        print(f"⚠️ _fetch_model_list({node_class}) error: {e}")
        return []


# Pattern nhận diện SDXL (KHÔNG tương thích ControlNet/IPAdapter SD 1.5)
SDXL_PATTERNS = ["xl", "pony", "sdxl", "illustrious", "_il", "realskin",
                 "mango", "moodypro", "_eps", "coreshift", "noobai", "animagine"]

# Từ khóa ưu tiên chọn checkpoint SD 1.5 theo style
# Pattern model KHÔNG hoàn chỉnh (UNet-only, thiếu CLIP/VAE) — không dùng cho CheckpointLoaderSimple
BROKEN_PATTERNS = ["animabase", "anima_base", "unet", "diffusion_pytorch"]

STYLE_KEYWORDS = {
    "Realistic": ["cyberrealistic", "realistic", "epicrealism", "majicmix", "chillout", "deliberate"],
    "Anime":     ["anythingand", "meina", "counterfeit", "anything", "anima"],
    "Semi":      ["deliberate", "dreamshaper", "anything"],
}


def _is_sdxl(name: str) -> bool:
    """Đoán model SDXL theo tên (4GB VRAM + ControlNet SD1.5 không chạy được SDXL)"""
    nl = name.lower()
    return any(p in nl for p in SDXL_PATTERNS)


def get_checkpoint_model(style="Anime", prefer_realistic=None) -> str:
    """
    Chọn checkpoint SD 1.5 phù hợp style.
    - Loại bỏ SDXL (không tương thích ControlNet/IPAdapter SD 1.5 hiện có)
    - prefer_realistic=True (tương thích cũ) → style="Realistic"
    """
    if prefer_realistic is True:
        style = "Realistic"
    models = _fetch_model_list("CheckpointLoaderSimple", "ckpt_name")
    print(f"📦 Available models: {models}")
    if not models:
        return ""

    # Chỉ xét SD 1.5 hoàn chỉnh (loại SDXL, LoRA, inpaint, model UNet-only thiếu CLIP/VAE)
    def _ok(m):
        ml = m.lower()
        return (not _is_sdxl(m)
                and "lora" not in ml and "inpaint" not in ml
                and not any(b in ml for b in BROKEN_PATTERNS))
    sd15 = [m for m in models if _ok(m)]
    if sd15:
        pool = sd15
    else:
        # Không có SD 1.5 thường → ít nhất bỏ inpaint + model hỏng
        pool = [m for m in models if "inpaint" not in m.lower()
                and not any(b in m.lower() for b in BROKEN_PATTERNS)] or models

    # Match theo style (ưu tiên từ khóa)
    for kw in STYLE_KEYWORDS.get(style, STYLE_KEYWORDS["Anime"]):
        for m in pool:
            if kw in m.lower():
                print(f"  → [{style}] chọn: {m}")
                return m

    print(f"  → [{style}] fallback: {pool[0]}")
    return pool[0]


def get_controlnet_models() -> list:
    return _fetch_model_list("ControlNetLoader", "control_net_name")


def get_ipadapter_models() -> list:
    return _fetch_model_list("IPAdapterModelLoader", "ipadapter_file")


def get_clipvision_models() -> list:
    return _fetch_model_list("CLIPVisionLoader", "clip_name")


def pick_controlnet(is_sdxl: bool, cn_keyword="canny"):
    """
    Chọn ControlNet khớp kiến trúc.
    Trả về (model_name, is_union). is_union=True nếu là ControlNet Union (cần SetUnionControlNetType).
    """
    models = get_controlnet_models()
    if not models:
        return None, False
    if is_sdxl:
        # Ưu tiên Union (1 model cho mọi loại điều khiển)
        for m in models:
            if "union" in m.lower():
                return m, True
        # ControlNet SDXL rời theo loại
        for m in models:
            ml = m.lower()
            if ("xl" in ml or "sdxl" in ml) and cn_keyword in ml:
                return m, False
        return None, False   # không có ControlNet SDXL
    else:
        # SD 1.5
        for m in models:
            ml = m.lower()
            if "sd15" in ml and cn_keyword in ml:
                return m, False
        for m in models:
            if cn_keyword in m.lower() and not _is_sdxl(m):
                return m, False
        return models[0], False


def pick_ipadapter(is_sdxl: bool):
    """Chọn IP-Adapter khớp kiến trúc (ưu tiên model face)"""
    models = get_ipadapter_models()
    if not models:
        return None
    if is_sdxl:
        for m in models:
            ml = m.lower()
            if ("sdxl" in ml or "xl" in ml) and "face" in ml:
                return m
        for m in models:
            if "sdxl" in m.lower() or "xl" in m.lower():
                return m
        return None     # không có IP-Adapter SDXL
    else:
        for m in models:
            ml = m.lower()
            if "sd15" in ml and "face" in ml:
                return m
        for m in models:
            if "face" in m.lower() and not _is_sdxl(m):
                return m
        return next((m for m in models if not _is_sdxl(m)), models[0])


def upload_image_to_comfy(pil_image: Image.Image) -> str:
    """Upload PIL image lên ComfyUI, trả về filename"""
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
    """Gửi workflow lên ComfyUI, trả về prompt_id"""
    client_id = str(uuid.uuid4())
    with httpx.Client(timeout=15) as c:
        r = c.post(f"{COMFY_URL}/prompt",
                   json={"prompt": workflow, "client_id": client_id})
        if r.status_code != 200:
            raise gr.Error(f"ComfyUI từ chối: {r.text}")
        pid = r.json()["prompt_id"]
        print(f"📤 Job: {pid}")
        return pid


def poll_and_download(prompt_id: str, progress=None, start_pct=0.3, timeout=300) -> Image.Image:
    """Poll ComfyUI history → download ảnh → PIL Image"""
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
                hr = c.get(f"{COMFY_URL}/history/{prompt_id}")
                hist = hr.json()
                if prompt_id not in hist:
                    continue
                job = hist[prompt_id]
                if job.get("status", {}).get("status_str") == "error":
                    msgs = job.get("status", {}).get("messages", [])
                    raise gr.Error(f"ComfyUI lỗi: {msgs}")
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
                  "subfolder": img_info.get("subfolder",""),
                  "type": img_info.get("type","output")}
        ir = c.get(f"{COMFY_URL}/view", params=params)
        img = Image.open(io.BytesIO(ir.content))
        print(f"🖼️ {img.size} — {len(ir.content):,} bytes")
    if progress:
        progress(1.0, desc="✅ Xong!")
    return img


# ══════════════════════════════════
# CHAT
# ══════════════════════════════════
def chat_fn(message, history):
    if not message.strip():
        yield ""; return
    messages = [{"role": "system", "content": PERSONA["system"]}]
    for u, b in history:
        messages.append({"role": "user", "content": u})
        if b: messages.append({"role": "assistant", "content": b})
    messages.append({"role": "user", "content": message})
    partial = ""
    try:
        with httpx.Client(timeout=120) as c:
            with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL, "messages": messages,
                "stream": True, "options": {"temperature": 0.85, "num_predict": 250}
            }) as resp:
                for line in resp.iter_lines():
                    if not line: continue
                    try:
                        d = json.loads(line)
                        partial += d.get("message", {}).get("content", "")
                        yield partial
                        if d.get("done"): return
                    except: continue
    except Exception as e:
        yield f"⚠️ {e}"


# ══════════════════════════════════
# TEXT2IMG
# ══════════════════════════════════
def build_quality_workflow(model, full_prompt, neg_prompt, seed, quality, upscale, width=512, height=768, latent_source=None):
    """
    Build workflow SD với quality settings và optional upscaler.
    latent_source: None = EmptyLatentImage (text2img), tuple = (node_id, slot) cho img2img
    """
    # SDXL cần độ phân giải cao hơn (native 1024); dưới 768 sẽ méo
    if _is_sdxl(model):
        if width  < 768: width  = 768
        if height < 768: height = 768
        print(f"  [SDXL] resolution → {width}x{height}")

    # Sampler settings theo quality
    q_settings = {
        "Nhanh (20 steps)":  {"steps": 20, "cfg": 7.0, "sampler": "euler",         "scheduler": "normal"},
        "Cân bằng (28 steps)":{"steps": 28, "cfg": 7.5, "sampler": "dpm_2",         "scheduler": "karras"},
        "Cao (35 steps)":    {"steps": 35, "cfg": 8.0, "sampler": "dpm_2_ancestral","scheduler": "karras"},
        "Tốt nhất (50 steps)":{"steps":50, "cfg": 8.5, "sampler": "dpm_2_ancestral","scheduler": "karras"},
    }.get(quality, {"steps": 28, "cfg": 7.5, "sampler": "euler", "scheduler": "normal"})

    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPTextEncode",         "inputs": {"text": full_prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",         "inputs": {"text": neg_prompt,  "clip": ["1", 1]}},
    }

    # Latent source
    if latent_source:
        wf["4"] = {"class_type": "VAEEncode", "inputs": {"pixels": [latent_source[0], latent_source[1]], "vae": ["1", 2]}}
        latent_node = "4"
        denoise = 0.75
    else:
        wf["4"] = {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}
        latent_node = "4"
        denoise = 1.0

    wf["5"] = {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
        "latent_image": [latent_node, 0], "seed": seed,
        "steps":    q_settings["steps"],
        "cfg":      q_settings["cfg"],
        "sampler_name": q_settings["sampler"],
        "scheduler":    q_settings["scheduler"],
        "denoise":  denoise
    }}
    wf["6"] = {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}}

    if upscale == "2x (ESRGAN)":
        # Tự động lấy model ESRGAN có sẵn thay vì hardcode tên
        up_models = get_upscale_models()
        if up_models:
            up_m = next((m for m in up_models if "anime" in m.lower()), up_models[0])
            wf["7"]  = {"class_type": "UpscaleModelLoader",   "inputs": {"model_name": up_m}}
            wf["8"]  = {"class_type": "ImageUpscaleWithModel","inputs": {"upscale_model": ["7", 0], "image": ["6", 0]}}
            wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "yumeai_up"}}
        else:
            # Fallback nếu chưa có ESRGAN model
            print("⚠️ Chưa có ESRGAN model, bỏ qua upscale")
            wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "yumeai"}}
    elif upscale == "1.5x (Latent)":
        # Hires fix — upscale trong latent space
        wf["7"] = {"class_type": "LatentUpscale", "inputs": {
            "samples": ["5", 0], "upscale_method": "bicubic",
            "width": int(width * 1.5), "height": int(height * 1.5), "crop": "disabled"
        }}
        wf["8"] = {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["7", 0], "seed": seed + 1,
            "steps": 15, "cfg": q_settings["cfg"],
            "sampler_name": q_settings["sampler"], "scheduler": q_settings["scheduler"],
            "denoise": 0.5
        }}
        wf["9"] = {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}}
        wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "yumeai_hires"}}
    else:
        # Không upscale
        wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "yumeai"}}

    return wf


def generate_image_fn(prompt, style, quality, upscale, progress=gr.Progress()):
    if not prompt.strip():
        raise gr.Error("Hãy nhập mô tả!")

    progress(0.1, desc="🔤 Đang dịch...")
    en_tags = translate_vi_en(prompt)

    style_tags = {
        "Anime":    "anime style, 2d illustration, vibrant colors, clean lineart",
        "Realistic":"photorealistic, 8k, detailed skin texture, soft lighting, cinematic",
        "Semi":     "semi-realistic, digital art, painterly, detailed",
    }.get(style, "anime style")

    progress(0.2, desc="📦 Chuẩn bị model...")
    model = get_checkpoint_model(style=style)
    if not model: raise gr.Error("Không tìm thấy model!")
    print(f"🎨 text2img: {model} | quality={quality} | upscale={upscale}")

    full_prompt = f"{en_tags}, {style_tags}, masterpiece, best quality, highly detailed, sharp focus"
    neg_prompt  = "worst quality, low quality, blurry, deformed, ugly, watermark, text, bad anatomy, extra fingers, jpeg artifacts"
    seed = random.randint(1, 2**31)

    workflow = build_quality_workflow(model, full_prompt, neg_prompt, seed, quality, upscale)

    progress(0.3, desc="📤 Gửi lệnh...")
    pid = submit_workflow(workflow)
    img = poll_and_download(pid, progress, 0.3)
    info = f"✨ {prompt}\n🔤 {en_tags}\n🎨 {style} | ⚙️ {quality} | 🔍 {upscale}\n🎲 seed: {seed}"
    return img, info


# ══════════════════════════════════
# IMG2IMG
# ══════════════════════════════════
def img2img_fn(input_image, prompt, strength, style, quality, upscale, progress=gr.Progress()):
    if input_image is None: raise gr.Error("Upload ảnh trước!")
    if not prompt.strip():  raise gr.Error("Nhập mô tả!")

    progress(0.05, desc="🔤 Đang dịch...")
    en_tags = translate_vi_en(prompt)

    style_tags = {
        "Anime":    "anime style, 2d illustration, vibrant colors",
        "Realistic":"photorealistic, 8k, detailed, cinematic",
        "Semi":     "semi-realistic, digital art, painterly",
        "Giữ nguyên style": "",
    }.get(style, "")

    progress(0.1, desc="📤 Upload ảnh...")
    img = input_image.convert("RGB")
    img.thumbnail((768, 768))
    uploaded = upload_image_to_comfy(img)

    progress(0.2, desc="📦 Chuẩn bị model...")
    model = get_checkpoint_model(style=style)
    if not model: raise gr.Error("Không tìm thấy model!")
    print(f"🎨 img2img: {model}, strength={strength}, quality={quality}")

    all_tags = ", ".join(filter(None, [en_tags, style_tags]))
    full_prompt = f"{all_tags}, masterpiece, best quality, highly detailed, sharp focus"
    neg_prompt  = "worst quality, low quality, blurry, deformed, ugly, watermark, jpeg artifacts"
    seed = random.randint(1, 2**31)

    # Workflow img2img: node "L" load ảnh, node "V" encode latent
    base_wf = {
        "L": {"class_type": "LoadImage", "inputs": {"image": uploaded}},
    }
    quality_wf = build_quality_workflow(
        model, full_prompt, neg_prompt, seed, quality, upscale,
        latent_source=("L", 0)
    )
    # Override denoise theo strength của user
    for node in quality_wf.values():
        if node.get("class_type") == "KSampler":
            node["inputs"]["denoise"] = float(strength)
            break

    workflow = {**base_wf, **quality_wf}

    progress(0.3, desc="📤 Gửi lệnh...")
    pid = submit_workflow(workflow)
    result = poll_and_download(pid, progress, 0.3)
    info = (f"✨ {prompt}\n🔤 {en_tags}\n"
            f"💪 Strength: {strength} | 🎨 {style}\n"
            f"⚙️ {quality} | 🔍 {upscale} | 🎲 seed: {seed}")
    return result, info


# ══════════════════════════════════
# HELPERS — ReActor và Upscale
# ══════════════════════════════════
def get_reactor_available() -> bool:
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/ReActorFaceSwap")
            return "ReActorFaceSwap" in r.json()
    except:
        return False


def get_upscale_models() -> list:
    return _fetch_model_list("UpscaleModelLoader", "model_name")


# ══════════════════════════════════
# CONTROLNET + IP-ADAPTER + REACTOR + ESRGAN
# ══════════════════════════════════
def controlnet_fn(input_image, prompt, cn_mode, cn_strength, face_strength,
                  use_ipadapter, use_reactor, use_upscale, style, resolution, progress=gr.Progress()):
    """
    Pipeline đầy đủ:
    1. IP-Adapter Face  → học khuôn mặt từ ảnh ref
    2. ControlNet       → giữ cấu trúc/tư thế
    3. ReActor          → face swap chính xác
    4. ESRGAN           → upscale 2x làm nét
    """
    if input_image is None: raise gr.Error("Upload ảnh trước!")
    if not prompt.strip():  raise gr.Error("Nhập mô tả!")

    # ── Chọn checkpoint TRƯỚC để biết kiến trúc (SD1.5 vs SDXL) ──
    model = get_checkpoint_model(style=style)
    if not model: raise gr.Error("Không tìm thấy checkpoint!")
    is_sdxl = _is_sdxl(model)
    arch = "SDXL" if is_sdxl else "SD1.5"
    print(f"🎨 {model}  [{arch}]")

    # ControlNet khớp kiến trúc
    cn_keyword = {"Canny (giữ cấu trúc)":"canny","Depth (giữ chiều sâu)":"depth","OpenPose (giữ tư thế)":"openpose"}.get(cn_mode,"canny")
    cn_model, is_union = pick_controlnet(is_sdxl, cn_keyword)
    if not cn_model:
        raise gr.Error(
            f"Checkpoint '{model}' là SDXL nhưng chưa có ControlNet SDXL!\n"
            "→ Tải controlnet-union-sdxl-1.0 (xem hướng dẫn), HOẶC chọn checkpoint SD 1.5."
        )
    print(f"🎛️ ControlNet: {cn_model} (union={is_union})")

    clip_models    = get_clipvision_models()
    upscale_models = get_upscale_models()
    reactor_ok     = get_reactor_available()
    upscale_ok     = bool(upscale_models)

    # IP-Adapter khớp kiến trúc
    ip_m = pick_ipadapter(is_sdxl) if use_ipadapter else None
    if use_ipadapter and not ip_m:
        raise gr.Error(
            f"Checkpoint là {arch} nhưng chưa có IP-Adapter {arch}!\n"
            + ("→ Tải ip-adapter-plus-face_sdxl_vit-h (xem hướng dẫn), " if is_sdxl
               else "→ ")
            + "HOẶC tắt checkbox IP-Adapter."
        )
    if use_ipadapter and not clip_models:
        raise gr.Error("Chưa có CLIP Vision model!")
    if use_reactor and not reactor_ok:
        raise gr.Error("Chưa cài ReActor! Chạy lệnh git clone trong tab Hướng dẫn.")

    progress(0.05, desc="🔤 Dịch prompt...")
    en_tags = translate_vi_en(prompt)
    print(f"🔤 EN: {en_tags}")

    style_tags = {
        "Anime":     "anime style, 2d illustration, vibrant colors",
        "Realistic": "photorealistic, 8k, detailed, soft lighting, cinematic",
        "Semi":      "semi-realistic, digital art, painterly",
    }.get(style, "anime style")

    progress(0.1, desc="📤 Upload ảnh...")
    img = input_image.convert("RGB")
    w, h = img.size
    target = int(resolution)
    if is_sdxl and target < 768:
        target = 768   # SDXL xuống dưới 768 sẽ bị méo, nâng sàn lên
    scale = min(target/w, target/h)
    nw = max(64, (int(w*scale)//64)*64)
    nh = max(64, (int(h*scale)//64)*64)
    img = img.resize((nw, nh))
    uploaded = upload_image_to_comfy(img)
    print(f"📐 {nw}x{nh} (target {target}px, {arch})")

    full_prompt = (
        f"{en_tags}, {style_tags}, "
        "masterpiece, best quality, highly detailed, sharp focus, "
        "beautiful lighting, intricate details, ultra high res"
    )
    neg_prompt = (
        "worst quality, low quality, blurry, deformed, ugly, watermark, "
        "bad anatomy, extra fingers, missing fingers, mutated hands, "
        "poorly drawn face, extra limbs, missing limbs, out of frame, "
        "text, logo, signature, jpeg artifacts"
    )
    seed = random.randint(1, 2**31)

    # Sampler tốt nhất cho anime: dpmpp_2m + karras → smooth, ít nhiễu
    # Realistic: dpm_2_ancestral → nhiều chi tiết hơn
    sampler = "dpmpp_2m" if style in ("Anime", "Semi") else "dpm_2_ancestral"
    steps   = 35   # tăng từ 30 → 35 cho chi tiết tốt hơn
    cfg     = 7.5 if style == "Anime" else 8.0

    preprocessor = {"Canny (giữ cấu trúc)":"Canny","Depth (giữ chiều sâu)":"DepthAnythingV2Preprocessor","OpenPose (giữ tư thế)":"OpenposePreprocessor"}.get(cn_mode,"Canny")
    pre_inputs = {"image":["20",0]}
    if preprocessor=="Canny":
        pre_inputs.update({"low_threshold":0.4,"high_threshold":0.8,"resolution":min(nw,nh)})
    else:
        pre_inputs.update({"resolution":min(nw,nh)})

    workflow = {
        "1":  {"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":model}},
        "20": {"class_type":"LoadImage","inputs":{"image":uploaded}},
        "21": {"class_type":preprocessor,"inputs":pre_inputs},
        "22": {"class_type":"ControlNetLoader","inputs":{"control_net_name":cn_model}},
        "30": {"class_type":"CLIPTextEncode","inputs":{"text":full_prompt,"clip":["1",1]}},
        "31": {"class_type":"CLIPTextEncode","inputs":{"text":neg_prompt,"clip":["1",1]}},
        "40": {"class_type":"EmptyLatentImage","inputs":{"width":nw,"height":nh,"batch_size":1}},
    }

    # ControlNet Union (SDXL) cần SetUnionControlNetType
    cn_out = "22"
    if is_union:
        union_type = {"canny":"canny/lineart/anime_lineart/mlsd","depth":"depth","openpose":"openpose"}.get(cn_keyword,"auto")
        workflow["22b"] = {"class_type":"SetUnionControlNetType","inputs":{"control_net":["22",0],"type":union_type}}
        cn_out = "22b"

    # ControlNetApplyAdvanced — dùng cho cả SD1.5 và SDXL (xuất cả positive + negative)
    workflow["32"] = {"class_type":"ControlNetApplyAdvanced","inputs":{
        "positive":["30",0],"negative":["31",0],
        "control_net":[cn_out,0],"image":["21",0],
        "strength":float(cn_strength),"start_percent":0.0,"end_percent":1.0
    }}

    # IP-Adapter (khớp kiến trúc)
    if use_ipadapter and ip_m:
        cl_m = next((m for m in clip_models if "h" in m.lower()), clip_models[0])
        print(f"🎭 IP-Adapter: {ip_m} + {cl_m}")
        workflow["23"] = {"class_type":"IPAdapterModelLoader","inputs":{"ipadapter_file":ip_m}}
        workflow["24"] = {"class_type":"CLIPVisionLoader","inputs":{"clip_name":cl_m}}
        workflow["25"] = {"class_type":"IPAdapterAdvanced","inputs":{
            "model":["1",0],"ipadapter":["23",0],"image":["20",0],
            "clip_vision":["24",0],"weight":float(face_strength),
            "weight_type":"linear","combine_embeds":"concat",
            "start_at":0.0,"end_at":1.0,"embeds_scaling":"V only"
        }}
        sampler_model = ["25",0]
    else:
        sampler_model = ["1",0]

    # KSampler base — positive/negative từ ControlNetApplyAdvanced (node 32: slot 0 & 1)
    workflow["50"] = {"class_type":"KSampler","inputs":{
        "model":sampler_model,"positive":["32",0],"negative":["32",1],
        "latent_image":["40",0],"seed":seed,"steps":steps,"cfg":cfg,
        "sampler_name":sampler,"scheduler":"karras","denoise":1.0
    }}

    # Hi-Res Fix: nâng chi tiết THẬT bằng latent upscale + refine pass (khi bật ESRGAN/quality)
    # Đây là chìa khóa làm nét — vẽ thêm chi tiết thay vì chỉ phóng to pixel
    if use_upscale:
        hr_w = int(nw * 1.5) // 8 * 8
        hr_h = int(nh * 1.5) // 8 * 8
        workflow["51"] = {"class_type":"LatentUpscale","inputs":{
            "samples":["50",0],"upscale_method":"bislerp",
            "width":hr_w,"height":hr_h,"crop":"disabled"
        }}
        workflow["52"] = {"class_type":"KSampler","inputs":{
            "model":sampler_model,"positive":["32",0],"negative":["32",1],
            "latent_image":["51",0],"seed":seed+1,
            "steps":max(18, steps-12),"cfg":cfg,
            "sampler_name":sampler,"scheduler":"karras","denoise":0.45
        }}
        latent_out = ["52",0]
        print(f"✨ Hi-Res Fix: {nw}x{nh} → {hr_w}x{hr_h} + refine (denoise 0.45)")
    else:
        latent_out = ["50",0]

    workflow["60"] = {"class_type":"VAEDecode","inputs":{"samples":latent_out,"vae":["1",2]}}

    # ReActor face swap
    if use_reactor and reactor_ok:
        print("🔄 ReActor: face swap")
        # ReActor mới dùng swap_model + facedetection thay vì input trực tiếp
        workflow["81"] = {"class_type":"ReActorLoadFaceModel","inputs":{
            "face_model": "none"
        }}
        workflow["82"] = {"class_type":"ReActorBuildFaceModel","inputs":{
            "save_mode":   False,
            "send_only":   False,
            "face_model_name": "none",
            "compute_method":  "Mean",
            "images":          ["20", 0]   # ảnh tham chiếu để build face model
        }}
        workflow["80"] = {"class_type":"ReActorFaceSwap","inputs":{
            "enabled":                  True,
            "input_image":              ["60", 0],   # ảnh vừa generate
            "swap_model":               "inswapper_128.onnx",
            "facedetection":            "retinaface_resnet50",
            "face_restore_model":       "GFPGANv1.4.pth",
            "face_restore_visibility":  1,
            "codeformer_weight":        0.5,
            "detect_gender_input":      "no",
            "detect_gender_source":     "no",
            "input_faces_index":        "0",
            "source_faces_index":       "0",
            "console_log_level":        1,
            "face_model":               ["82", 0]
        }}
        final_node = ["80", 0]
    else:
        final_node = ["60", 0]

    # ESRGAN upscale
    if use_upscale and upscale_ok:
        up_m = next((m for m in upscale_models if "anime" in m.lower()), upscale_models[0])
        print(f"🔍 ESRGAN: {up_m}")
        workflow["90"] = {"class_type":"UpscaleModelLoader","inputs":{"model_name":up_m}}
        workflow["91"] = {"class_type":"ImageUpscaleWithModel","inputs":{"upscale_model":["90",0],"image":final_node}}
        workflow["92"] = {"class_type":"SaveImage","inputs":{"images":["91",0],"filename_prefix":"yumeai_final"}}
    elif use_upscale and not upscale_ok:
        # Chưa có ESRGAN model — dùng ImageScale thông thường thay thế
        print("⚠️ ESRGAN chưa có — dùng ImageScale 1.5x")
        workflow["90"] = {"class_type":"ImageScale","inputs":{
            "image":          final_node,
            "upscale_method": "lanczos",
            "width":          int(nw * 1.5),
            "height":         int(nh * 1.5),
            "crop":           "disabled"
        }}
        workflow["92"] = {"class_type":"SaveImage","inputs":{"images":["90",0],"filename_prefix":"yumeai_final"}}
    else:
        workflow["92"] = {"class_type":"SaveImage","inputs":{"images":final_node,"filename_prefix":"yumeai_final"}}

    modes = []
    if use_ipadapter and ip_m: modes.append("IP-Adapter")
    if use_reactor   and reactor_ok:   modes.append("ReActor")
    modes.append("ControlNet")
    if use_upscale   and upscale_ok:   modes.append("ESRGAN 2x")
    mode_desc = " + ".join(modes)

    progress(0.3, desc=f"📤 Gửi lệnh ({mode_desc})...")
    pid = submit_workflow(workflow)
    result = poll_and_download(pid, progress, 0.3)

    info = (f"✨ {prompt}\n🔤 {en_tags}\n⚙️ {mode_desc}\n"
            f"🎛️ CN:{cn_strength}"+(f" | Face:{face_strength}" if use_ipadapter else "")+
            f"\n🎨 {style} | 📐 {nw}x{nh} | 🎲 seed:{seed}")
    return result, info


# ══════════════════════════════════
# KIỂM TRA KẾT NỐI + MODELS
# ══════════════════════════════════
def check_status():
    ok_o = ok_c = False
    try:
        with httpx.Client(timeout=3) as c:
            ok_o = c.get(f"{OLLAMA_URL}/api/tags").status_code == 200
    except: pass
    try:
        with httpx.Client(timeout=3) as c:
            ok_c = c.get(f"{COMFY_URL}/system_stats").status_code == 200
    except: pass
    o  = "🟢 Ollama"  if ok_o else "🔴 Ollama"
    cf = "🟢 ComfyUI" if ok_c else "🔴 ComfyUI"
    return f"{o}  |  {cf}"


def check_controlnet_status():
    """Kiểm tra ControlNet + IP-Adapter + ESRGAN model"""
    cn   = get_controlnet_models()
    ip   = get_ipadapter_models()
    clip = get_clipvision_models()
    up   = get_upscale_models()

    lines = []
    if cn:
        lines.append("✅ ControlNet:")
        for m in cn: lines.append(f"  • {m}")
    else:
        lines.append("❌ ControlNet: chưa cài")

    if ip:
        lines.append("✅ IP-Adapter:")
        for m in ip: lines.append(f"  • {m}")
    else:
        lines.append("❌ IP-Adapter: chưa cài (cần để giữ khuôn mặt)")

    if clip:
        lines.append("✅ CLIP Vision:")
        for m in clip: lines.append(f"  • {m}")
    else:
        lines.append("❌ CLIP Vision: chưa cài (cần cho IP-Adapter)")

    if up:
        lines.append("✅ ESRGAN Upscale:")
        for m in up: lines.append(f"  • {m}")
    else:
        lines.append("⚠️  ESRGAN: chưa có → chất lượng upscale thấp")
        lines.append("   Tải: RealESRGAN_x4plus_anime_6B.pth vào models/upscale_models/")

    # Flux Kontext (edit thông minh)
    flux = get_flux_models()
    if flux:
        lines.append("✅ Flux Kontext (edit thông minh):")
        lines.append(f"  • {flux['unet']}")
    else:
        lines.append("⚠️  Flux Kontext: chưa đủ model (tùy chọn)")

    # Turbo LoRA tăng tốc (8 bước)
    turbo = get_flux_turbo_lora()
    if turbo:
        lines.append(f"✅ Turbo LoRA: {turbo}")
    else:
        lines.append("⚠️  Turbo LoRA: chưa có → tải FLUX.1-Turbo-Alpha vào models/loras (nhanh ~5x)")

    return "\n".join(lines)


# ══════════════════════════════════
# INPAINTING — Xóa / thay thế vật thể bằng text
# ══════════════════════════════════
import tempfile

def _dummy_progress(*a, **kw): pass


def get_segment_available() -> bool:
    """Kiểm tra node segment-anything (GroundingDINO+SAM) có cài không"""
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/GroundingDinoSAMSegment (segment anything)")
            return "GroundingDinoSAMSegment (segment anything)" in r.json()
    except:
        return False


def get_lama_available() -> bool:
    """Kiểm tra node LaMa (comfyui-inpaint-nodes của Acly) có cài không"""
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/INPAINT_InpaintWithModel")
            return "INPAINT_InpaintWithModel" in r.json()
    except:
        return False


def get_lama_model() -> str:
    """Tìm model LaMa trong models/inpaint"""
    models = _fetch_model_list("INPAINT_LoadInpaintModel", "model_name")
    for m in models:
        if "lama" in m.lower():
            return m
    return models[0] if models else "big-lama.pt"


def get_inpaint_model(style="Realistic") -> str:
    """Ưu tiên checkpoint inpainting (không SDXL), fallback checkpoint thường theo style"""
    models = _fetch_model_list("CheckpointLoaderSimple", "ckpt_name")
    for m in models:
        if "inpaint" in m.lower() and not _is_sdxl(m):
            return m
    return get_checkpoint_model(style=style)


# Từ điển dịch vật thể VI→EN cho GroundingDINO
# Với chi/vật dài: dùng nhiều từ cách nhau bởi "." để GroundingDINO phủ hết
OBJECT_DICT = {
    "cánh tay": "arm. hand. forearm",
    "bàn tay": "hand",
    "tay": "arm. hand",                 # mặc định phủ cả cánh tay
    "chân": "leg. foot",
    "bàn chân": "foot",
    "kính mát": "sunglasses",
    "mắt kính": "glasses",
    "kính": "glasses",
    "mũ": "hat", "nón": "hat",
    "áo khoác": "jacket",
    "áo": "shirt. clothing",
    "quần": "pants",
    "váy": "dress",
    "tóc": "hair",
    "râu": "beard",
    "khẩu trang": "face mask",
    "mặt nạ": "mask",
    "người phía sau": "person in background",
    "người đằng sau": "person in background",
    "người": "person",
    "phông nền": "background",
    "nền": "background",
    "background": "background",
    "đồng hồ": "watch",
    "túi": "bag", "ba lô": "backpack",
    "điện thoại": "phone",
    "xe": "car", "cây": "tree",
    "logo": "logo", "chữ": "text", "watermark": "watermark",
    "bông tai": "earring", "dây chuyền": "necklace",
    "cà vạt": "tie", "giày": "shoes", "dép": "sandals",
}

# Từ thừa cần loại khỏi tên vật thể
_FILLER = ["của tôi", "của", "trong ảnh", "trong hình", "ở trong",
           "phía trên", "bên trên", "này", "đó", "kia", "ấy", "ạ"]


def translate_object(vi_text: str) -> str:
    """Dịch tên vật thể — ưu tiên từ điển (key dài trước), fallback Llama"""
    key = vi_text.lower().strip()
    # Bỏ từ thừa
    for f in _FILLER:
        key = key.replace(f, "").strip()
    key = re.sub(r"\s+", " ", key).strip()

    # Khớp chính xác
    if key in OBJECT_DICT:
        return OBJECT_DICT[key]
    # Khớp một phần — ưu tiên key DÀI NHẤT (cánh tay > tay)
    for k in sorted(OBJECT_DICT.keys(), key=len, reverse=True):
        if k in key:
            return OBJECT_DICT[k]
    # Fallback: Llama dịch
    return translate_vi_en(key) if key else "object"


def parse_inpaint_command(text: str):
    """
    Phân tích lệnh inpaint:
    - "thay X bằng Y" / "đổi X thành Y" → ("replace", "X", "Y")
    - "xóa X" / "bỏ X" / "remove X"     → ("remove", "X", None)
    Trả về (action, target, replacement) hoặc (None, None, None)
    """
    t = text.lower().strip()
    # Thay thế
    for pat in [r"thay (?:thế )?(.+?) bằng (.+)",
                r"đổi (.+?) thành (.+)",
                r"replace (.+?) with (.+)"]:
        m = re.search(pat, t)
        if m:
            return ("replace", m.group(1).strip(), m.group(2).strip())
    # Xóa
    for pat in [r"xóa (?:cái |con |bỏ )?(.+?)(?:\s+ra|\s+khỏi|\s+đi|$)",
                r"bỏ (?:cái |con )?(.+?)(?:\s+ra|\s+khỏi|\s+đi|$)",
                r"loại bỏ (.+?)(?:\s+ra|\s+khỏi|$)",
                r"remove (?:the )?(.+?)(?:\s+from|$)"]:
        m = re.search(pat, t)
        if m:
            return ("remove", m.group(1).strip(), None)
    return (None, None, None)


def inpaint_fn(input_image, action, target_vi, replacement_vi,
               style, resolution, progress=_dummy_progress):
    """
    Inpainting có hướng dẫn bằng text:
    1. GroundingDINO + SAM: tìm và tạo mask cho 'target'
    2. Xóa vật:
       - LaMa (nếu cài comfyui-inpaint-nodes): lấp nền theo ngữ cảnh, THẬT, nhẹ
       - Fallback SD: VAEEncodeForInpaint + KSampler vẽ nền
    3. Thay vật: SD inpaint vẽ vật mới
    """
    if not get_segment_available():
        raise gr.Error(
            "Chưa cài node Segment Anything!\n"
            "Cần: git clone comfyui_segment_anything (xem hướng dẫn tôi gửi)."
        )

    # Dịch tên vật thể sang English cho GroundingDINO
    target_en = translate_object(target_vi)
    print(f"🎯 Target: {target_vi} → {target_en}")

    progress(0.1, desc="📤 Upload ảnh...")
    img = input_image.convert("RGB")
    w, h = img.size
    target = int(resolution)
    scale = min(target/w, target/h)
    nw = max(64, (int(w*scale)//64)*64)
    nh = max(64, (int(h*scale)//64)*64)
    img = img.resize((nw, nh))
    uploaded = upload_image_to_comfy(img)
    seed = random.randint(1, 2**31)

    use_lama = (action == "remove") and get_lama_available()

    # Mở rộng mask: LaMa xử lý mask lớn rất tốt → phủ rộng để xóa HẾT
    if use_lama:
        mask_expand = 45          # LaMa: phủ rộng, xóa sạch
        threshold = 0.22          # bắt nhiều vùng vật thể hơn
    elif action == "remove":
        mask_expand = 30          # SD remove
        threshold = 0.25
    else:
        mask_expand = 15          # replace
        threshold = 0.25

    # ── Nodes chung: load ảnh + tìm vật + tạo mask ──
    base = {
        "20": {"class_type": "LoadImage", "inputs": {"image": uploaded}},
        "30": {"class_type": "GroundingDinoModelLoader (segment anything)",
               "inputs": {"model_name": "GroundingDINO_SwinT_OGC (694MB)"}},
        "31": {"class_type": "SAMModelLoader (segment anything)",
               "inputs": {"model_name": "sam_vit_b (375MB)"}},
        "32": {"class_type": "GroundingDinoSAMSegment (segment anything)",
               "inputs": {"grounding_dino_model": ["30", 0], "sam_model": ["31", 0],
                          "image": ["20", 0], "prompt": target_en, "threshold": threshold}},
        "33": {"class_type": "GrowMask",
               "inputs": {"mask": ["32", 1], "expand": mask_expand, "tapered_corners": True}},
    }

    if use_lama:
        # ══ XÓA bằng LaMa — lấp nền theo ngữ cảnh, thật & nhẹ ══
        lama_model = get_lama_model()
        print(f"🧹 LaMa removal: {lama_model}")
        workflow = {
            **base,
            # Feather viền mask cho mượt (mask lớn cần feather rộng hơn)
            "34": {"class_type": "FeatherMask",
                   "inputs": {"mask": ["33", 0], "left": 14, "top": 14, "right": 14, "bottom": 14}},
            "40": {"class_type": "INPAINT_LoadInpaintModel",
                   "inputs": {"model_name": lama_model}},
            "41": {"class_type": "INPAINT_InpaintWithModel",
                   "inputs": {"inpaint_model": ["40", 0], "image": ["20", 0],
                              "mask": ["34", 0], "seed": seed}},
            "70": {"class_type": "SaveImage",
                   "inputs": {"images": ["41", 0], "filename_prefix": "yumeai_lama"}},
        }
        method = "LaMa (lấp nền theo ngữ cảnh)"
    else:
        # ══ XÓA/THAY bằng SD inpaint ══
        model = get_inpaint_model()
        if not model:
            raise gr.Error("Không tìm thấy checkpoint!")
        print(f"🎨 SD inpaint model: {model}")

        if action == "replace":
            fill_en = translate_object(replacement_vi)
            full_prompt = f"{fill_en}, photorealistic, highly detailed, seamless, natural lighting"
            print(f"🔄 Replace với: {replacement_vi} → {fill_en}")
        else:
            full_prompt = "empty background, clean, seamless, natural, nothing there, plain"
            print("🗑️ Remove (SD): vẽ nền thay vào")

        neg_prompt = "worst quality, low quality, blurry, deformed, artifacts, extra objects, duplicate"
        workflow = {
            **base,
            "1":  {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
            "40": {"class_type": "CLIPTextEncode", "inputs": {"text": full_prompt, "clip": ["1", 1]}},
            "41": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_prompt,  "clip": ["1", 1]}},
            "42": {"class_type": "VAEEncodeForInpaint",
                   "inputs": {"pixels": ["20", 0], "vae": ["1", 2],
                              "mask": ["33", 0], "grow_mask_by": 12}},
            "50": {"class_type": "KSampler",
                   "inputs": {"model": ["1", 0], "positive": ["40", 0], "negative": ["41", 0],
                              "latent_image": ["42", 0], "seed": seed,
                              "steps": 32, "cfg": 7.5,
                              "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0}},
            "60": {"class_type": "VAEDecode", "inputs": {"samples": ["50", 0], "vae": ["1", 2]}},
            "70": {"class_type": "SaveImage",
                   "inputs": {"images": ["60", 0], "filename_prefix": "yumeai_inpaint"}},
        }
        method = "SD inpaint"

    act_desc = f"Thay '{target_vi}' bằng '{replacement_vi}'" if action == "replace" else f"Xóa '{target_vi}'"
    progress(0.35, desc=f"🎨 {act_desc}...")
    pid = submit_workflow(workflow)
    result = poll_and_download(pid, progress, 0.35, timeout=300)
    info = f"✅ {act_desc}\n🎯 Phát hiện: {target_en}\n⚙️ {method}\n📐 {nw}x{nh} | 🎲 seed:{seed}"
    return result, info


# ══════════════════════════════════
# FLUX KONTEXT — edit ảnh theo lệnh (hiểu lệnh thật)
# ══════════════════════════════════
def get_flux_models() -> dict:
    """
    Kiểm tra Flux Kontext có đủ model không.
    Trả về dict {unet, t5, clip_l, vae} nếu đủ, else {} (rỗng).
    """
    try:
        unet = _fetch_model_list("UnetLoaderGGUF", "unet_name")
        kontext = next((m for m in unet if "kontext" in m.lower()), None)
        if not kontext:
            return {}

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
    """
    Tìm LoRA tăng tốc cho Flux (Alimama Turbo-Alpha / Hyper / Lightning...).
    Trả về tên file nếu có, else "" (rỗng).
    Để dùng: tải FLUX.1-Turbo-Alpha.safetensors vào ComfyUI/models/loras
    """
    loras = _fetch_model_list("LoraLoaderModelOnly", "lora_name")
    # Ưu tiên tên cụ thể trước, rồi tới từ khóa chung
    for kw in ("turbo-alpha", "turbo_alpha", "flux.1-turbo", "flux1-turbo",
               "alimama", "turbo", "hyper", "lightning", "8step", "8-step"):
        for m in loras:
            if kw in m.lower():
                return m
    return ""


def translate_for_kontext(vi_text: str) -> str:
    """
    Dịch lệnh tiếng Việt thành CÂU LỆNH EDIT tiếng Anh kiểu Kontext.
    Khác translate_vi_en (tag) — Kontext cần câu lệnh tự nhiên, rõ ràng.
    """
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
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": vi_text}
                ],
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
                    guidance=3.5, steps=28, use_turbo=True, lora_strength=1.0,
                    instruction_override=""):
    """
    Edit ảnh bằng Flux Kontext — hiểu lệnh thật (đổi đồ, đổi nền, xóa vật...).
    Rất chậm trên 4GB VRAM (offload sang RAM) nhưng chất lượng cao.

    guidance:      độ bám lệnh. 2.5 = giữ ảnh gốc/đổi nhẹ; 3.0–4.0 = đổi rõ; >4.5 dễ méo.
    steps:         số bước khử nhiễu. 20 nhanh, 28 cân bằng, 32+ nét hơn nhưng chậm hơn.
    use_turbo:     bật LoRA tăng tốc (nếu có file) → ép ~8 bước, nhanh ~5x.
    lora_strength: độ mạnh LoRA turbo (1.0 = chuẩn theo Alimama).
    """
    if progress: progress(0.02, desc="Kiểm tra model Flux...")
    flux = get_flux_models()
    if not flux:
        raise Exception("Flux Kontext chưa đủ model (cần GGUF + clip_l + t5 + ae trong ComfyUI)")

    # Chặn giá trị ngoài khoảng an toàn (phòng khi UI gửi số lạ)
    guidance = max(1.0, min(6.0, float(guidance)))
    lora_strength = max(0.0, min(2.0, float(lora_strength)))

    # Turbo LoRA: model distill chỉ cần ~8 bước → ép step về vùng turbo
    turbo_lora = get_flux_turbo_lora() if use_turbo else ""
    if turbo_lora:
        steps = 8 if int(steps) > 12 else max(4, int(steps))
    else:
        steps = max(12, min(40, int(steps)))
        if use_turbo:
            print("⚠️ Bật Turbo nhưng chưa thấy LoRA tăng tốc trong models/loras — "
                  "chạy bình thường (chậm). Tải FLUX.1-Turbo-Alpha.safetensors để dùng.")

    # Upload ảnh
    if progress: progress(0.05, desc="Upload ảnh...")
    img_name = upload_image_to_comfy(input_image)

    # Lệnh EN: ưu tiên lệnh người dùng tự nhập/sửa; nếu trống thì tự dịch
    if progress: progress(0.10, desc="Chuẩn bị lệnh...")
    _override = (instruction_override or "").strip()
    if _override:
        instruction_en = _override
        print(f"🧠 Kontext (lệnh tự nhập/đã sửa): {instruction_en}")
    else:
        instruction_en = translate_for_kontext(instruction_vi)

    seed = random.randint(1, 2**31)
    print(f"🧠 Flux Kontext: {flux['unet']}  | guidance={guidance} | steps={steps}"
          + (f" | ⚡Turbo x{lora_strength}" if turbo_lora else ""))

    # Workflow Flux Kontext (theo cấu trúc chính thức ComfyUI)
    wf = {
        "1":  {"class_type": "UnetLoaderGGUF",
               "inputs": {"unet_name": flux["unet"]}},
        "2":  {"class_type": "DualCLIPLoader",
               "inputs": {"clip_name1": flux["clip_l"], "clip_name2": flux["t5"], "type": "flux"}},
        "3":  {"class_type": "VAELoader",
               "inputs": {"vae_name": flux["vae"]}},
        "20": {"class_type": "LoadImage",
               "inputs": {"image": img_name}},
        "21": {"class_type": "FluxKontextImageScale",
               "inputs": {"image": ["20", 0]}},
        "22": {"class_type": "VAEEncode",
               "inputs": {"pixels": ["21", 0], "vae": ["3", 0]}},
        "30": {"class_type": "CLIPTextEncode",
               "inputs": {"text": instruction_en, "clip": ["2", 0]}},
        "31": {"class_type": "ReferenceLatent",
               "inputs": {"conditioning": ["30", 0], "latent": ["22", 0]}},
        "32": {"class_type": "FluxGuidance",
               "inputs": {"conditioning": ["31", 0], "guidance": guidance}},   # ← từ UI
        "33": {"class_type": "ConditioningZeroOut",
               "inputs": {"conditioning": ["30", 0]}},
        "50": {"class_type": "KSampler",
               "inputs": {
                   "model": ["1", 0], "positive": ["32", 0], "negative": ["33", 0],
                   "latent_image": ["22", 0], "seed": seed, "steps": steps, "cfg": 1.0,  # ← steps từ UI
                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "60": {"class_type": "VAEDecode",
               "inputs": {"samples": ["50", 0], "vae": ["3", 0]}},
        "70": {"class_type": "SaveImage",
               "inputs": {"images": ["60", 0], "filename_prefix": "yumeai_flux"}},
    }

    # Chèn Turbo LoRA (nếu có) vào giữa UNet và KSampler để chạy ~8 bước
    if turbo_lora:
        wf["10"] = {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": ["1", 0], "lora_name": turbo_lora,
                               "strength_model": lora_strength}}
        wf["50"]["inputs"]["model"] = ["10", 0]

    if progress: progress(0.15, desc="Đang chạy Flux Kontext (rất chậm trên 4GB)...")
    pid = submit_workflow(wf)
    # Flux trên 4GB cực chậm → timeout dài (15 phút)
    img = poll_and_download(pid, progress=progress, start_pct=0.15, timeout=900)

    info = (f"🧠 Flux Kontext (edit thông minh)\n📝 {instruction_en}\n"
            f"🎚️ guidance:{guidance} | steps:{steps} | 🎲 seed:{seed}")
    return img, info


# ══════════════════════════════════
# UNIFIED CHAT HANDLER
# ══════════════════════════════════
def preview_kontext_fn(message):
    """Dịch thử lệnh VI → EN cho Kontext (KHÔNG tạo ảnh) để người dùng xem/sửa trước khi chạy."""
    text = (message or "").strip()
    if not text:
        return gr.update()   # không có gì để dịch → giữ nguyên ô EN
    return translate_for_kontext(text)


def build_multichar_prompt(feat1, out1, feat2, out2, keep):
    """
    Ghép lệnh Kontext nhiều nhân vật theo cấu trúc tối ưu cho Kontext:
    gọi TỪNG người bằng ĐẶC ĐIỂM nhận dạng + tách từng vế bằng ';'.
    Kết quả đổ vào ô 'Lệnh Kontext (EN)' để người dùng xem/sửa rồi Gửi.
    """
    def clause(feat, out, fallback):
        feat, out = (feat or "").strip(), (out or "").strip()
        if not out:
            return None
        subj = f"the girl with {feat}" if feat else fallback
        return f"{subj} wears {out}"

    parts = [c for c in (clause(feat1, out1, "the first girl"),
                         clause(feat2, out2, "the second girl")) if c]
    if not parts:
        return gr.update()   # chưa điền đồ mới cho ai → giữ nguyên ô EN
    sentence = "; ".join(parts)
    if keep:
        sentence += "; keep the same faces, hairstyle, body and pose"
    return sentence[0].upper() + sentence[1:]


def unified_chat(message, history,
                 ref_image, style, resolution,
                 use_ip, use_reactor, use_esrgan,
                 cn_str, face_str, use_flux,
                 flux_guidance, flux_steps,
                 flux_turbo, flux_lora_str,
                 kontext_prompt):
    """
    History format Gradio 6.0: [{"role": "user/assistant", "content": "..."}, ...]
    """
    text = (message or "").strip()
    has_ref = ref_image is not None
    if not text and not has_ref:
        yield history, None
        return

    create_kw = ["tạo ảnh", "vẽ cho", "vẽ ", "tạo ra", "hãy vẽ", "tạo hình",
                 "generate", "draw", "tạo một ảnh", "tạo cho tôi"]
    is_create = any(kw in text.lower() for kw in create_kw) and not has_ref

    user_display = (f"📎 [ảnh tham chiếu]\n{text}" if text else "📎 [ảnh tham chiếu]") if has_ref else text
    user_msg = {"role": "user", "content": user_display}

    # ── Tạo ảnh từ text ──
    if is_create:
        yield history + [user_msg, {"role": "assistant", "content": "⏳ Đang tạo ảnh..."}], None
        try:
            model = get_checkpoint_model(style=style)
            if not model: raise Exception("Không tìm thấy checkpoint!")
            en_tags = translate_vi_en(text)
            style_tags = {
                "Anime":     "anime style, 2d illustration, vibrant colors",
                "Realistic": "photorealistic, 8k, detailed, soft lighting, cinematic",
                "Semi":      "semi-realistic, digital art, painterly",
            }.get(style, "anime style")
            full_prompt = f"{en_tags}, {style_tags}, masterpiece, best quality"
            neg_prompt  = "worst quality, low quality, blurry, deformed, ugly, watermark"
            seed = random.randint(1, 2**31)
            res  = int(resolution)
            wf   = build_quality_workflow(model, full_prompt, neg_prompt,
                                          seed,
                                          "Cân bằng (28 steps)",
                                          "2x (ESRGAN)" if use_esrgan else "Không upscale",
                                          res, res)
            pid = submit_workflow(wf)
            img = poll_and_download(pid, timeout=300)
            new_hist = history + [user_msg, {"role": "assistant", "content": "✅ Ảnh đã tạo xong! Xem bên dưới ↓"}]
            yield new_hist, img
        except Exception as e:
            yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi tạo ảnh: {e}"}], None

    # ── ControlNet hoặc Inpaint hoặc Flux Kontext (đều cần ảnh) ──
    elif has_ref:
        # ── FLUX KONTEXT: nếu bật → dùng cho MỌI lệnh edit (hiểu lệnh thật) ──
        if use_flux:
            if not get_flux_models():
                msg = ("⚠️ Flux Kontext chưa đủ model. Cần trong ComfyUI:\n"
                       "• `models/unet/flux1-kontext-dev-Q3_K_S.gguf`\n"
                       "• `models/text_encoders/clip_l.safetensors` + `t5xxl_fp8_e4m3fn_scaled.safetensors`\n"
                       "• `models/vae/ae.safetensors`\n"
                       "• Node `ComfyUI-GGUF` (city96)\n\n"
                       "Restart ComfyUI sau khi cài đủ.")
                yield history + [user_msg, {"role": "assistant", "content": msg}], None
                return

            yield history + [user_msg, {"role": "assistant",
                  "content": "🧠 Đang edit bằng Flux Kontext...\n⏳ Rất chậm trên 4GB (offload RAM), kiên nhẫn 5-15 phút nhé!"}], None
            try:
                img, info = flux_kontext_fn(
                    ref_image, text or "improve the image quality", _dummy_progress,
                    guidance=flux_guidance, steps=flux_steps,
                    use_turbo=flux_turbo, lora_strength=flux_lora_str,
                    instruction_override=kontext_prompt
                )
                new_hist = history + [user_msg, {"role": "assistant", "content": f"✅ Xong! Xem ảnh bên dưới ↓\n{info}"}]
                yield new_hist, img
            except Exception as e:
                yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi Flux Kontext: {e}"}], None
            return

        # Kiểm tra có phải lệnh xóa/thay vật thể không
        action, target, replacement = parse_inpaint_command(text)

        if action in ("remove", "replace"):
            # Đây CHẮC CHẮN là lệnh inpaint — không rơi sang ControlNet
            if not get_segment_available():
                msg = ("⚠️ Tính năng xóa/thay vật thể cần cài thêm node.\n\n"
                       "Mở terminal chạy:\n"
                       "```\n"
                       'cd "D:\\baitap\\code py\\formyhorny\\ComfyUI\\custom_nodes"\n'
                       "git clone https://github.com/storyicon/comfyui_segment_anything\n"
                       "cd comfyui_segment_anything\n"
                       "pip install -r requirements.txt\n"
                       "```\n"
                       "Rồi restart ComfyUI. Model sẽ tự tải lần đầu.")
                yield history + [user_msg, {"role": "assistant", "content": msg}], None
                return

            # ── INPAINT: xóa / thay vật thể ──
            act_label = "🗑️ Đang xóa vật thể..." if action == "remove" else "🔄 Đang thay vật thể..."
            yield history + [user_msg, {"role": "assistant", "content": act_label}], None
            try:
                img, info = inpaint_fn(ref_image, action, target, replacement,
                                       style, resolution, _dummy_progress)
                new_hist = history + [user_msg, {"role": "assistant", "content": f"{info}\nXem ảnh bên dưới ↓"}]
                yield new_hist, img
            except Exception as e:
                yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi inpaint: {e}"}], None

        else:
            # ── CONTROLNET: giữ khuôn mặt (chỉ khi KHÔNG phải lệnh inpaint) ──
            yield history + [user_msg, {"role": "assistant", "content": "⏳ Đang xử lý khuôn mặt..."}], None
            try:
                img, info = controlnet_fn(
                    ref_image, text or "masterpiece, best quality, highly detailed",
                    "Canny (giữ cấu trúc)", cn_str, face_str,
                    use_ip, use_reactor, use_esrgan, style, resolution,
                    _dummy_progress
                )
                new_hist = history + [user_msg, {"role": "assistant", "content": f"✅ Xong! Xem ảnh bên dưới ↓\n{info}"}]
                yield new_hist, img
            except Exception as e:
                yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi: {e}"}], None

    # ── Chat Yuki bình thường ──
    else:
        current = history + [user_msg, {"role": "assistant", "content": ""}]
        yield current, None
        system = PERSONA["system"]
        ollama_msgs = []
        for m in history:
            role = m.get("role", "")
            content = m.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str) \
               and not content.startswith(("⏳","✅","❌","📎")):
                ollama_msgs.append({"role": role, "content": content})
        ollama_msgs.append({"role": "user", "content": text})
        try:
            with httpx.Client(timeout=30) as c:
                with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                    "model": OLLAMA_MODEL,
                    "messages": [{"role": "system", "content": system}] + ollama_msgs,
                    "stream": True
                }) as resp:
                    full = ""
                    for line in resp.iter_lines():
                        if line:
                            data = json.loads(line)
                            full += data.get("message", {}).get("content", "")
                            current[-1] = {"role": "assistant", "content": full}
                            yield current, None
        except Exception as e:
            current[-1] = {"role": "assistant", "content": f"(Lỗi Ollama: {e})"}
            yield current, None


# ══════════════════════════════════
# GIAO DIỆN — Một trang chat duy nhất
# ══════════════════════════════════
with gr.Blocks(title="YumeAI 🌸") as demo:

    with gr.Row():
        gr.Markdown("# 🌸 YumeAI")
        status_md = gr.Markdown(check_status())

    with gr.Row():

        # ── Cột trái: Chat ──────────────────────
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                height=480,
                show_label=False,
            )
            gen_image = gr.Image(
                label="Ảnh vừa tạo",
                height=280,
            )
            with gr.Row():
                ref_input = gr.Image(
                    label="📎 Ảnh tham chiếu",
                    type="pil",
                    height=160,
                    scale=2,
                )
                with gr.Column(scale=5):
                    msg_input = gr.Textbox(
                        placeholder='Nhắn với Yuki... hoặc "tạo ảnh [mô tả]"',
                        show_label=False,
                        lines=3,
                        container=False,
                    )
                    send_btn = gr.Button("Gửi ↗", variant="primary")

            gr.Markdown(
                "<small>💡 Nhắn bình thường → chat &nbsp;|&nbsp; "
                '"tạo ảnh X" → tạo ảnh &nbsp;|&nbsp; '
                "Upload + mô tả → giữ mặt &nbsp;|&nbsp; "
                'Upload + "xóa X" / "thay X bằng Y" → sửa vật thể &nbsp;|&nbsp; '
                "🧠 Bật Flux Kontext → edit theo lệnh thông minh</small>"
            )

        # ── Cột phải: Cài đặt ──────────────────
        with gr.Column(scale=1, min_width=200):
            gr.Markdown("### ⚙️ Cài đặt")
            style = gr.Radio(["Anime","Realistic","Semi"], value="Anime", label="Phong cách")
            resolution = gr.Radio(["384","512","640","768"], value="512", label="Độ phân giải")
            gr.Markdown("**Pipeline giữ mặt:**")
            use_ip      = gr.Checkbox(label="IP-Adapter",    value=True)
            use_reactor = gr.Checkbox(label="ReActor",       value=False)
            use_esrgan  = gr.Checkbox(label="ESRGAN 2x",     value=False)
            cn_str   = gr.Slider(0.3, 1.0, 0.6, step=0.05, label="ControlNet")
            face_str = gr.Slider(0.3, 1.0, 0.7, step=0.05, label="Face strength")
            gr.Markdown("---")
            gr.Markdown("**🧠 Edit thông minh:**")
            use_flux = gr.Checkbox(label="Flux Kontext (hiểu lệnh, rất chậm)", value=False)
            flux_guidance = gr.Slider(1.0, 5.0, value=3.5, step=0.1,
                                      label="Kontext Guidance (cao = đổi mạnh hơn)")
            flux_steps    = gr.Slider(16, 40, value=28, step=4,
                                      label="Kontext Steps (cao = nét hơn nhưng chậm hơn)")
            flux_turbo    = gr.Checkbox(value=True,
                                      label="⚡ Turbo LoRA (ép ~8 bước, nhanh ~5x)")
            flux_lora_str = gr.Slider(0.0, 1.5, value=1.0, step=0.05,
                                      label="Turbo LoRA strength (1.0 = chuẩn)")
            kontext_prompt = gr.Textbox(
                label="Lệnh Kontext (EN) — để trống = tự dịch; điền = dùng thẳng",
                placeholder="vd: The blonde girl wears a red bikini; the dark-haired girl wears a navy one-piece swimsuit",
                lines=3,
            )
            preview_btn = gr.Button("🔤 Dịch thử (xem lệnh EN, không tạo ảnh)", size="sm")
            gr.Markdown("<small>Quy trình chuẩn: gõ tiếng Việt ở khung chat → bấm <b>Dịch thử</b> → "
                        "sửa lại lệnh EN cho đúng món đồ → bấm Gửi. Để trống ô này = tự dịch như cũ.</small>")
            with gr.Accordion("🧩 Ghép lệnh nhiều nhân vật (tùy chọn)", open=False):
                gr.Markdown("<small>Điền đặc điểm nhận dạng + đồ mới cho từng người rồi bấm "
                            "<b>Ghép lệnh</b>. Câu EN chuẩn sẽ đổ vào ô 'Lệnh Kontext (EN)' phía trên "
                            "để bạn xem/sửa rồi Gửi. (Chỉ điền 1 người cũng được.)</small>")
                mc_feat1 = gr.Textbox(label="Người 1 — đặc điểm", placeholder="blonde hair / red dress", lines=1)
                mc_out1  = gr.Textbox(label="Người 1 — đồ mới",   placeholder="a red bikini", lines=1)
                mc_feat2 = gr.Textbox(label="Người 2 — đặc điểm", placeholder="dark hair, blue bow", lines=1)
                mc_out2  = gr.Textbox(label="Người 2 — đồ mới",   placeholder="a navy one-piece swimsuit", lines=1)
                mc_keep  = gr.Checkbox(value=True, label="Giữ nguyên mặt / tóc / dáng / tư thế")
                mc_build_btn = gr.Button("🧩 Ghép lệnh → ô EN", size="sm")
            gr.Markdown("<small>Bật để đổi đồ/nền/xóa vật theo lệnh tự nhiên. Bỏ các tùy chọn trên — Flux tự xử lý.<br>"
                        "Ảnh không đổi → tăng <b>Guidance</b> 3.5→4.0. Vẫn không đổi → đổi model sang bản <b>Q4_K_M</b>.<br>"
                        "Đang test cho nhanh thì kéo <b>Steps</b> về 16–20; ưng rồi nâng lên 28–32 cho nét.<br>"
                        "<b>⚡ Turbo</b>: cần file <code>FLUX.1-Turbo-Alpha.safetensors</code> trong <code>models/loras</code>. "
                        "Khi bật, Steps tự ép ~8 và nên để Guidance ~3.5.</small>")
            gr.Markdown("---")
            refresh_btn = gr.Button("🔄 Refresh", size="sm")
            refresh_btn.click(check_status, outputs=status_md)

    shared_inputs = [msg_input, chatbot, ref_input,
                     style, resolution,
                     use_ip, use_reactor, use_esrgan,
                     cn_str, face_str, use_flux,
                     flux_guidance, flux_steps,
                     flux_turbo, flux_lora_str,
                     kontext_prompt]

    preview_btn.click(preview_kontext_fn, inputs=[msg_input], outputs=[kontext_prompt])

    mc_build_btn.click(build_multichar_prompt,
                       inputs=[mc_feat1, mc_out1, mc_feat2, mc_out2, mc_keep],
                       outputs=[kontext_prompt])

    send_btn.click(
        unified_chat, inputs=shared_inputs, outputs=[chatbot, gen_image]
    ).then(lambda: (gr.update(value=""), gr.update(value="")),
           outputs=[msg_input, kontext_prompt])

    msg_input.submit(
        unified_chat, inputs=shared_inputs, outputs=[chatbot, gen_image]
    ).then(lambda: (gr.update(value=""), gr.update(value="")),
           outputs=[msg_input, kontext_prompt])


if __name__ == "__main__":
    print("\n" + "="*55)
    print("🌸  YumeAI — Unified Chat")
    print("="*55)
    print(check_status())
    print("-"*55)
    print(check_controlnet_status())
    print("="*55 + "\n")
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        theme=gr.themes.Soft(primary_hue="purple")
    )