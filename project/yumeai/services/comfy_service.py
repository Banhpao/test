"""
YumeAI ComfyUI Service
- Lấy model từ ComfyUI object_info
- Chọn checkpoint theo style / kiến trúc SD1.5-SDXL
- Chọn ControlNet/IP-Adapter khớp kiến trúc
- Upload ảnh, submit workflow, poll và download ảnh
"""
import io
import time
import uuid

import httpx
from PIL import Image

from yumeai.core.config import COMFY_URL


class ComfyUIError(Exception):
    """Lỗi khi giao tiếp với ComfyUI (upload/submit/poll/timeout).

    Framework-agnostic — KHÔNG dùng gr.Error ở đây để main.py (FastAPI) dùng chung
    comfy_service mà không phải cài gradio. Bên gọi tự đổi sang kiểu lỗi phù hợp:
    app_gradio.py bọc lại thành gr.Error, main.py bắt và đổi thành HTTPException.
    """
    pass

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

SDXL_PATTERNS = ["xl", "pony", "sdxl", "illustrious", "_il", "realskin",
                 "mango", "moodypro", "_eps", "coreshift", "noobai", "animagine"]

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
            raise ComfyUIError(f"Upload ảnh lỗi: {r.text}")
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
            raise ComfyUIError(f"ComfyUI từ chối: {r.text}")
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
                    raise ComfyUIError(f"ComfyUI lỗi: {msgs}")
                for node_out in job.get("outputs", {}).values():
                    imgs = node_out.get("images", [])
                    if imgs:
                        img_info = imgs[0]
                        print(f"✅ Done in {elapsed:.0f}s")
                        break
                if img_info:
                    break
            except ComfyUIError:
                raise
            except Exception as e:
                print(f"⚠️ Poll: {e}")

    if not img_info:
        raise ComfyUIError("Timeout — ComfyUI chạy quá lâu")

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

def build_quality_workflow(model, full_prompt, neg_prompt, seed, quality, upscale,
                            width=512, height=768, latent_source=None) -> dict:
    """
    Build workflow SD với quality settings và optional upscaler.
    (Chuyển từ app_gradio.py sang đây để main.py dùng chung, không tự định nghĩa lại.)
    latent_source: None = EmptyLatentImage (text2img), tuple = (node_id, slot) cho img2img
    """
    if _is_sdxl(model):
        if width  < 768: width  = 768
        if height < 768: height = 768
        print(f"  [SDXL] resolution → {width}x{height}")

    q_settings = {
        "Nhanh (20 steps)":   {"steps": 20, "cfg": 7.0, "sampler": "euler",          "scheduler": "normal"},
        "Cân bằng (28 steps)":{"steps": 28, "cfg": 7.5, "sampler": "dpm_2",          "scheduler": "karras"},
        "Cao (35 steps)":     {"steps": 35, "cfg": 8.0, "sampler": "dpm_2_ancestral","scheduler": "karras"},
        "Tốt nhất (50 steps)":{"steps": 50, "cfg": 8.5, "sampler": "dpm_2_ancestral","scheduler": "karras"},
    }.get(quality, {"steps": 28, "cfg": 7.5, "sampler": "euler", "scheduler": "normal"})

    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPTextEncode",         "inputs": {"text": full_prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",         "inputs": {"text": neg_prompt,  "clip": ["1", 1]}},
    }

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
        up_models = get_upscale_models()
        if up_models:
            up_m = next((m for m in up_models if "anime" in m.lower()), up_models[0])
            wf["7"]  = {"class_type": "UpscaleModelLoader",   "inputs": {"model_name": up_m}}
            wf["8"]  = {"class_type": "ImageUpscaleWithModel","inputs": {"upscale_model": ["7", 0], "image": ["6", 0]}}
            wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "yumeai_up"}}
        else:
            print("⚠️ Chưa có ESRGAN model, bỏ qua upscale")
            wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "yumeai"}}
    elif upscale == "1.5x (Latent)":
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
        wf["10"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "yumeai"}}

    return wf


def get_reactor_available() -> bool:
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/ReActorFaceSwap")
            return "ReActorFaceSwap" in r.json()
    except:
        return False

def get_upscale_models() -> list:
    return _fetch_model_list("UpscaleModelLoader", "model_name")

